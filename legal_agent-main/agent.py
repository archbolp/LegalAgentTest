from dotenv import load_dotenv

import re
from typing import Optional, List, Literal, Dict, Any, Tuple
from pydantic import BaseModel, Field

from langchain_core.tools import tool
from langchain_core.documents import Document
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_community.document_loaders import PyPDFLoader

from langgraph.graph import StateGraph, END
from langgraph.prebuilt import create_react_agent

load_dotenv()

ChangeType = Literal["added", "removed", "modified", "unchanged"]


class DiffItem(BaseModel):
    clause_id: Optional[str] = None
    change_type: ChangeType
    old_text: Optional[str] = None
    new_text: Optional[str] = None
    summary: str
    risk: Literal["low", "medium", "high"]
    rationale: str


class DiffReport(BaseModel):
    items: List[DiffItem]
    overall_summary: str


class AgentState(BaseModel):
    old_path: str
    new_path: str
    old_text: Optional[str] = None
    new_text: Optional[str] = None
    old_clauses: Optional[List[Tuple[Optional[str], str]]] = None
    new_clauses: Optional[List[Tuple[Optional[str], str]]] = None
    pairs: Optional[List[Dict[str, Any]]] = None   # {clause_id, old, new, score}
    diffs: Optional[List[Dict[str, Any]]] = None
    notes: List[str] = Field(default_factory=list)
    report: dict | None = None


CLAUSE_RE = re.compile(
    r"(?m)^(?P<id>(?:\d+)(?:\.\d+){0,4})[)\.]?\s+(?P<body>.+?)(?=^\d+(?:\.\d+){0,4}[)\.]?\s+|\Z)",
    re.DOTALL
)


def normalize_text(s: str) -> str:
    s = s.replace("\x00", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def split_into_clauses(text: str) -> List[Tuple[Optional[str], str]]:
    matches = list(CLAUSE_RE.finditer(text))
    if not matches:
        return [(None, text)]
    out = []
    for m in matches:
        cid = m.group("id")
        body = normalize_text(m.group("body"))
        out.append((cid, body))
    return out


@tool
def load_pdf_text(path: str) -> str:
    """Load PDF from path and return normalized text."""
    loader = PyPDFLoader(path)
    pages = loader.load()
    full = "\n".join(p.page_content for p in pages)
    return normalize_text(full)

@tool
def extract_clauses(text: str) -> List[Dict[str, Optional[str]]]:
    """Split contract text into clauses. Returns list of {clause_id, text}."""
    clauses = split_into_clauses(text)
    return [{"clause_id": cid, "text": body} for cid, body in clauses]

@tool
def align_clauses_by_embeddings(
    old_clauses: List[Dict[str, Optional[str]]],
    new_clauses: List[Dict[str, Optional[str]]],
    min_score: float = 0.35,
    k: int = 1,
) -> List[Dict[str, Any]]:
    """
    Align clauses using embeddings similarity.
    Returns list of {clause_id, old, new, score, matched_new_idx, status}
    status in: matched / removed / added
    """
    # Changes 
    if not new_clauses:
     return [
        {"clause_id": oc["clause_id"], "old": oc["text"], "new": None,
         "score": None, "status": "removed"}
        for oc in old_clauses
     ]
    if not old_clauses:
     return [
        {"clause_id": nc["clause_id"], "old": None, "new": nc["text"],
         "score": None, "status": "added"}
        for nc in new_clauses
     ]
    #Changes 
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")

    new_docs = [
        Document(page_content=c["text"], metadata={"idx": i, "clause_id": c["clause_id"]})
        for i, c in enumerate(new_clauses)
    ]
    vs = FAISS.from_documents(new_docs, embeddings)

    used_new = set()
    pairs = []

    for oc in old_clauses:
        res = vs.similarity_search_with_score(oc["text"], k=k)
        if not res:
            pairs.append({"clause_id": oc["clause_id"], "old": oc["text"], "new": None, "score": None, "status": "removed"})
            continue

        best_doc, best_score = res[0]
        sim = 1 / (1 + float(best_score))

        if sim < min_score:
            pairs.append({"clause_id": oc["clause_id"], "old": oc["text"], "new": None, "score": sim, "status": "removed"})
            continue

        idx = best_doc.metadata["idx"]
        used_new.add(idx)

        clause_id = oc["clause_id"] or best_doc.metadata.get("clause_id")
        pairs.append({"clause_id": clause_id, "old": oc["text"], "new": best_doc.page_content, "score": sim, "matched_new_idx": idx, "status": "matched"})

    for i, nc in enumerate(new_clauses):
        if i not in used_new:
            pairs.append({"clause_id": nc["clause_id"], "old": None, "new": nc["text"], "score": None, "status": "added"})

    return pairs


@tool
def compare_clause_llm(clause_id: Optional[str], old_text: Optional[str], new_text: Optional[str]) -> Dict[str, Any]:
    """Compare two clause versions and return DiffItem as dict."""
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0).with_structured_output(DiffItem)

    system = (
        "You are a legal assistant. Compare two versions of a contract clause and return the result strictly according to the schema.\n"
        "Rules: if a clause is missing in one version => added/removed. "
        "Use modified only when meaning has changed, not just style. "
        "Assess risk by impact on money/liability/deadlines/termination/exposure/rights."
    )
    user = f"""clause_id: {clause_id}

OLD:
{old_text or ""}

NEW:
{new_text or ""}
"""
    item = llm.invoke([("system", system), ("user", user)])
    return item.model_dump()


