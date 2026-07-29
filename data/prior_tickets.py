"""
Mock long-term memory: prior support tickets, keyed by customer ID.

This is the "long-term" half of §2.4 — history that predates the current
conversation. The agent looks a customer up here to answer questions that depend
on their past ("have I contacted you about this before?", "what happened with my
last appeal?"), which the short-term buffer alone can't cover.
"""

# customer_id -> list of past tickets (most recent last)
PRIOR_TICKETS = {
    "cust_1001": [
        {"ticket_id": "tk_001", "date": "2026-06-02", "type": "order_status",
         "summary": "Asked about ord_5002 delivery; resolved, delivered on time."},
        {"ticket_id": "tk_014", "date": "2026-07-05", "type": "refund_request",
         "summary": "Refund requested for a phone case, approved and processed."},
    ],
    "cust_2002": [
        {"ticket_id": "tk_009", "date": "2026-05-21", "type": "subscription_account",
         "summary": "Account flagged after a payment dispute on ord_6002; dispute still open."},
    ],
    "cust_3003": [
        {"ticket_id": "tk_022", "date": "2026-07-16", "type": "delivery_issue",
         "summary": "Reported ord_7004 keyboard delayed past estimate; delay compensation discussed."},
    ],
    "cust_4004": [
        {"ticket_id": "tk_030", "date": "2026-07-11", "type": "subscription_account",
         "summary": "Account suspended for repeated chargebacks; first appeal denied."},
    ],
}


def get_prior_tickets(customer_id: str) -> list[dict]:
    """Past tickets for a customer, or [] if none. Scoped by the caller to the
    ticket's own customer — same permission rule as the MCP tools."""
    return PRIOR_TICKETS.get(customer_id, [])
