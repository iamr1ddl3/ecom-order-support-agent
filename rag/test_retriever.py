"""
Retriever sanity check — the §7 "guess which doc, confirm the retriever returns
it, BEFORE wiring an LLM" step, as runnable asserts.

Guards two things that break silently: (1) each real question retrieves its
expected doc, (2) the uncovered question (customs fees) clears NOTHING, so the
honest-gap path actually fires. If you change the doc set or min_score, this is
what tells you the coverage threshold still holds.

Run: python -m rag.test_retriever   (from the project root)
"""

from rag.retriever import Retriever

# (question, expected top doc_id) — the "guess" half of the §7 sanity check.
EXPECTED = [
    ("How many days after delivery can I request a refund?", "refund_window"),
    ("My order arrived 9 days late, do I get the shipping fee back?", "shipping_delays"),
    ("How do I appeal a suspended account?", "account_suspension_appeal"),
    ("Can I cancel my subscription and get a refund?", "subscription_cancellation"),
    ("My package arrived damaged, what do I do?", "damaged_items"),
    ("Is a final-sale item eligible to be returned?", "return_eligibility"),
]

# Plausible but out-of-scope — must return an empty result (honest gap).
UNCOVERED = "If my order is stuck in customs and I owe an international customs fee, will you cover it?"


def demo() -> None:
    r = Retriever()
    print(f"Loaded {len(r.docs)} policy docs: {list(r.docs)}\n")

    for question, expected in EXPECTED:
        hits = r.retrieve(question, k=1)
        got = hits[0][0] if hits else "(none)"
        score = hits[0][1] if hits else 0.0
        ok = got == expected
        print(f"[{'ok' if ok else 'FAIL'}] {expected:26s} <- got {got} ({score:.2f})")
        assert ok, f"expected '{expected}' for {question!r}, got '{got}'"

    gap_hits = r.retrieve(UNCOVERED, k=2)
    print(f"\n[{'ok' if not gap_hits else 'FAIL'}] coverage gap (customs)   <- got {[h[0] for h in gap_hits] or '(none, correct)'}")
    assert not gap_hits, f"customs question should clear nothing, got {[h[0] for h in gap_hits]}"

    print("\nAll retriever sanity checks passed.")


if __name__ == "__main__":
    demo()
