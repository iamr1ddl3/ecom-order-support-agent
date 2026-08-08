# The confident wrong path (§2.2)

A real case, found by reading trajectories from an actual eval run — not a
fixture written to satisfy the requirement. It was not the ticket I expected to
find it in.

## The case: ticket T03

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

Deliberately left unfixed for now: **the pgvector migration (§2.6.2) replaces
this scorer entirely.** Cosine similarity over embeddings does not have BM25's
single-rare-term failure mode, and the threshold is being recalibrated from
scratch there. Fixing the BM25 tokenizer would be fixing a component that is
about to be deleted. The re-calibration in Phase 4 must confirm this specific
query no longer pulls `subscription_cancellation` — that is the regression test
this finding leaves behind.

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
