"""
The before/after report (§2.5).

Runs the SAME eval suite against two versions of the agent and prints both pass
rates plus exactly which tickets changed status. §2.5 is explicit that the output
has to be a specific number — "83% to 50%, and here is which tickets flipped" —
not "the regressed version performs worse".

The regressed version is selected by `AGENT_REGRESSION`, read in
agent/tools_schema.py. Two flavours, both of which break the PATH while leaving
the prose plausible (§2.4's own examples):

  drop_lookup_order   removes the lookup_order tool. The model can no longer
                      check an order, but it still answers — fluently, from the
                      question's own wording. This is the case that proves the
                      gate isn't just reading the final text.
  no_retrieval        disables policy retrieval. Answers become ungrounded
                      recollection instead of quoted policy.

Why an env var rather than two git refs: the report stays one command with no
checkout dance, and CI can flip it without a detached HEAD. The regression is
real code taking a real different path, not a mocked score.

Run: python -m eval.before_after
     python -m eval.before_after --regression no_retrieval
"""

from __future__ import annotations

import argparse
import os
import sys

from eval.trajectory_eval import load_tickets, pass_rate, run

REGRESSIONS = ("drop_lookup_order", "no_retrieval")


def _run_variant(label: str, regression: str | None, provider: str | None):
    """Run the suite with AGENT_REGRESSION set (or cleared), in-process.

    The env var is read at Harness construction, and `run()` builds a fresh
    Harness, so setting it here is enough — no subprocess needed.
    """
    if regression:
        os.environ["AGENT_REGRESSION"] = regression
    else:
        os.environ.pop("AGENT_REGRESSION", None)

    print(f"\n{'=' * 72}\n{label}\n{'=' * 72}")
    results = run(provider)
    return {r.ticket_id: r for r in results}


def main() -> int:
    ap = argparse.ArgumentParser(description="Before/after eval comparison (§2.5).")
    ap.add_argument("--provider", choices=["anthropic", "groq", "glm"], default=None)
    ap.add_argument("--regression", choices=REGRESSIONS, default="drop_lookup_order")
    args = ap.parse_args()

    total = len(load_tickets())
    before = _run_variant("BEFORE — clean baseline agent", None, args.provider)
    after = _run_variant(f"AFTER — regressed agent (AGENT_REGRESSION={args.regression})",
                         args.regression, args.provider)
    os.environ.pop("AGENT_REGRESSION", None)

    before_rate = pass_rate(list(before.values()))
    after_rate = pass_rate(list(after.values()))

    broke = sorted(tid for tid in before if before[tid].passed and not after[tid].passed)
    fixed = sorted(tid for tid in before if not before[tid].passed and after[tid].passed)

    print(f"\n{'=' * 72}\nBEFORE / AFTER REPORT\n{'=' * 72}")
    print(f"Regression applied : {args.regression}")
    print(f"Tickets            : {total}")
    print(f"Before             : {before_rate:.1f}%  ({sum(r.passed for r in before.values())}/{total})")
    print(f"After              : {after_rate:.1f}%  ({sum(r.passed for r in after.values())}/{total})")
    print(f"Change             : {after_rate - before_rate:+.1f} points")

    print(f"\n{before_rate:.0f}% to {after_rate:.0f}%"
          + (f", and here are the tickets that flipped to failing: {broke}" if broke
             else ", with no tickets flipping to failing"))

    if fixed:
        # Surprising, and worth surfacing rather than hiding: it usually means a
        # ticket's expectations were satisfiable without the removed capability.
        print(f"Flipped to PASSING (unexpected, check the ticket's expectations): {fixed}")

    for tid in broke:
        print(f"\n  {tid} ({before[tid].ticket_type})")
        print(f"     before: {before[tid].trajectory}")
        print(f"     after : {after[tid].trajectory}")
        for f in after[tid].failures:
            print(f"     why   : {f}")
        # The point of §2.5: the answer often still READS fine.
        snippet = " ".join(after[tid].reply.split())[:160]
        if snippet:
            print(f"     answer: \"{snippet}...\"")

    return 0


if __name__ == "__main__":
    sys.exit(main())
