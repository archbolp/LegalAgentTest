import os
import re
import uuid
import tempfile

import markdown
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from agent import build_graph, AgentState
from otel import setup_otel


load_dotenv()

agent_id = os.getenv("GEN_AI_AGENT_ID", "legal-diff-agent")
agent_name = os.getenv("GEN_AI_AGENT_NAME", "Legal Diff Agent")

app = FastAPI(title="Legal Diff Agent", version="1.0.0")
tracer = setup_otel(app)
templates = Jinja2Templates(directory="templates")
graph = build_graph()

ALLOWED_EXT = {".pdf"}


def prettify_summary(summary: str) -> dict:
    summary = summary or ""
    parts = re.split(r"\*\*Most Risky Changes:\*\*", summary, maxsplit=1)

    main_md = parts[0].strip()
    risky_md = parts[1].strip() if len(parts) > 1 else ""

    summary_html = markdown.markdown(
        main_md,
        extensions=["extra", "sane_lists"],
        output_format="html5",
    )

    risky = []
    if risky_md:
        for line in risky_md.splitlines():
            line = line.strip()
            if not line:
                continue
            risky.append(line)

    return {"summary_html": summary_html, "risky": risky}


def _check_ext(filename: str):
    _, ext = os.path.splitext(filename.lower())
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext}. Allowed: {sorted(ALLOWED_EXT)}",
        )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/diff")
async def diff_contracts(
    old: UploadFile = File(...),
    new: UploadFile = File(...),
):
    _check_ext(old.filename)
    _check_ext(new.filename)

    req_id = str(uuid.uuid4())

    with tempfile.TemporaryDirectory(prefix="legal_diff_") as tmp:
        old_path = os.path.join(tmp, f"old_{req_id}.pdf")
        new_path = os.path.join(tmp, f"new_{req_id}.pdf")

        old_bytes = await old.read()
        new_bytes = await new.read()

        if not old_bytes or not new_bytes:
            raise HTTPException(status_code=400, detail="Both files must be non-empty.")

        with open(old_path, "wb") as f:
            f.write(old_bytes)

        with open(new_path, "wb") as f:
            f.write(new_bytes)

        with tracer.start_as_current_span("agent.graph.invoke") as span:
            # GenAI semantic conventions
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.id", agent_id)
            span.set_attribute("gen_ai.agent.name", agent_name)

            span.set_attribute("request_id", req_id)
            span.set_attribute("old.filename", old.filename or "")
            span.set_attribute("new.filename", new.filename or "")
            span.set_attribute("old.bytes", len(old_bytes))
            span.set_attribute("new.bytes", len(new_bytes))

            state = graph.invoke(AgentState(old_path=old_path, new_path=new_path))
            raw = state.get("report") or {}

    return JSONResponse(content={"request_id": req_id, "report": raw})


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            #"request": request,
            "report": None,
            "filters": {
                "show_unchanged": False,
                "risk_high": True,
                "risk_medium": True,
                "risk_low": True,
            },
        },
    )


@app.post("/ui/diff", response_class=HTMLResponse)
async def ui_diff(
    request: Request,
    old: UploadFile = File(...),
    new: UploadFile = File(...),
    show_unchanged: bool = Form(False),
    risk_high: bool = Form(True),
    risk_medium: bool = Form(True),
    risk_low: bool = Form(True),
):
    _check_ext(old.filename)
    _check_ext(new.filename)

    req_id = str(uuid.uuid4())

    with tempfile.TemporaryDirectory(prefix="legal_diff_") as tmp:
        old_path = os.path.join(tmp, f"old_{req_id}.pdf")
        new_path = os.path.join(tmp, f"new_{req_id}.pdf")

        old_bytes = await old.read()
        new_bytes = await new.read()

        if not old_bytes or not new_bytes:
            raise HTTPException(status_code=400, detail="Both files must be non-empty.")

        with open(old_path, "wb") as f:
            f.write(old_bytes)

        with open(new_path, "wb") as f:
            f.write(new_bytes)

        with tracer.start_as_current_span("agent.graph.invoke") as span:
            # GenAI semantic conventions
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.id", agent_id)
            span.set_attribute("gen_ai.agent.name", agent_name)

            span.set_attribute("request_id", req_id)
            span.set_attribute("old.filename", old.filename or "")
            span.set_attribute("new.filename", new.filename or "")
            span.set_attribute("old.bytes", len(old_bytes))
            span.set_attribute("new.bytes", len(new_bytes))

            state = graph.invoke(AgentState(old_path=old_path, new_path=new_path))
            raw = state.get("report") or {}

    risks = set()
    if risk_high:
        risks.add("high")
    if risk_medium:
        risks.add("medium")
    if risk_low:
        risks.add("low")

    items = raw.get("items", []) or []
    filtered = []

    for it in items:
        if it.get("risk") not in risks:
            continue
        if not show_unchanged and it.get("change_type") == "unchanged":
            continue
        filtered.append(it)

    raw["items_filtered"] = filtered
    raw["request_id"] = req_id

    pretty = prettify_summary(raw.get("overall_summary", ""))
    raw["overall_summary_html"] = pretty["summary_html"]

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            #"request": request,
            "report": raw,
            "filters": {
                "show_unchanged": show_unchanged,
                "risk_high": risk_high,
                "risk_medium": risk_medium,
                "risk_low": risk_low,
            },
        },
    )
