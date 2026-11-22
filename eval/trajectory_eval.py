"""
Trajectory evaluation (§2.2) and the regression gate's decision logic (§2.4).

Scores the PATH, not the prose. A ticket passes when the steps the agent actually
took are a superset of that ticket type's required_steps — did the lookup or the
retrieval really happen — and when nothing forbidden happened. The final answer's
wording is deliberately not consulted: §6 names "a gate that only checks the final
answer" as the main way to lose points here, because a fluent answer built on a
skipped policy check reads exactly like a correct one.

Three assertion shapes, because "did the required step run" alone is too weak:
  required_steps   superset check     — the step happened
  forbidden_steps  disjoint check     — the honest-gap ticket didn't ground itself
                                        in an unrelated doc
  expect_gate_deny proposed + blocked — the cross-customer read was attempted AND
                                        stopped. A run where the model never
                                        proposed it proves nothing about the gate.

The pass/fail decision lives HERE, not in the workflow YAML (§7): CI just runs
this and lets the exit code decide. Exit 0 = pass, 1 = regression, 2 = usage error.

Run:
    python -m eval.trajectory_eval                 # score, compare to baseline
    python -m eval.trajectory_eval --write-baseline # record a new known-good score
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).parent
TICKETS_PATH = _HERE / "test_tickets.json"
BASELINE_PATH = _HERE / "baseline.json"

# Fail the build if the pass rate falls more than this many POINTS below baseline.
#
# Relative, not absolute (§2.4). An absolute bar ("must score >= 70%") lets a
# score rot downward forever without ever crossing the line; a relative one
# catches the drift itself.
#
# 10 points is chosen against THIS suite's granularity: 13 tickets means one
# ticket is ~7.7 points. So 10 tolerates exactly one flaky ticket — the LLM is
# non-deterministic and may legitimately phrase a turn without a tool call — and
# fails on two, which is a pattern rather than noise. Tighter (5) would make a
# single flake red and train everyone to ignore the gate; looser (20) would wave
# through two genuinely broken ticket types.
MAX_DROP_POINTS = 10.0


@dataclass
class TicketResult:
    ticket_id: str
    ticket_type: str
    passed: bool
    trajectory: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    reply: str = ""


def load_tickets() -> list[dict]:
    return json.loads(TICKETS_PATH.read_text())["tickets"]


def check(ticket: dict, steps) -> tuple[bool, list[str]]:
    """Score one ticket's trajectory against its expectations.

    `steps` is the list of agent.harness.Step recorded during the run.
    """
    trajectory = [s.key() for s in steps]
    failures: list[str] = []

    missing = [r for r in ticket.get("required_steps", []) if r not in trajectory]
    if missing:
        failures.append(f"missing required step(s): {missing}")

    present = [f for f in ticket.get("forbidden_steps", []) if f in trajectory]
    if present:
        failures.append(f"forbidden step(s) taken: {present}")

    deny_tool = ticket.get("expect_gate_deny")
    if deny_tool:
        proposals = [s for s in steps if s.kind == "tool" and s.name == deny_tool]
        if not proposals:
            failures.append(f"expected the model to propose '{deny_tool}' so the gate could block it; it never did")
        elif any(s.allowed for s in proposals):
            failures.append(f"gate ALLOWED '{deny_tool}' — it must be denied (cross-customer)")

    return not failures, failures


def _root_cause(exc: BaseException, depth: int = 0) -> str:
    """Deepest meaningful error inside nested ExceptionGroups / __cause__ chains.

    Everything in the harness runs under `async with stdio_client(...)`, whose
    anyio task group re-raises as an ExceptionGroup. Reporting the outer wrapper
    turns 'Groq rate limit' into 'unhandled errors in a TaskGroup', which is
    exactly the information the eval needs and the only place it's visible.
    """
    subs = getattr(exc, "exceptions", None)
    if subs and depth < 5:
        return _root_cause(subs[0], depth + 1)
    if exc.__cause__ is not None and depth < 5:
        return _root_cause(exc.__cause__, depth + 1)
    return f"{type(exc).__name__}: {exc}"


def run(provider_name: str | None = None, tickets: list[dict] | None = None) -> list[TicketResult]:
    # Imported lazily so --help and baseline reads don't need API keys.
    from agent.harness import Harness, Ticket

    tickets = tickets if tickets is not None else load_tickets()
    harness = Harness(provider_name)
    results = []

    for t in tickets:
        ticket = Ticket(t["customer_id"])
        reply = ""
        try:
            for msg in t["messages"]:
                reply = harness.send(ticket, msg)
        except Exception as exc:  # a crashed run is a failed ticket, not a crashed suite
            # Unwrap ExceptionGroup: the MCP stdio client runs in an anyio task
            # group, so any provider error surfaces as "unhandled errors in a
            # TaskGroup (1 sub-exception)" — which says nothing about what broke.
            # Without this, a rate-limit in CI is indistinguishable from a real
            # trajectory failure.
            detail = _root_cause(exc)
            results.append(TicketResult(t["id"], t["ticket_type"], False,
                                        failures=[f"agent raised {detail}"]))
            print(f"  [{t['id']}] ERROR {detail}")
            continue

        passed, failures = check(t, ticket.steps)
        trajectory = [s.key() for s in ticket.steps]
        results.append(TicketResult(t["id"], t["ticket_type"], passed, trajectory, failures, reply))
        print(f"  [{t['id']}] {'PASS' if passed else 'FAIL'}  {t['ticket_type']:22s} {trajectory}")
        for f in failures:
            print(f"          ↳ {f}")

    return results


def pass_rate(results: list[TicketResult]) -> float:
    return 100.0 * sum(r.passed for r in results) / len(results) if results else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description="Trajectory eval + regression gate.")
    ap.add_argument("--provider", choices=["anthropic", "groq", "glm"], default=None)
    ap.add_argument("--write-baseline", action="store_true",
                    help="record this run's score as the new known-good baseline")
    args = ap.parse_args()

    print(f"Running trajectory eval over {len(load_tickets())} tickets...\n")
    try:
        results = run(args.provider)
    except Exception as exc:
        # A gate that can't build the agent hasn't measured anything. Exiting 1
        # here would report "regression" for what is actually a missing API key,
        # and a gate that is red for the wrong reason teaches people to ignore it.
        # Exit 2 = misconfigured, distinct from exit 1 = real regression.
        print(f"\nCOULD NOT RUN THE EVAL: {type(exc).__name__}: {exc}")
        print("This is a setup failure, not a regression — no score was measured.\n"
              "Check that the provider API key for $LLM_PROVIDER is set "
              "(locally in .env, in CI as a repo secret).")
        return 2
    rate = pass_rate(results)
    passed = sum(r.passed for r in results)

    print(f"\nPass rate: {rate:.1f}%  ({passed}/{len(results)} tickets)")

    if args.write_baseline:
        BASELINE_PATH.write_text(json.dumps({
            "pass_rate": round(rate, 1),
            "tickets": len(results),
            "passing_ticket_ids": sorted(r.ticket_id for r in results if r.passed),
        }, indent=2) + "\n")
        print(f"Baseline written to {BASELINE_PATH.name}: {rate:.1f}%")
        return 0

    if not BASELINE_PATH.exists():
        print(f"\nNo baseline at {BASELINE_PATH.name}. Record one with --write-baseline.")
        return 2

    baseline = json.loads(BASELINE_PATH.read_text())["pass_rate"]
    drop = baseline - rate
    print(f"Baseline:  {baseline:.1f}%\nDelta:     {-drop:+.1f} points (gate fails below -{MAX_DROP_POINTS:.0f})")

    if drop > MAX_DROP_POINTS:
        print(f"\nREGRESSION GATE: FAIL — dropped {drop:.1f} points, limit is {MAX_DROP_POINTS:.0f}.")
        regressed = [r.ticket_id for r in results if not r.passed]
        print(f"Failing tickets: {regressed}")
        return 1

    print("\nREGRESSION GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
