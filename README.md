# Order-Support Agent — Harness, Tools & Grounding

A multi-turn e-commerce order-support agent. It handles four ticket types — order
status, delivery issues, refund requests, and subscription/account questions —
by classifying each turn, retrieving the relevant policy, looking up real (mock)
order and account data through tools, and answering with memory across the
conversation. The defining property: **a harness I wrote — not the model —
decides whether each proposed tool call is allowed to run**, and it decides
*before* the call is dispatched. Order and account data are read through an MCP
server; policy answers are grounded in a retrieved policy doc (and the agent says
so honestly when no doc covers the question).

## Prerequisites

- **Python 3.10+** (developed on 3.14). Nothing else needs to be pre-installed —
  the vector store is pure-Python BM25, so there is no database server, no Docker,
  and no embedding-model download.
- One LLM API key for any one of: **Anthropic** (default), **Groq** (free tier),
  or **GLM via Z.ai**. Groq and GLM share one OpenAI-compatible provider class.

## Setup

```bash
# 1. clone
git clone <your-repo-url>
cd order-support-agent

# 2. create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. install dependencies
pip install -r requirements.txt

# 4. configure your key
cp .env.example .env
# then edit .env and set ONE of:
#   ANTHROPIC_API_KEY=...     (and set LLM_PROVIDER=anthropic)
#   GROQ_API_KEY=...          (and set LLM_PROVIDER=groq)
#   ZAI_API_KEY=...           (and set LLM_PROVIDER=glm)
```

## How to run it

**Start here — the scripted demo** runs all 5 ticket scenarios end to end (this is
what the video records):

```bash
python main.py --demo
```

**Interactive chat** as a single customer (multi-turn, same ticket):

```bash
python main.py --customer cust_1001
```

Valid demo customers: `cust_1001`, `cust_2002`, `cust_3003`, `cust_4004`.
Add `--provider groq` / `--provider glm` (or set `LLM_PROVIDER`) to switch backend.

Every run prints, on screen:
- `[RETRIEVER] used chunk: <doc> (score=...)` — which policy doc grounded the answer,
  or `honest gap` when nothing covered the question.
- `[GATE] <tool>(<args>) -> ALLOW/DENY: <reason>` — the harness permission decision
  for every proposed tool call.

**Verify without an API key** (the harness gate, the MCP protections, the retriever):

```bash
python -m agent.test_gate         # permission gate: allow/deny truth table
python -m mcp_server.test_server  # MCP: schema rejection + scoped rejection, live
python -m rag.test_retriever      # retrieval hits expected docs; customs = honest gap
```

## Project structure

```
main.py                  CLI: --demo (5 scenarios) and --customer <id> (chat)
scenarios.py             the 5 demo ticket scenarios
agent/
  harness.py             THE harness loop + the permission gate (_permission_gate)
  providers.py           provider abstraction: get_provider('anthropic'|'groq')
  tools_schema.py        tool specs offered to the model, per-provider shape
  memory.py              short-term buffer + long-term prior-ticket lookup
  test_gate.py           permission-gate unit test (no LLM)
mcp_server/
  server.py              FastMCP stdio server: lookup_order, check_account_status
  mock_data.py           mock orders + accounts (shared customer IDs)
  test_server.py         MCP-layer test: schema + scoped rejection (no LLM)
rag/
  retriever.py           BM25 retriever behind a Retriever interface
  policy_docs/           6 policy docs; customs fees deliberately uncovered
  test_retriever.py      retrieval sanity check (no LLM)
data/
  prior_tickets.py       mock long-term memory, keyed by customer ID
```

Find the harness loop in `agent/harness.py` (`Harness._converse`), the permission
gate at `Harness._permission_gate` in the same file, the MCP server in
`mcp_server/server.py`, and the retriever in `rag/retriever.py`.

## Why I built the harness this way

**The permission boundary is in the harness, before dispatch — not inside the
tool.** The single most important line is in `agent/harness.py`,
`_run_tool_calls`: `decision = self._permission_gate(...)`, and the MCP dispatch on
the following lines runs *only* when `decision.allowed`. The model proposes a
`{tool, args}` call; the gate resolves the requested order/customer ID against the
ticket's bound customer and returns ALLOW or DENY; a denied call never reaches the
server. The MCP server (`mcp_server/server.py`) re-checks ownership too, so no
unscoped path exists anywhere — but that server check is defense-in-depth, not the
boundary. This is deliberate: the assignment's common pitfall is letting the
proposed call execute and bolting the permission check on *inside* the tool as an
afterthought. Here the harness is the real boundary, and you can point at the exact
line where a proposed action is checked before it runs.

### Defensible decisions

**1. How I scoped ticket types, and why.** Four types — `order_status`,
`delivery_issue`, `refund_request`, `subscription_account` — chosen because each
maps cleanly to a *different* combination of the two capabilities the agent has:
order/account tool lookups and policy retrieval. Order-status is tool-only;
refunds are retrieval-heavy; delivery issues need both (look up the order, then
apply the delay policy); account questions lean on long-term memory. Scoping by
*what the agent must do to answer* keeps classification (`Harness.classify`) a
cheap deterministic keyword map instead of another LLM call, and it makes the
capability each type needs obvious. Critically, a ticket is **bound to exactly one
customer at open time** (`Ticket.__init__`) — that binding is what makes
permission scoping enforceable: there is no ambiguous "current customer" for the
gate to guess.

**2. One specific permission-boundary decision.** The gate resolves an `order_id`
to its **owning customer** before allowing the call, rather than only checking that
the ID is well-formed. A well-formed ID for *someone else's* order (`ord_6002`,
owned by `cust_2002`, requested on a `cust_1001` ticket) is the exact leak this
prevents — see `[GATE] ... DENY: order 'ord_6002' belongs to cust_2002` in demo
scenario 5. The gate loads the same ownership source the tools use, so "who owns
this order" is answered once, consistently, at the boundary — not re-derived
differently in each tool.

## Not in scope (by design)

`issue_refund` — the one irreversible action — is intentionally not built. It
belongs later in the cohort, once human-in-the-loop approval and policy guardrails
exist to gate it. The permission gate already rejects it as an unoffered tool
(see `test_gate.py`).
