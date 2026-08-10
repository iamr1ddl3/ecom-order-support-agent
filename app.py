"""
HTTP wrapper so the ALB has something to route to (§2.6.1).

The agent itself is a CLI. An Application Load Balancer needs an HTTP target to
health-check and forward to, so this exposes the same `Harness` over two
endpoints — it adds no agent behaviour of its own. `curl`-able, per §4.5.

  GET  /health   ALB target-group health check. Deliberately does NOT touch the
                 LLM or the database: a health check that calls a paid API turns
                 every 30-second probe into a bill, and a provider blip into a
                 killed task. It answers "is this process serving?", nothing more.
  GET  /ready    Deeper check, called by hand. Verifies the retrieval backend
                 actually answers, which is what you want after a deploy when
                 the question is "did RDS wiring work?"
  POST /chat     {"customer_id": "cust_1001", "message": "..."} -> the reply plus
                 the trajectory, so a trace is visible from the response itself.

One Harness is built at startup and reused: it holds the provider client and a
pooled pgvector connection, and rebuilding those per request would add a
connection setup to every call.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel, Field

load_dotenv()

app = FastAPI(title="eCom Order Support Agent", version="2.0")

_harness = None


def get_harness():
    """Lazily build the shared Harness on first use.

    Not at import time: uvicorn imports this module before the container is
    necessarily able to reach RDS, and a constructor that raises during import
    gives you a crash-looping task with a stack trace instead of a health
    endpoint that can tell you what's wrong.
    """
    global _harness
    if _harness is None:
        from agent.harness import Harness

        _harness = Harness()
    return _harness


class ChatRequest(BaseModel):
    customer_id: str = Field(..., examples=["cust_1001"])
    message: str = Field(..., examples=["What's the status of my order ord_5001?"])


@app.get("/health")
def health() -> dict:
    """Liveness only — no LLM call, no DB query. See the module docstring."""
    return {"status": "ok", "backend": os.environ.get("RETRIEVAL_BACKEND", "bm25")}


@app.get("/ready")
def ready() -> dict:
    """Readiness: can we actually retrieve? Use this to confirm RDS wiring."""
    try:
        # A real question from rag/test_retriever.py, not a bare keyword phrase:
        # BM25 strips domain stopwords, so "refund policy" tokenizes to nothing
        # and reports not-ready on a perfectly healthy backend. The probe has to
        # be something the corpus genuinely answers, or it only tests itself.
        hits = get_harness().retriever.retrieve(
            "How many days after delivery can I request a refund?", k=1
        )
        return {
            "status": "ready",
            "backend": os.environ.get("RETRIEVAL_BACKEND", "bm25"),
            "retrieval_ok": bool(hits),
            "top_doc": hits[0][0] if hits else None,
        }
    except Exception as exc:
        # 200 with an error body, not a 5xx: this endpoint is for a human
        # diagnosing a deploy, and the message is more useful than the status.
        return {"status": "not_ready", "error": f"{type(exc).__name__}: {exc}"}


@app.post("/chat")
def chat(req: ChatRequest) -> dict:
    """One support turn. Returns the reply and the trajectory it took."""
    from agent.harness import Ticket

    harness = get_harness()
    ticket = Ticket(req.customer_id)
    reply = harness.send(ticket, req.message)
    return {
        "customer_id": req.customer_id,
        "reply": reply,
        # The same trajectory the §2.2 eval scores — exposed so a reviewer can
        # see which tools ran and what the gate decided without the dashboard.
        "trajectory": [s.key() for s in ticket.steps],
        "gate_decisions": [
            {"tool": s.name, "allowed": s.allowed, "reason": s.detail}
            for s in ticket.steps
            if s.kind == "tool"
        ],
    }
