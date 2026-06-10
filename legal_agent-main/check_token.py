"""
Standalone diagnostic for the Agent 365 observability token chain.
Run this from your project folder with the .venv active:
    python check_token.py
"""
import os
import sys
from dotenv import load_dotenv
import httpx

load_dotenv()


def main():
    tenant_id = os.environ.get("AGENT365OBSERVABILITY__TENANTID")
    agent_id = os.environ.get("AGENT365OBSERVABILITY__AGENTID")
    blueprint_client_id = os.environ.get("AGENT365OBSERVABILITY__CLIENTID")
    blueprint_secret = os.environ.get("AGENT365OBSERVABILITY__CLIENTSECRET")

    print("=" * 60)
    print("Agent 365 token chain diagnostic")
    print("=" * 60)
    print(f"Tenant ID:         {tenant_id}")
    print(f"Agent Identity ID: {agent_id}")
    print(f"Blueprint App ID:  {blueprint_client_id}")
    secret_status = (
        f"set ({len(blueprint_secret)} chars)"
        if blueprint_secret
        else "MISSING"
    )
    print(f"Blueprint Secret:  {secret_status}")
    print()

    if not all([tenant_id, agent_id, blueprint_client_id, blueprint_secret]):
        print("ERROR: One or more required env vars are missing.")
        sys.exit(1)

    token_endpoint = (
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    )

    # --- Hop 1+2 ---
    print("--- Hop 1+2: Blueprint client_credentials + fmi_path ---")
    print(f"POST {token_endpoint}")
    print(f"  client_id   = {blueprint_client_id}")
    print(f"  scope       = api://AzureADTokenExchange/.default")
    print(f"  fmi_path    = {agent_id}")
    print()

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
    except Exception as exc:
        print(f"Transport error: {exc}")
        sys.exit(1)

    print(f"Status: {fmi_resp.status_code}")
    print(f"Body:   {fmi_resp.text[:1500]}")
    print()

    if fmi_resp.status_code != 200:
        print("Hop 1+2 FAILED. Stopping.")
        sys.exit(1)

    fmi_token = fmi_resp.json().get("access_token")
    if not fmi_token:
        print("Hop 1+2 returned no access_token. Stopping.")
        sys.exit(1)

    print(f"Hop 1+2 SUCCEEDED. FMI token = {len(fmi_token)} chars.")
    print()

    # --- Hop 3 ---
    print("--- Hop 3: Agent Identity using FMI as client_assertion ---")
    print(f"POST {token_endpoint}")
    print(f"  client_id   = {agent_id}")
    print(f"  assertion   = <FMI token>")
    print(f"  scope       = api://9b975845-388f-4429-889e-eab1ef63949c/.default")
    print()

    try:
        obs_resp = httpx.post(
            token_endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": agent_id,
                "client_assertion_type": (
                    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
                ),
                "client_assertion": fmi_token,
                "scope": "api://9b975845-388f-4429-889e-eab1ef63949c/.default",
            },
            timeout=30.0,
        )
    except Exception as exc:
        print(f"Transport error: {exc}")
        sys.exit(1)

    print(f"Status: {obs_resp.status_code}")
    print(f"Body:   {obs_resp.text[:1500]}")
    print()

    if obs_resp.status_code == 200:
        body = obs_resp.json()
        access_token = body.get("access_token")
        if access_token:
            print("=" * 60)
            print("Both hops SUCCEEDED.")
            print(f"  Observability token = {len(access_token)} chars")
            print(f"  Expires in          = {body.get('expires_in')} seconds")
            print("=" * 60)
            print()
            print("Telemetry SHOULD be flowing. If the admin UI is still")
            print("empty, that's an ingestion/UI delay issue, not auth.")
            return

    print("Hop 3 FAILED.")
    sys.exit(1)


if __name__ == "__main__":
    main()