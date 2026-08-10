# Before/after report (§2.5) — and the gate blocking it live (§2.4)

## The gate blocked a real regressed version in a real Actions run

Not a described gate. Branch `regression-demo` removes `lookup_order` from the
offered tools and was pushed through the real pipeline:

| Run | Branch | Result |
|---|---|---|
| [31296187273](https://github.com/iamr1ddl3/ecom-order-support-agent/actions/runs/31296187273) | `assignment-2` | **success** — 13/13, 100.0%, gate PASS |
| [31296404885](https://github.com/iamr1ddl3/ecom-order-support-agent/actions/runs/31296404885) | `regression-demo` | **failure** — 8/13, 61.5%, gate FAIL |

```
[T01] FAIL  order_status   ['tool:check_account_status']
[T02] FAIL  order_status   ['tool:check_account_status']
[T03] FAIL  order_status   ['retrieval:subscription_cancellation', 'tool:check_account_status']
[T06] FAIL  delivery_issue ['tool:check_account_status']
[T12] FAIL  order_status   ['tool:check_account_status']

Pass rate: 61.5%  (8/13 tickets)
Baseline:  100.0%
Delta:     -38.5 points (gate fails below -10)
REGRESSION GATE: FAIL — dropped 38.5 points, limit is 10.
Failing tickets: ['T01', 'T02', 'T03', 'T06', 'T12']
```

CI reproduced the local before/after figure exactly — same rate, same five
tickets — which is the useful property: the gate isn't measuring runner
conditions, it's measuring the agent.

`regression-demo` is deliberately broken and is **not for merge**. It exists so
the failing run is real and clickable.

---

# The local report

```bash
python -m eval.before_after --provider groq
```

## The number

**100% → 62%, and the tickets that flipped are T01, T02, T03, T06, T12.**

```
Regression applied : drop_lookup_order
Tickets            : 13
Before             : 100.0%  (13/13)
After              :  61.5%  (8/13)
Change             : -38.5 points
```

The gate threshold is 10 points. A 38.5-point drop blocks the build, and the
script exits 1:

```
$ AGENT_REGRESSION=drop_lookup_order python -m eval.trajectory_eval; echo $?
REGRESSION GATE: FAIL — dropped 53.8 points, limit is 10.
1

$ python -m eval.trajectory_eval; echo $?
REGRESSION GATE: PASS
0
```

(The gate run scores lower than the before/after run's "after" column — 46.2% vs
61.5% — because each is a fresh set of live LLM calls. Both are far past the
threshold, which is the point of a 10-point band rather than an exact-match
assertion.)

## The regression

`AGENT_REGRESSION=drop_lookup_order` removes `lookup_order` from the tool list
the model is offered (`agent/tools_schema.py`). This is §2.4's own example —
"removing a tool" — and it is real: the model genuinely cannot call it, and
falls back to `check_account_status`, which returns the customer's order *IDs*
but no status or delivery date.

## Why the final answers still read fine

This is what the report is for. Every flipped ticket produced a fluent,
professional, apologetic response that a reviewer skimming final text would pass:

> **T01** — "Hi Asha, I've confirmed that your account is active and that
> **ord_5001** is indeed one of your orders. However, I don't have the current
> status of that specific..."

> **T06** — "I'm sorry to hear that your keyboard is still delayed. Your account
> is active and ord_7004 is listed under your order history, but I don't have the
> real-time sh..."

Nothing is fabricated. The agent correctly reports what it can and cannot see.
Read in isolation these are *good* answers — arguably better-behaved than the
baseline's, since they refuse to guess. Only the trajectory shows the capability
regression: `['tool:lookup_order']` became `['tool:check_account_status']`.

A gate reading final answers would have to decide that a polite, honest,
correctly-hedged response is a failure. A gate reading trajectories just notices
the required step is missing.

## T12: the flip that matters most

T12 is the cross-customer security ticket — `cust_1001` asking for `ord_6002`,
which belongs to `cust_2002`. The permission gate must block it.

Before: `['tool:lookup_order']`, gate DENY, PASS.
After: `['tool:check_account_status']` — **FAIL**, with the reason:

```
expected the model to propose 'lookup_order' so the gate could block it; it never did
```

With the tool removed the model never proposes the cross-customer read, so the
gate is never exercised. The security boundary stops being *tested* without
anything appearing to break — and the agent's answer ("I don't see an order
ord_6002 in your account history") is arguably still the correct refusal.

A naive check of "was the call denied?" would pass this: nothing was allowed
that shouldn't have been. That is why `expect_gate_deny` asserts the call was
**proposed AND blocked**. A security control that is never reached is not a
security control that passed, and this ticket is the reason that distinction is
written into the scorer.

## Second regression flavour

`--regression no_retrieval` disables policy retrieval instead, breaking the
RAG-grounded tickets rather than the tool-calling ones:

```bash
python -m eval.before_after --regression no_retrieval --provider groq
```
