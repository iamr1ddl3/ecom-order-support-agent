"""
HTTP wrapper so the ALB has something to route to.

The agent itself is a CLI. An Application Load Balancer needs an HTTP target to
health-check and forward to, so this exposes the same `Harness` over two
endpoints — it adds no agent behaviour of its own. `curl`-able.

  GET  /health   ALB target-group health check. Deliberately does NOT touch the
                 LLM or the database: a health check that calls a paid API turns
                 every 30-second probe into a bill, and a provider blip into a
                 killed task. It answers "is this process serving?", nothing more.
  GET  /ready    Deeper check, called by hand. Verifies the retrieval backend
                 actually answers, which is what you want after a deploy when
                 the question is "did RDS wiring work?"
  POST /chat     {"customer_id": "cust_1001", "message": "...", "session_id": null}
                 -> the reply plus this turn's trajectory, so a trace is visible
                 from the response itself. Pass the returned session_id back to
                 continue the same conversation (multi-turn memory).
  GET  /         The customer-facing chat page (static/index.html).
  GET  /api/customers  The demo customers and their orders, for the page's picker.

Sessions live in this process's memory. They are lost on restart or scale to
zero, and a second replica cannot see the first one's sessions, so run one
replica or turn on sticky sessions. An unknown session_id just starts a fresh
conversation and says so (session_reset), it never errors.

One Harness is built at startup and reused: it holds the provider client and a
pooled pgvector connection, and rebuilding those per request would add a
connection setup to every call.
"""

from __future__ import annotations

import hmac
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

load_dotenv()

app = FastAPI(title="eCom Order Support Agent", version="2.1")

_STATIC = Path(__file__).parent / "static"

# Conversation sessions: id -> _Session. Bounded in both time and count so a
# public URL cannot grow memory without limit.
SESSION_TTL_S = int(os.environ.get("CHAT_SESSION_TTL_S", "1800"))
MAX_SESSIONS = int(os.environ.get("CHAT_MAX_SESSIONS", "500"))
# Per-client request budget for /chat. Every turn is a paid LLM call, so this is
# the cheapest guard against one caller running up the bill. 0 disables it.
RATE_LIMIT_PER_MIN = int(os.environ.get("CHAT_RATE_LIMIT_PER_MIN", "12"))
MAX_MESSAGE_CHARS = 1000
# Daily budgets, reset at midnight UTC. They bound what a public demo URL can
# spend in a day even if someone scripts around the per-minute limit.
DAILY_PER_CLIENT = int(os.environ.get("CHAT_DAILY_PER_CLIENT", "30"))
DAILY_TOTAL = int(os.environ.get("CHAT_DAILY_TOTAL", "300"))
# Optional invite code. When set, /chat only answers callers that send it (the
# page picks it up from a ?code= link). Unset means the chat is open to all.
ACCESS_CODE = os.environ.get("CHAT_ACCESS_CODE", "")

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
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_CHARS,
                         examples=["What's the status of my order ord_5001?"])
    session_id: str | None = Field(None, description="From a previous reply, to continue that conversation.")


class _Session:
    def __init__(self, ticket):
        self.ticket = ticket
        self.last_used = time.monotonic()
        # One turn at a time per conversation: two overlapping turns would
        # interleave their messages in the same history.
        self.lock = threading.Lock()


_sessions: dict[str, _Session] = {}
_sessions_lock = threading.Lock()


