# The confident wrong path (§2.2)

Two real cases, both found by reading output from actual eval runs rather than
written to satisfy the requirement. Neither was the ticket expected to produce
one.

They fail in different ways, which is why both are here: T03 is a *contaminated
path with a correct answer*, T07 is a *correct path with a false answer*. A gate
that only checks one of trajectory or final text misses one of them, and the
suite as committed passes both.

---

## Case 1: ticket T03

**Question** (a plain order-status question, no policy content at all):

> Vikram here (cust_4004). Did my standing desk ord_8005 ever arrive?

**Final answer:** correct. The agent calls `lookup_order(ord_8005)`, gets the
real record, and reports that the Standing Desk was delivered on 2026-07-10.
Read on its own, this response is clean — right tool, right order, right date,
nothing invented. It passes a final-answer review without a second look.

**Trajectory:**

```
retrieval:subscription_cancellation  (score=1.47)
tool:lookup_order                    (ALLOW)
```

The first step should not be there. Before the agent ever looked anything up,
the **subscription cancellation policy** was retrieved and injected into its
system prompt as authoritative "Retrieved policy context" for a question about a
desk delivery.

## Why it happened

BM25 matched on exactly one term:

```
query terms: ['vikram','here','cust','4004','did','standing','desk','ord','8005','ever','arrive']
matched in subscription_cancellation: 'ever'   tf=1  idf=1.54  -> score 1.47
```

The word is **"ever"** — from the policy's phrase *"first-**ever** subscription
charge"*. An incidental adverb, carrying no topical signal, scoring 1.47 against
a `min_score` of 1.0. `retriever.py` already drops domain boilerplate
(`order`, `fee`, `item`) as stopwords for precisely this reason; `ever` slipped
through because it doesn't look like e-commerce vocabulary.

## Why the final answer alone would have missed it

Nothing about the response reveals the contamination. The retrieved text was
irrelevant rather than contradictory, so the model correctly ignored it and
answered from the tool result. The failure is **latent**: this run was fine
because the wrong doc happened not to conflict with the question.

Change one variable and it isn't. Had the ticket been "did my standing desk ever
arrive, and can I get my money back?", the agent would have had genuine refund
policy from an unrelated product line — *"no partial-month refunds"*, *"14 days"*
— sitting in its prompt labelled as the policy that governs this customer. A
fluent, confident, wrong answer, with a final-answer review seeing nothing wrong.

That is the whole point of §2.2: **the answer being right this time is not
evidence the path was right.**

## What this says about our own gate (Defensible #2, §3)

The uncomfortable part. **The trajectory gate passes T03.** Its `required_steps`
are `["tool:lookup_order"]`, that step ran, superset check satisfied — PASS.

So the gate catches a *missing* required step but not a *spurious extra* one. §6
warns about rebuilding the final-answer blind spot one layer up; this is a
narrower version of the same mistake living inside a trajectory check.

We did not paper over it by adding `subscription_cancellation` to T03's
`forbidden_steps`. That would turn a green suite back on by hard-coding the one
instance we happened to find, and the next incidental term would sail through
just the same. What the eval set does instead is assert the *general* property
where it's checkable: T13's `forbidden_steps` requires that the honest-gap
ticket ground itself in **no** policy doc at all.

The real fix belongs in retrieval, not in the eval:

- add `ever` (and similar incidental adverbs) to `_STOPWORDS`, or
- require ≥2 distinct matched terms before a doc clears the threshold, since a
  single common-word hit is close to meaningless on a 6-doc corpus.

Deliberately not patched in BM25: **the pgvector migration (§2.6.2) replaces
this scorer entirely.** Cosine similarity over embeddings has no
single-rare-term failure mode, so fixing the BM25 tokenizer would mean fixing a
component being replaced in the same PR.

### Confirmed fixed by pgvector

The prediction above was checked rather than assumed, on the same query:

```
bm25    : [('subscription_cancellation', 1.47)]
pgvector: []
```

Dense retrieval correctly finds **nothing** relevant to a desk-delivery question
in a 6-doc policy corpus, so no spurious context reaches the prompt. The
single-word coincidence that produced the contamination doesn't survive
embedding — "ever" carries no semantic weight next to "standing desk arrive".

Note this makes the deployed agent (pgvector) strictly better on this case than
the CI gate's agent (bm25, no database in CI). That gap is deliberate and
documented in the README: the gate scores trajectory shape, which is
backend-independent, and both backends pass `rag/test_retriever.py`.

## Reproduce

```bash
python -c "
from rag.retriever import Retriever, _tokenize
r = Retriever()
q = 'Vikram here (cust_4004). Did my standing desk ord_8005 ever arrive?'
print('retrieved:', [(d, round(s,2)) for d,s,_ in r.retrieve(q, k=3)])
print('matched term:', [t for t in _tokenize(q) if r._tf['subscription_cancellation'].get(t)])
"
```

Expected output:

```
retrieved: [('subscription_cancellation', 1.47)]
matched term: ['ever']
```

---

## Case 2: ticket T07 — the trajectory is right and the answer is false

Found by the LLM-as-judge (§2.3), which scored it **0.00 on all three runs**.
This is the mirror image of T03 and the more dangerous of the two.

**Question:** "My order arrived 9 days late. Do I get the shipping fee back?"

**Trajectory:** `retrieval:shipping_delays` — exactly the required step. Right
doc, right path, nothing spurious.

**The answer:**

> order delivered on 2026-07-18 ... the order was 9 days later than the original
> estimated arrival date ... no indication the delay was caused by severe weather

**The actual record:**

```python
'ord_7004': {'customer_id': 'cust_3003', 'item': 'Mechanical Keyboard',
             'status': 'delayed', 'delivery_date': '2026-07-18'}
```

The status is **`delayed`** — the order has not arrived. `2026-07-18` is the
*estimated* delivery date, and the agent reported it as the date the package
*was delivered*. It then derived "9 days late" from that misreading and issued a
severe-weather ruling on evidence that does not exist.

A customer reads a specific date and a specific entitlement decision. Both are
manufactured from a field the agent misinterpreted.

### Why the trajectory eval cannot catch this

T07's `required_steps` is `["retrieval:shipping_delays"]`. That retrieval
happened. Superset satisfied. **PASS.**

There is no version of a path check that catches this, because the path is
correct. The agent did everything right and then misread one field. This is the
exact division of labour §2.3 describes: a rule verifies that the right steps
ran; only a judge reading the response against the record can see that the claim
built on those steps is false.

### Together, T03 and T07 are the argument for both scorers

| | T03 | T07 |
|---|---|---|
| Trajectory | contaminated (spurious doc) | correct |
| Final answer | correct | **false** |
| Trajectory eval | PASS | PASS |
| LLM judge | 10.00 | **0.00** |

Neither scorer catches both. The trajectory eval is the CI gate because it is
cheap, deterministic, and blocks the regression class §2.4 asks about; the judge
is not in the gate because a non-deterministic score that swings 7 points
between identical runs (see JUDGE_RESULTS.md) cannot be a build-breaking
threshold. This is Defensible #2 in concrete form: **what the gate checks is the
path, and T07 is precisely what that choice cannot catch.**
