"""
LLM-as-judge (§2.3) — the second scoring method, for what a rule can't check.

The trajectory eval proves the right STEPS ran. It cannot tell you whether the
answer built on those steps is actually supported by what came back, or whether
the agent quietly invented a delivery date that reads perfectly. That's this.

Two things the assignment is specific about, and both change the result:

1. **A fact-checking rubric, not a vague one.** "Is this a good response?" scores
   fluency, and a confidently fabricated detail is fluent. The rubric here asks
   the only question that catches invention: does every specific claim in this
   response appear in the reference? The reference is the actual policy doc or
   order record — never the agent's own text, which would just ask the model to
   agree with itself.

2. **Repeated runs, averaged.** §6's second pitfall is trusting a single judge
   run as a verdict; judge scores are non-deterministic. Each input is scored
   RUNS times and averaged, and the spread is reported alongside the mean —
   a 7.0 from {7,7,7} and a 7.0 from {3,8,10} are not the same finding, and
   only printing the mean would hide that.

The judge reuses the agent's own provider (§7): a judge is just another prompt.

Run: python -m eval.judge            (all judgeable tickets)
     python -m eval.judge --runs 5
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

from eval.trajectory_eval import load_tickets
from rag.retriever import load_policy_docs

RUNS = 3  # §6: never report a single judge run as a verdict.

_RUBRIC = """You are grading a customer-support agent's response for factual grounding.

Score 0-10 on ONE criterion: does every specific claim in the RESPONSE appear in,
or follow directly from, the REFERENCE?

A specific claim is a checkable detail — a date, a status, a number of days, a
monetary amount, an account standing, an eligibility rule.

  10  every specific claim is supported by the reference
   7  all claims supported, but adds unsupported vague reassurance
   4  one specific claim is not in the reference
   0  multiple invented specifics, or a claim contradicting the reference

Judge grounding ONLY. Do not reward tone, helpfulness, or writing quality. A
terse, well-grounded answer scores higher than a warm one that invents a date.
An answer that correctly says no policy covers the question, and invents nothing,
scores 10.

QUESTION:
{question}

REFERENCE (the ground truth — the agent's answer is NOT evidence for itself):
{reference}

RESPONSE:
{response}

Reply with JSON only: {{"score": <0-10>, "unsupported_claims": ["..."], "reason": "<one sentence>"}}"""


def resolve_reference(ticket: dict) -> str:
    """Turn a ticket's `reference` field into the actual ground-truth text.

    'policy_doc:<id>' expands to the doc body; anything else is already a literal
    fact string (an order or account record).

    The customer's prior tickets are always appended. The agent is given that
    history in its system prompt, so a response citing it is grounded, not
    invented — but a reference that omitted it would score those citations as
    fabrications. That is a broken reference, not a caught hallucination, and it
    would make the judge's numbers worthless in the direction that looks most
    impressive. Found by T06 scoring 0.00 for correctly recalling ticket tk_022.
    """
    from data.prior_tickets import get_prior_tickets

    ref = ticket.get("reference", "")
    if ref.startswith("policy_doc:"):
        doc_id = ref.split(":", 1)[1]
        ref = load_policy_docs().get(doc_id, f"(no such policy doc: {doc_id})")

    prior = get_prior_tickets(ticket["customer_id"])
    if prior:
        history = "\n".join(f"- {p['date']} ({p['type']}): {p['summary']}" for p in prior)
        ref += f"\n\nThis customer's prior support tickets (the agent can see these):\n{history}"
    return ref


def _parse(text: str) -> dict | None:
    """Pull the JSON verdict out of the judge's reply, tolerating code fences."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def judge_once(provider, question: str, reference: str, response: str,
               attempts: int = 3) -> dict | None:
    """One judge verdict, retrying transient API failures.

    Without the retry a dropped connection silently costs a sample, and the run
    reports a mean over fewer runs than requested while claiming the configured
    number — which is the §6 pitfall wearing a disguise. Returns None only when
    every attempt failed; callers must report the real sample count.
    """
    prompt = _RUBRIC.format(question=question, reference=reference, response=response)
    for attempt in range(attempts):
        try:
            # No tools: the judge reads and scores, it does not act.
            result = provider.create("You are a strict grading assistant. Reply with JSON only.",
                                     [{"role": "user", "content": prompt}], [])
            verdict = _parse(result.text)
            if verdict is not None and isinstance(verdict.get("score"), (int, float)):
                return verdict
        except Exception as exc:
            if attempt == attempts - 1:
                print(f"          ↳ judge call failed after {attempts} attempts: "
                      f"{type(exc).__name__}: {exc}")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM-as-judge over agent responses.")
    ap.add_argument("--provider", choices=["anthropic", "groq", "glm"], default=None)
    ap.add_argument("--runs", type=int, default=RUNS,
                    help=f"judge calls per ticket, averaged (default {RUNS})")
    ap.add_argument("--limit", type=int, default=None, help="only judge the first N tickets")
    args = ap.parse_args()

    from agent.harness import Harness, Ticket

    # Only tickets with a concrete reference can be fact-checked.
    tickets = [t for t in load_tickets() if t.get("reference")]
    if args.limit:
        tickets = tickets[: args.limit]

    harness = Harness(args.provider)
    provider = harness.provider

    print(f"Judging {len(tickets)} tickets, {args.runs} runs each "
          f"(rubric: is every specific claim supported by the reference?)\n")

    rows = []
    for t in tickets:
        ticket = Ticket(t["customer_id"])
        reply = ""
        for msg in t["messages"]:
            reply = harness.send(ticket, msg)

        question = t["messages"][-1]
        reference = resolve_reference(t)

        scores, verdicts = [], []
        for _ in range(args.runs):
            v = judge_once(provider, question, reference, reply)
            if v:
                scores.append(float(v["score"]))
                verdicts.append(v)

        if not scores:
            print(f"  [{t['id']}] judge returned no parseable score in {args.runs} runs")
            continue

        mean = statistics.mean(scores)
        spread = max(scores) - min(scores)
        rows.append((t["id"], t["ticket_type"], mean, spread, scores, verdicts))

        # Say so when a mean rests on fewer samples than asked for. A 10.00 from
        # one surviving call is not the same evidence as a 10.00 from three, and
        # printing them identically is how a single run becomes a "verdict".
        short = f"  <-- only {len(scores)}/{args.runs} runs succeeded" if len(scores) < args.runs else ""
        flag = "  <-- judge disagrees with itself" if spread >= 3 else ""
        print(f"  [{t['id']}] {t['ticket_type']:22s} mean {mean:5.2f}/10  "
              f"runs={[f'{s:g}' for s in scores]}  spread={spread:g}{flag}{short}")
        worst = min(verdicts, key=lambda v: v.get("score", 10))
        if worst.get("unsupported_claims"):
            print(f"          ↳ flagged: {worst['unsupported_claims']}")

    if not rows:
        print("\nNo scores collected.")
        return 1

    means = [r[2] for r in rows]
    print(f"\nJudged {len(rows)} tickets x {args.runs} runs = {len(rows) * args.runs} judge calls")
    print(f"Mean grounding score: {statistics.mean(means):.2f}/10  "
          f"(lowest {min(means):.2f} on {min(rows, key=lambda r: r[2])[0]})")

    noisy = [r for r in rows if r[3] >= 3]
    if noisy:
        print(f"Non-determinism: {len(noisy)} ticket(s) varied by >=3 points across runs "
              f"— {[r[0] for r in noisy]}. This is why a single run is not a verdict (§6).")
    else:
        print(f"Non-determinism: no ticket varied by >=3 points across {args.runs} runs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
