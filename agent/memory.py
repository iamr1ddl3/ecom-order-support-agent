"""
Memory for a support conversation — the two halves of §2.4.

- Short-term: the message buffer for the current ticket. It IS the `messages`
  list the provider threads turn to turn, so "retains earlier turns" is not a
  separate feature — it's the same object the harness appends to each turn.
- Long-term: a lookup into the mock prior-ticket store (data/prior_tickets.py),
  keyed by the ticket's customer ID. Injected into the system prompt so the model
  can answer history-dependent questions.

Both are scoped to one customer: a Memory is created for one ticket's customer and
never reaches across to another's history — the same boundary the MCP tools enforce.
"""

from __future__ import annotations

from data.prior_tickets import get_prior_tickets


class Memory:
    def __init__(self, customer_id: str):
        self.customer_id = customer_id
        self.messages: list[dict] = []  # short-term buffer (provider message list)

    # --- short-term ---------------------------------------------------------
    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def history(self) -> list[dict]:
        return self.messages

    # --- long-term ----------------------------------------------------------
    def prior_tickets_summary(self) -> str:
        """One-line-per-ticket summary of this customer's history, for the system
        prompt. Empty string if the customer has no prior tickets."""
        tickets = get_prior_tickets(self.customer_id)
        if not tickets:
            return ""
        lines = [f"- [{t['date']}] ({t['type']}) {t['summary']}" for t in tickets]
        return "\n".join(lines)
