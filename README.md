# eCom Order Support Agent — Harness, Tools & Grounding

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

- **Python 3.10+** (developed on 3.14). For the default BM25 backend nothing else
  is needed — no database, no Docker, no embedding-model download. The Assignment 2
  pgvector backend does need Postgres and an OpenAI key; see
  [Retrieval on pgvector](#7-retrieval-on-pgvector-262).
- One LLM API key for any one of: **Anthropic** (default), **Groq** (free tier),
  or **GLM via Z.ai**. Groq and GLM share one OpenAI-compatible provider class.

## Setup

```bash
# 1. clone
git clone <your-repo-url>
cd ecom-order-support-agent

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

---

# Assignment 2 — Tracing, Evaluation & the Regression Gate

Four layers on top of the same agent: real tracing, trajectory + LLM-judge
evaluation, a CI gate that blocks a regressed version, and a pgvector retrieval
backend.

Branches: `assignment-1` (tag `assignment-1-submission`) marks the A1 end-state
at `8f793ea`; `assignment-2` branches from it.

## 1. Tracing setup (§2.1)

[LangFuse](https://cloud.langfuse.com) Cloud free tier. Create a project, then
**Settings → API Keys**, and put these in `.env`:

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com   # LANGFUSE_HOST also works
```

Self-hosting instead? Only `LANGFUSE_BASE_URL` changes (e.g.
`http://localhost:3000`). **Keys are optional** — with none set, tracing degrades
to a no-op and the agent, tests and CI all run untraced.

```bash
python main.py --demo          # produces traces
python -m agent.audit_traces   # fetches them back and verifies nesting
```

`audit_traces` exists because a clean terminal run proves nothing about what
reached the dashboard. It asserts one root observation per trace, no orphans, and
correct observation *types*:

```
=== ticket-turn  (5 observations)
  [SPAN] ticket-turn
     └─ [GENERATION] groq-create
     └─ [GENERATION] groq-create
     └─ [RETRIEVER] retrieve-policy
     └─ [TOOL] lookup_order  gate=DENY
```

Verified on all 8 turns of a full `--demo` run. The gate decision rides on the
tool span, so a blocked cross-customer read is visible in the trace as an
*attempt that was denied* rather than a call that never happened.

## 2. Trajectory eval (§2.2)

```bash
python -m eval.trajectory_eval                  # score vs baseline
python -m eval.trajectory_eval --write-baseline # record a new known-good score
```

- **Test tickets:** `eval/test_tickets.json` — 13 tickets across all 4 ticket
  types (§2.2 asks for ≥10 across ≥3).
- **Baseline:** `eval/baseline.json` — 100.0% (13/13).

It scores the **path**, not the prose, in three shapes:

| Assertion | Catches |
|---|---|
| `required_steps` (superset) | the required tool/retrieval never ran |
| `forbidden_steps` (disjoint) | the honest-gap ticket grounded itself in an unrelated doc |
| `expect_gate_deny` | the cross-customer read was **proposed AND blocked** |

`expect_gate_deny` checks *proposed and blocked* deliberately: a run where the
model never proposed the call proves nothing about the gate, and a naive "was
anything wrongly allowed?" check would pass it. See §2.5 below — that's exactly
what the regression does.

**The suite scored 100% on the first run, so `eval/test_check.py` exists** to
prove the scorer isn't vacuous. It feeds deliberately-broken trajectories in and
requires rejection. No LLM, runs free in CI.

## 3. Confident wrong path (§2.2) — `eval/CONFIDENT_WRONG_PATH.md`

**Two real cases**, both found by reading actual output. They fail in opposite
directions, which is the point:

| | T03 | T07 |
|---|---|---|
| Trajectory | contaminated | **correct** |
| Final answer | correct | **false** |
| Trajectory eval | PASS | PASS |
| LLM judge | 10.00 | **0.00** |

- **T03** — "did my standing desk arrive?" retrieves the *subscription
  cancellation policy* on the single word **"ever"** (from "first-ever
  subscription charge"). Answer is fine; unrelated policy silently entered the
  prompt. **Fixed by pgvector**, verified: `bm25 → [('subscription_cancellation',
  1.47)]`, `pgvector → []`.
- **T07** — the agent reports `ord_7004` *"delivered on 2026-07-18"*. The record
  says `status: delayed` and that date is the **estimate**. It then derives "9
  days late" and a severe-weather ruling from the misreading. The trajectory is
  correct, so no path check can catch it.

## 4. LLM-as-judge (§2.3) — `eval/JUDGE_RESULTS.md`

```bash
python -m eval.judge --provider groq
```

Fact-checking rubric ("does every specific claim appear in the reference?"),
scored against the policy doc / order record / prior tickets — never the agent's
own text. **39 real judge calls** (13 tickets × 3 runs). **Mean grounding: 6.82 /
10.**

Each input is scored 3× and averaged because §6's pitfall is real here: **4 of 13
tickets varied by ≥3 points across identical calls**, two by a full 7 points. The
spread prints beside every mean — a 6.00 from {4,10,4} is not a 6.00 from
{6,6,6}.

## 5. The CI gate (§2.4)

**File:** `.github/workflows/eval-gate.yml`. **Threshold: 10 points**, relative to
baseline.

Two jobs. `fast-checks` runs the no-LLM suites (no secrets, so fork PRs still get
signal, and a broken scorer can't hide behind a green eval); `eval-gate` then runs
the trajectory eval. **The pass/fail logic is in the Python, not the YAML** (§7) —
CI just runs the script and uses its exit code.

```bash
python -m eval.trajectory_eval; echo $?                              # 0
AGENT_REGRESSION=drop_lookup_order python -m eval.trajectory_eval; echo $?  # 1
```

## 6. Before/after (§2.5) — `eval/BEFORE_AFTER.md`

```bash
python -m eval.before_after --provider groq
```

**100% → 62%, and the tickets that flipped are T01, T02, T03, T06, T12.** A −38.5
point drop against a 10-point threshold.

The regression (`AGENT_REGRESSION=drop_lookup_order`) removes `lookup_order` from
the tools the model is offered. Every flipped ticket still produces a fluent,
honest, correctly-hedged answer — *"I don't have the current status of that
specific order"* — which is why a final-answer gate would have to call polite,
accurate refusals failures. The trajectory gate just sees the missing step.

**T12 is the flip that matters:** with the tool gone the model never proposes the
cross-customer read, so the permission gate is never exercised. The security
boundary stops being *tested* while nothing appears to break.

## 7. Retrieval on pgvector (§2.6.2)

```bash
docker run -d --name pgvector -p 5432:5432 -e POSTGRES_PASSWORD=postgres pgvector/pgvector:pg17
python -m rag.build_index                             # idempotent
RETRIEVAL_BACKEND=pgvector python -m rag.test_retriever
```

`rag/pgvector_retriever.py` embeds with OpenAI `text-embedding-3-small` (1536-dim)
and queries `policy_docs` with `<=>` (cosine distance), filtering in SQL. Same
`retrieve(query, k) -> [(doc_id, score, text)]` contract as BM25, so the harness
is untouched. `RETRIEVAL_BACKEND=bm25|pgvector` selects; both pass the same
`rag/test_retriever.py`.

**The threshold polarity inverts, and that's the trap.** BM25 is unbounded and
higher-is-better; cosine distance is 0–2 and lower-is-better. Carrying
`min_score=1.0` across would let every irrelevant doc through and silently destroy
the honest-gap path. Calibrated empirically rather than guessed:

```
worst correct hit  (damaged_items)                d = 0.4302
nearest wrong hit  (customs → shipping_delays)    d = 0.5313
=> DEFAULT_MAX_DISTANCE = 0.48  (midpoint, ~0.05 margin each side)
```

An intuitive guess of 0.62 "feels" strict for cosine distance and would have
broken the gap path.

## 8. The two Defensible justifications (§3)

### 1. Why the regression threshold is 10 points

The suite has 13 tickets, so one ticket is ~7.7 points. **10 points tolerates
exactly one flaky ticket and fails on two.** That's the band I want: the agent is
a non-deterministic LLM, and a single turn legitimately phrasing itself without a
tool call is noise, while two is a pattern.

*Looser (20 points)* would wave through two genuinely broken ticket types — on
this suite that's an entire capability, like every order lookup failing, since
the tool-calling tickets cluster. *Tighter (5 points)* would make a single flaky
turn red the build; the gate would then get ignored or re-run until green, which
is worse than no gate because it looks like coverage. The observed regression
drops 38.5–53.8 points, so there's an order of magnitude of headroom above the
noise floor — the threshold isn't finely balanced, and it doesn't need to be.

It is **relative**, not absolute, because an absolute bar ("must score ≥70%")
lets a score rot downward forever without ever crossing the line. A relative
check catches the drift itself.

### 2. What the gate checks — and what that can't catch

**The gate checks the trajectory: which tool and retrieval steps actually ran.**
It does not read the final answer. That's deliberate — §6 names a final-answer
gate as the main way to lose points here, because a fluent answer built on a
skipped policy check reads exactly like a correct one.

**What it cannot catch is T07** (§3 above): the agent called the right tool,
retrieved the right doc, then misread `status: delayed` as delivered and invented
a delivery date and a compensation ruling. The path is *correct*. No path check
can see that, and this is not hypothetical — the judge scored it 0.00 three times
while the trajectory eval passed it.

That is why the LLM judge exists alongside the gate. It is deliberately **not** in
the gate: a score that swings 7 points between identical runs cannot be a
build-breaking threshold without making the build a coin flip. So the division is
explicit — **the gate blocks capability regressions deterministically; the judge
catches grounding failures non-deterministically and is read by a human.**
Neither alone is sufficient, and T03 vs T07 is the proof.

## Assignment 2 file map

```
agent/
  tracing.py             LangFuse init; no-ops cleanly without keys
  audit_traces.py        fetch traces back, assert correct nesting
  harness.py             + tracing spans, + Ticket.steps trajectory capture
eval/
  test_tickets.json      13 tickets, 4 types, with required/forbidden steps
  trajectory_eval.py     the scorer AND the gate's exit code
  test_check.py          proves the scorer can fail (no LLM)
  judge.py               LLM-as-judge, 3x averaged, fact-checking rubric
  before_after.py        clean vs regressed, prints the flipped tickets
  baseline.json          100.0% known-good
  CONFIDENT_WRONG_PATH.md / JUDGE_RESULTS.md / BEFORE_AFTER.md
rag/
  pgvector_retriever.py  cosine-distance retrieval, same contract as BM25
  build_index.py         idempotent index build
.github/workflows/
  eval-gate.yml          fast-checks -> eval-gate (exit code decides)
```

## Status: AWS deployment (§2.6.1, §2.6.3, §2.6.5)

**Not yet deployed.** §2.1–§2.5 and the pgvector migration (§2.6.2) are complete
and verified locally; the ECS Fargate + ALB + ECR stack, the OIDC deploy
pipeline, autoscaling and the teardown script are not in this PR yet. They will
land as further commits on this branch — which stays open per §4.

Stated plainly rather than described as if it were running: §6 is explicit that a
described deployment reads exactly like a described gate nobody watched block
anything.
