# Tracing, evaluation and the regression gate

Four layers on top of the same agent: real tracing, trajectory + LLM-judge
evaluation, a CI gate that blocks a regressed version, and a pgvector retrieval
backend.

## 1. Tracing setup

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

## 2. Trajectory eval

```mermaid
flowchart LR
    T[("13 test tickets<br/>eval/test_tickets.json")] --> A["Run agent<br/>capture trajectory"]
    A --> S{"Score the path<br/>required · forbidden · gate deny"}
    S --> P["Pass rate"]
    B[("baseline.json<br/>100%")] --> CMP{"Drop > 10 points?"}
    P --> CMP
    CMP -->|"no"| OK(["exit 0 — deploy allowed"])
    CMP -->|"yes"| FAIL(["exit 1 — regression, deploy blocked"])
    A -.->|"no API key / quota"| SETUP(["exit 2 — setup failure"])
    A --> J["LLM judge (3× per ticket)<br/>read by a human, not in the gate"]
```

```bash
python -m eval.trajectory_eval                  # score vs baseline
python -m eval.trajectory_eval --write-baseline # record a new known-good score
```

- **Test tickets:** `eval/test_tickets.json` — 13 tickets across all 4 ticket
  types.
- **Baseline:** `eval/baseline.json` — 100.0% (13/13).

It scores the **path**, not the prose, in three shapes:

| Assertion | Catches |
|---|---|
| `required_steps` (superset) | the required tool/retrieval never ran |
| `forbidden_steps` (disjoint) | the honest-gap ticket grounded itself in an unrelated doc |
| `expect_gate_deny` | the cross-customer read was **proposed AND blocked** |

`expect_gate_deny` checks *proposed and blocked* deliberately: a run where the
model never proposed the call proves nothing about the gate, and a naive "was
anything wrongly allowed?" check would pass it. See the before/after section below — that's exactly
what the regression does.

**The suite scored 100% on the first run, so `eval/test_check.py` exists** to
prove the scorer isn't vacuous. It feeds deliberately-broken trajectories in and
requires rejection. No LLM, runs free in CI.

## 3. Confident wrong path — `eval/CONFIDENT_WRONG_PATH.md`

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

## 4. LLM-as-judge — `eval/JUDGE_RESULTS.md`

```bash
python -m eval.judge --provider groq
```

Fact-checking rubric ("does every specific claim appear in the reference?"),
scored against the policy doc / order record / prior tickets — never the agent's
own text. **39 real judge calls** (13 tickets × 3 runs). **Mean grounding: 6.82 /
10.**

Each input is scored 3× and averaged because judge scores are noisy: **4 of 13
tickets varied by ≥3 points across identical calls**, two by a full 7 points. The
spread prints beside every mean — a 6.00 from {4,10,4} is not a 6.00 from
{6,6,6}.

## 5. The CI gate

**File:** `.github/workflows/eval-gate.yml`. **Threshold: 10 points**, relative to
baseline.

Two jobs. `fast-checks` runs the no-LLM suites (no secrets, so fork PRs still get
signal, and a broken scorer can't hide behind a green eval); `eval-gate` then runs
the trajectory eval. **The pass/fail logic is in the Python, not the YAML** —
CI just runs the script and uses its exit code.

```bash
python -m eval.trajectory_eval; echo $?                                     # 0
AGENT_REGRESSION=drop_lookup_order python -m eval.trajectory_eval; echo $?  # 1
```

Exit codes are three-valued so the gate can't lie about why it's red:
**0** clean · **1** measured regression · **2** setup failure (no API key, etc).
That distinction exists because the first CI runs failed on a missing secret and
then on an exhausted provider quota, both reported as "regression" — and a gate
that goes red for the wrong reason is one people learn to ignore.

### Watched blocking a real regressed version

| Run | Branch | Result |
|---|---|---|
| [31296187273](https://github.com/iamr1ddl3/ecom-order-support-agent/actions/runs/31296187273) | feature branch | **success** — 13/13, gate PASS |
| [31296404885](https://github.com/iamr1ddl3/ecom-order-support-agent/actions/runs/31296404885) | `regression-demo` | **failure** — 8/13, −38.5 points, gate FAIL |

Branch `regression-demo` removes `lookup_order` from the offered tools and is
deliberately not for merge. CI reproduced the local before/after figure exactly
— same 61.5%, same five tickets — so the gate is measuring the agent, not the
runner.

## 6. Before/after — `eval/BEFORE_AFTER.md`

```mermaid
xychart-beta
    title "Trajectory pass rate"
    x-axis ["Clean agent", "lookup_order removed"]
    y-axis "Pass rate (%)" 0 --> 100
    bar [100, 61.5]
```

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

## 7. Retrieval on pgvector

```bash
docker run -d --name pgvector -p 5432:5432 -e POSTGRES_PASSWORD=postgres pgvector/pgvector:pg17
python -m rag.build_index                             # idempotent
RETRIEVAL_BACKEND=pgvector python -m rag.test_retriever
```

`rag/pgvector_retriever.py` embeds with OpenAI `text-embedding-3-small` (1536-dim;
via OpenRouter when only `OPENROUTER_API_KEY` is set)
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

## 8. Two design justifications

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
It does not read the final answer. That's deliberate: a final-answer
gate misses the regressions that matter, because a fluent answer built on a
skipped policy check reads exactly like a correct one.

**What it cannot catch is T07**: the agent called the right tool,
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

## Evaluation, tracing and deployment file map

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

AWS deployment details: [DEPLOYMENT_AWS.md](DEPLOYMENT_AWS.md).