@tool
def summarize_report(diffs: List[Dict[str, Any]]) -> str:
    """Summarize diffs into an overall summary."""
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    lines = "\n".join([f"- [{d['change_type']}][{d['risk']}] {d.get('clause_id')}: {d['summary']}" for d in diffs])
    msg = "Make a brief summary (8-12 lines) and list the 3-5 most risky changes.\n\n" + lines
    return llm.invoke(msg).content


# agentic workflow
def node_load(state: AgentState) -> AgentState:
    state.old_text = load_pdf_text.invoke(state.old_path)
    state.new_text = load_pdf_text.invoke(state.new_path)
    state.notes.append("Loaded documents.")
    return state


def node_extract(state: AgentState) -> AgentState:
    old = extract_clauses.invoke(state.old_text)
    new = extract_clauses.invoke(state.new_text)
    state.old_clauses = [(c["clause_id"], c["text"]) for c in old]
    state.new_clauses = [(c["clause_id"], c["text"]) for c in new]
    state.notes.append(f"Extracted clauses: old={len(old)} new={len(new)}")
    return state


def node_align(state: AgentState) -> AgentState:
    old = [{"clause_id": cid, "text": txt} for cid, txt in state.old_clauses]
    new = [{"clause_id": cid, "text": txt} for cid, txt in state.new_clauses]
    state.pairs = align_clauses_by_embeddings.invoke({"old_clauses": old, "new_clauses": new, "min_score": 0.38, "k": 1})
    state.notes.append(f"Aligned pairs: {len(state.pairs)}")
    return state


def node_diff(state: AgentState) -> AgentState:
    diffs = []
    for p in state.pairs:
        diffs.append(compare_clause_llm.invoke({
            "clause_id": p.get("clause_id"),
            "old_text": p.get("old"),
            "new_text": p.get("new"),
        }))
    state.diffs = diffs
    state.notes.append("Compared all pairs with LLM.")
    return state


def node_finalize(state: AgentState) -> AgentState:
    diffs = state.diffs or []
    summary = summarize_report.invoke({"diffs": diffs})
    state.report = {
        "items": state.diffs or [],
        "overall_summary": summary,
    }
    return state


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("load", node_load)
    g.add_node("extract", node_extract)
    g.add_node("align", node_align)
    g.add_node("diff", node_diff)
    g.add_node("finalize", node_finalize)

    g.set_entry_point("load")
    g.add_edge("load", "extract")
    g.add_edge("extract", "align")
    g.add_edge("align", "diff")
    g.add_edge("diff", "finalize")
    g.add_edge("finalize", END)

    return g.compile()


if __name__ == "__main__":
    app = build_graph()
    result = app.invoke(AgentState(old_path="old.pdf", new_path="new.pdf"))
    import json
    print(json.dumps(result, indent=2, ensure_ascii=False))
