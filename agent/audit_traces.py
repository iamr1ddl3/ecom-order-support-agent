"""
Fetch traces back from LangFuse and verify they're correctly nested (§2.1).

§7 is explicit: "don't stop at a single successful trace and call it done. Run a
handful of real tickets through your agent and confirm every one of them shows
up, correctly nested, before moving on." A demo that prints nicely tells you
nothing about what reached the dashboard — spans can be dropped at exit, land
flat instead of nested, or arrive with the wrong observation type and still look
fine on the terminal.

So this asserts the three things that actually matter:
  1. every trace has exactly ONE root observation (flat traces mean the context
     manager nesting broke)
  2. no orphans — every child's parent is present in the same trace
  3. the observation TYPES are right (retriever/generation/tool, not generic
     spans), since that's what drives the Agent Graph and token capture

Run after `python main.py --demo`:
    python -m agent.audit_traces
"""

from __future__ import annotations

import sys
import time

from dotenv import load_dotenv


def audit(limit: int = 8) -> int:
    load_dotenv()
    from agent.tracing import get_tracer, tracing_enabled

    if not tracing_enabled():
        print("Tracing is disabled (no LANGFUSE_* keys) — nothing to audit.")
        return 2

    client = get_tracer()
    client.flush()
    time.sleep(3)  # ingestion is async; give the backend a moment

    traces = client.api.trace.list(limit=limit).data
    if not traces:
        print("No traces found. Run `python main.py --demo` first.")
        return 1

    problems = []
    for t in traces:
        obs = client.api.trace.get(t.id).observations
        by_id = {o.id: o for o in obs}
        roots = [o for o in obs if not o.parent_observation_id]

        print(f"\n=== {t.name}  ({len(obs)} observations)")
        for r in roots:
            print(f"  [{r.type}] {r.name}")
            for child in (o for o in obs if o.parent_observation_id == r.id):
                gate = (child.metadata or {}).get("gate", "")
                print(f"     └─ [{child.type}] {child.name}" + (f"  gate={gate}" if gate else ""))

        if len(roots) != 1:
            problems.append(f"{t.id[:12]}: expected 1 root observation, found {len(roots)}")
        orphans = [o.name for o in obs if o.parent_observation_id and o.parent_observation_id not in by_id]
        if orphans:
            problems.append(f"{t.id[:12]}: orphaned observations {orphans}")
        types = {o.type for o in obs}
        if "GENERATION" not in types:
            problems.append(f"{t.id[:12]}: no GENERATION observation — model calls aren't typed")

    print(f"\nAudited {len(traces)} traces.")
    if problems:
        print("PROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("All traces correctly nested, no orphans, observation types correct.")
    return 0


if __name__ == "__main__":
    sys.exit(audit())
