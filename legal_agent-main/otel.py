"""
Agent 365 observability wiring for the Legal Diff Agent.

Replaces the old generic OTel/OTLP exporter with the Microsoft OpenTelemetry
distro, which exports to Agent 365's observability API via a 3-hop FMI
(Federated Managed Identity) token chain:

    Blueprint (client_credentials + fmi_path)
      -> Hop 1+2: FMI token
      -> Hop 3:   FMI token used as client_assertion to obtain the
                  Observability API token (api://9b975845-.../.default)
"""

import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from microsoft.opentelemetry import use_microsoft_opentelemetry
from opentelemetry import trace


_OBS_API_SCOPE = "api://9b975845-388f-4429-889e-eab1ef63949c/.default"

_token_lock = threading.Lock()
_token_cache: dict[str, tuple[str, datetime]] = {}
_EXPIRY_BUFFER = timedelta(minutes=5)


def _is_truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _cache_token(key: str, bearer_token: str, expires_in_seconds: int) -> None:
    expires_at = (
        datetime.now(timezone.utc)
        + timedelta(seconds=expires_in_seconds)
        - _EXPIRY_BUFFER
    )
    with _token_lock:
        _token_cache[key] = (bearer_token, expires_at)


def _get_cached_token(key: str) -> Optional[str]:
    with _token_lock:
        entry = _token_cache.get(key)
    if entry is None:
        return None
    token, expires_at = entry
    if datetime.now(timezone.utc) >= expires_at:
        return None
    return token


def _acquire_observability_token(agent_id: str, tenant_id: str) -> Optional[str]:
    cache_key = "obs|" + tenant_id + "|" + agent_id
    cached = _get_cached_token(cache_key)
    if cached:
        return cached

    blueprint_client_id = os.environ.get("AGENT365OBSERVABILITY__CLIENTID")
    blueprint_secret = os.environ.get("AGENT365OBSERVABILITY__CLIENTSECRET")

    if not blueprint_client_id or not blueprint_secret:
        print("[Agent 365] Missing AGENT365OBSERVABILITY__CLIENTID or CLIENTSECRET - cannot acquire token.")
        return None

    token_endpoint = "https://login.microsoftonline.com/" + tenant_id + "/oauth2/v2.0/token"

    # Hop 1+2: Blueprint client_credentials + fmi_path -> FMI token
    try:
        fmi_resp = httpx.post(
            token_endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": blueprint_client_id,
                "client_secret": blueprint_secret,
                "scope": "api://AzureADTokenExchange/.default",
                "fmi_path": agent_id,
            },
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        print("[Agent 365] Hop 1+2 transport error: " + str(exc))
        return None

    if fmi_resp.status_code != 200:
        print("[Agent 365] Hop 1+2 FMI token failed (" + str(fmi_resp.status_code) + "): " + fmi_resp.text[:500])
        return None

    fmi_token = fmi_resp.json().get("access_token")
    if not fmi_token:
        print("[Agent 365] Hop 1+2 returned no access_token")
        return None

    # Hop 3: Agent Identity using FMI as client_assertion -> Observability API token
    try:
        obs_resp = httpx.post(
            token_endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": agent_id,
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": fmi_token,
                "scope": _OBS_API_SCOPE,
            },
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        print("[Agent 365] Hop 3 transport error: " + str(exc))
        return None

    if obs_resp.status_code != 200:
        print("[Agent 365] Hop 3 observability token failed (" + str(obs_resp.status_code) + "): " + obs_resp.text[:500])
        return None

    body = obs_resp.json()
    access_token = body.get("access_token")
    expires_in = int(body.get("expires_in", 3600))
    if not access_token:
        return None

    bearer = "Bearer " + access_token
    _cache_token(cache_key, bearer, expires_in)
    return bearer


def _token_resolver(agent_id: str, tenant_id: str) -> Optional[str]:
    try:
        return _acquire_observability_token(agent_id, tenant_id)
    except Exception as exc:
        print("[Agent 365] Token resolver error: " + str(exc))
        return None


def setup_otel(app):
    enabled = _is_truthy(os.getenv("ENABLE_A365_OBSERVABILITY_EXPORTER", "false"))

    use_microsoft_opentelemetry(
        enable_a365=enabled,
        enable_azure_monitor=False,
        a365_token_resolver=_token_resolver,
    )

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
    except ImportError:
        print("[Agent 365] opentelemetry-instrumentation-fastapi not installed - FastAPI request spans will not be auto-captured.")

    service_name = os.getenv("GEN_AI_AGENT_NAME", "legal-diff-agent").strip('"')

    if enabled:
        print("[Agent 365] Observability ENABLED - exporting telemetry as '" + service_name + "'.")
    else:
        print("[Agent 365] Observability DISABLED - set ENABLE_A365_OBSERVABILITY_EXPORTER=true to export.")

    return trace.get_tracer(service_name)