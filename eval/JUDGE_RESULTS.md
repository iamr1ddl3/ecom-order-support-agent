# LLM-as-judge results (§2.3)

Real scores from a real run, not a description of a judge that exists.

```
Provider : groq (openai/gpt-oss-120b)
Command  : python -m eval.judge --provider groq
Tickets  : 13 (§2.3 asks for >=5)
Runs     : 3 per ticket, averaged  = 39 judge calls
Rubric   : "does every specific claim in this response appear in the reference?"
           scored 0-10 against the policy doc / order record / prior tickets —
           never against the agent's own text
```

| Ticket | Type | Mean | Runs | Spread |
|---|---|---:|---|---:|
| T01 | order_status | 10.00 | 10, 10, 10 | 0 |
| T02 | order_status | 4.00 | 4, 4, 4 | 0 |
| T03 | order_status | 10.00 | 10, 10, 10 | 0 |
| T04 | refund_request | 10.00 | 10, 10, 10 | 0 |
| T05 | refund_request | 6.00 | 4, 10, 4 | **6** |
| T06 | delivery_issue | 10.00 | 10, 10, 10 | 0 |
| T07 | delivery_issue | **0.00** | 0, 0, 0 | 0 |
| T08 | delivery_issue | 1.33 | 0, 4, 0 | **4** |
| T09 | subscription_account | 10.00 | 10, 10, 10 | 0 |
| T10 | subscription_account | 3.67 | 0, 4, 7 | **7** |
| T11 | subscription_account | 3.67 | 4, 0, 7 | **7** |
| T12 | order_status | 10.00 | 10, 10, 10 | 0 |
| T13 | refund_request | 10.00 | 10, 10, 10 | 0 |

**Mean grounding score: 6.82 / 10.** Lowest: T07 at 0.00.

## Why every input is scored three times (§6)

§6's second pitfall is trusting a single judge run as a verdict. That isn't
hypothetical here — **4 of 13 tickets varied by ≥3 points across three identical
calls**, and T10 and T11 both swung a full 7 points (0 → 7). A single run would
have reported T10 as either a clean failure or a near-pass depending purely on
which call happened to land.

The spread is printed next to every mean for this reason. A 6.00 from
{4, 10, 4} is a different finding from a 6.00 from {6, 6, 6}, and reporting only
the mean would erase the distinction.

## What the judge caught that the trajectory eval cannot

**T07 — a real hallucination, 0.00 across all three runs.**

The agent's answer:

> order delivered on 2026-07-18 ... the order was 9 days later than the original
> estimated arrival date

The actual record:

```python
'ord_7004': {'customer_id': 'cust_3003', 'item': 'Mechanical Keyboard',
             'status': 'delayed', 'delivery_date': '2026-07-18'}
```

The order status is **`delayed`**, not delivered. `2026-07-18` is the *estimated*
delivery date, and the agent reported it as the date the order *arrived*. It also
invented the "9 days late" figure and a severe-weather assessment that appears
nowhere in the record.

**The trajectory eval passes T07.** Its required step is
`retrieval:shipping_delays`, that retrieval happened, superset satisfied. The
path was right and the claim built on it was still false — which is precisely
the gap §2.3 says a second scoring method exists to cover.

**T02, T08, T10 — ungrounded date arithmetic.** The agent asserts *"today is
2026-08-08"* and computes claim windows from it. It has no clock and no
system-date tool; the current date is nowhere in its context. The claim happens
to be right, which is worse than if it were wrong — it reads authoritative and
is unverifiable from anything the agent was given.

## Two reference bugs this run exposed (and how they were fixed)

Worth recording because both failed in the flattering direction — they made the
agent look like it was fabricating when it wasn't, and a judge that cries wolf
gets ignored exactly when it's right.

1. **T06 scored 0.00 for being correct.** It cited prior-ticket history
   (`tk_022`, 2026-07-16, delay compensation discussed) that the agent is
   genuinely given in its system prompt, but which the reference omitted.
2. **T09 scored 0.00 for accurately quoting a policy doc.** "14 days",
   "3 business days", the payment-dispute exclusion — all verbatim from
   `account_suspension_appeal.md`, which the retriever had handed it. The
   reference only contained `cust_4004: standing=suspended`.

`resolve_reference()` now assembles ground truth from all three things the agent
can actually see: the ticket's record, **the policy docs the retriever returned
on this run** (read off `Ticket.steps`, so it reflects reality rather than what
the ticket author predicted), and the customer's prior tickets. After the fix
T06 and T09 both score 10.00 — the same responses, correctly recognised as
grounded.

## Reproduce

```bash
python -m eval.judge --provider groq
```

Scores will not reproduce exactly — that is the finding, not a defect. Use
`--runs 5` for a tighter mean.