def _get_or_create_session(session_id: str | None, customer_id: str) -> tuple[str, _Session, bool]:
    """Returns (id, session, reset). reset is True when the caller sent an id we
    no longer have (expired, restarted, other replica) or one bound to another
    customer, so the page can tell the user the earlier context is gone."""
    from agent.harness import Ticket

    now = time.monotonic()
    with _sessions_lock:
        for sid in [k for k, v in _sessions.items() if now - v.last_used > SESSION_TTL_S]:
            del _sessions[sid]
        sess = _sessions.get(session_id) if session_id else None
        # A session is bound to the customer it was opened for. Switching
        # customers never reuses the old history: that is the same boundary the
        # permission gate enforces on tool calls.
        if sess is not None and sess.ticket.customer_id == customer_id:
            sess.last_used = now
            return session_id, sess, False
        if len(_sessions) >= MAX_SESSIONS:
            oldest = min(_sessions, key=lambda k: _sessions[k].last_used)
            del _sessions[oldest]
        try:
            ticket = Ticket(customer_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        new_id = uuid.uuid4().hex
        sess = _Session(ticket)
        _sessions[new_id] = sess
        return new_id, sess, session_id is not None


_hits: dict[str, deque] = {}
_hits_lock = threading.Lock()


def _client_key(request: Request) -> str:
    # Behind Azure Container Apps / an ALB the socket peer is the proxy, so the
    # caller is the first X-Forwarded-For entry.
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(request: Request) -> None:
    if RATE_LIMIT_PER_MIN <= 0:
        return
    key = _client_key(request)
    now = time.monotonic()
    with _hits_lock:
        q = _hits.setdefault(key, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT_PER_MIN:
            retry = int(60 - (now - q[0])) + 1
            raise HTTPException(status_code=429,
                                detail=f"Too many messages. Try again in {retry} seconds.",
                                headers={"Retry-After": str(retry)})
        q.append(now)
        if len(_hits) > 10_000:  # drop idle clients so the table stays small
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > 60]:
                del _hits[k]


_daily: dict[str, int] = {}
_daily_day = ""
_daily_lock = threading.Lock()


def _check_daily_budget(request: Request) -> None:
    global _daily_day
    today = time.strftime("%Y-%m-%d", time.gmtime())
    key = _client_key(request)
    with _daily_lock:
        if today != _daily_day:
            _daily.clear()
            _daily_day = today
        total = _daily.get("*", 0)
        mine = _daily.get(key, 0)
        if DAILY_TOTAL > 0 and total >= DAILY_TOTAL:
            raise HTTPException(status_code=429, detail="The demo has reached today's message limit. Please come back tomorrow.")
        if DAILY_PER_CLIENT > 0 and mine >= DAILY_PER_CLIENT:
            raise HTTPException(status_code=429, detail="You've reached today's message limit for this demo. Please come back tomorrow.")
        _daily["*"] = total + 1
        _daily[key] = mine + 1


def _check_access(request: Request) -> None:
    if not ACCESS_CODE:
        return
    sent = request.headers.get("x-access-code", "")
    if not hmac.compare_digest(sent.encode(), ACCESS_CODE.encode()):
        raise HTTPException(status_code=401, detail="This demo needs an access code.")


@app.get("/api/config")
def config() -> dict:
    """What the page needs to know before the first message."""
    return {"access_code_required": bool(ACCESS_CODE)}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@app.get("/api/customers")
def customers() -> list[dict]:
    """Demo customers for the page's picker. Mock data only."""
    from mcp_server.mock_data import ACCOUNTS, CUSTOMERS, ORDERS

    return [
        {
            "customer_id": cid,
            "name": info["name"],
            "standing": ACCOUNTS.get(cid, {}).get("standing"),
            "orders": [{"order_id": oid, **{k: v for k, v in o.items() if k != "customer_id"}}
                       for oid, o in ORDERS.items() if o["customer_id"] == cid],
        }
        for cid, info in CUSTOMERS.items()
    ]


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
def chat(req: ChatRequest, request: Request) -> dict:
    """One support turn. Returns the reply and the trajectory this turn took."""
    _check_access(request)
    _check_rate_limit(request)
    _check_daily_budget(request)
    harness = get_harness()
    session_id, sess, reset = _get_or_create_session(req.session_id, req.customer_id)
    with sess.lock:
        ticket = sess.ticket
        start = len(ticket.steps)
        reply = harness.send(ticket, req.message)
        steps = ticket.steps[start:]
        sess.last_used = time.monotonic()
    return {
        "customer_id": req.customer_id,
        "session_id": session_id,
        "session_reset": reset,
        "reply": reply,
        # The same trajectory the trajectory eval scores, for this turn only,
        # so a reviewer can see which tools ran and what the gate decided
        # without the dashboard.
        "trajectory": [s.key() for s in steps],
        "retrieved": [{"doc": s.name, "detail": s.detail} for s in steps if s.kind == "retrieval"],
        "gate_decisions": [
            {"tool": s.name, "allowed": s.allowed, "reason": s.detail}
            for s in steps
            if s.kind == "tool"
        ],
    }
