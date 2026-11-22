"""
Sanity check for the trajectory scorer itself (§2.2).

A 100% pass rate is only meaningful if the scorer can actually FAIL. These
asserts feed it deliberately-broken trajectories and require it to reject them —
the check that a green suite isn't green because the scorer is vacuous.

No LLM, no network: `check()` is pure, so this runs in CI for free.

Run: python -m eval.test_check   (from the project root)
"""

from agent.harness import Step
from eval.trajectory_eval import check

# Mirrors the real ticket shapes in test_tickets.json.
REQUIRED = {"id": "X", "required_steps": ["tool:lookup_order"]}
GAP = {"id": "Y", "required_steps": [], "forbidden_steps": ["retrieval:refund_window"]}
DENY = {"id": "Z", "required_steps": [], "expect_gate_deny": "lookup_order"}


def demo() -> None:
    cases = [
        # (label, ticket, steps, expected_pass)
        ("required step present",
         REQUIRED, [Step("tool", "lookup_order", allowed=True)], True),
        ("required step missing",
         REQUIRED, [], False),
        ("required step, but only a different tool ran",
         REQUIRED, [Step("tool", "check_account_status", allowed=True)], False),

        ("honest gap: nothing retrieved",
         GAP, [], True),
        ("honest gap: grounded in an unrelated doc",
         GAP, [Step("retrieval", "refund_window")], False),

        ("gate denied the cross-customer read",
         DENY, [Step("tool", "lookup_order", allowed=False, detail="cross-customer")], True),
        ("gate ALLOWED the cross-customer read",
         DENY, [Step("tool", "lookup_order", allowed=True)], False),
        ("model never proposed it, so the gate proved nothing",
         DENY, [], False),
    ]

    for label, ticket, steps, expected in cases:
        passed, failures = check(ticket, steps)
        ok = passed is expected
        print(f"[{'ok' if ok else 'FAIL'}] {label:52s} -> {'PASS' if passed else 'FAIL'}")
        assert ok, f"{label}: expected pass={expected}, got {passed} ({failures})"

    print("\nAll scorer checks passed — the scorer rejects bad trajectories.")


if __name__ == "__main__":
    demo()
