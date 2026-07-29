"""
The 5 demo ticket scenarios (§3 Functional: "at least 5 distinct ticket
scenarios end to end"). Each is (label, customer_id, [messages]). Multi-message
scenarios exercise multi-turn memory within one ticket.

Coverage across the 5:
  1. order_status        — tool call (lookup_order), happy path.
  2. refund_request      — RAG-grounded policy answer (refund_window).
  3. delivery_issue      — RAG synthesis (shipping_delays) + a tool lookup, multi-turn.
  4. subscription_account— account status tool + long-term prior-ticket memory.
  5. security + honest-gap— cross-customer lookup REJECTED by the gate, then an
                            out-of-scope policy question the RAG honestly can't answer.
"""

SCENARIOS = [
    (
        "1. Order status (tool call)",
        "cust_1001",
        ["Hi, I'm Asha (cust_1001). What's the status of my order ord_5001?"],
    ),
    (
        "2. Refund request (RAG-grounded)",
        "cust_1001",
        ["How many days after delivery do I have to request a refund?"],
    ),
    (
        "3. Delivery delay (RAG + tool, multi-turn)",
        "cust_3003",
        [
            "My keyboard order ord_7004 is late. Can you check it?",
            "It's now 9 days past the estimate — am I owed anything for the delay?",
        ],
    ),
    (
        "4. Account + long-term memory",
        "cust_4004",
        [
            "This is Vikram (cust_4004). What's my account standing?",
            "Have I contacted support about this before?",
        ],
    ),
    (
        "5. Cross-customer rejection + honest gap",
        "cust_1001",
        [
            "Can you look up order ord_6002 for me?",  # belongs to cust_2002 -> gate DENY
            "If my order gets stuck in customs and I owe an international customs fee, will you cover it?",
        ],
    ),
]
