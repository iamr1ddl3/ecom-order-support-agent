"""
Mock in-memory DBs for the order-support agent.

Same customer IDs across ORDERS and ACCOUNTS (assignment §2.2). Account standing
spans active / flagged / suspended so the account and appeal scenarios have real
state to key off. Several orders belong to customers *other* than the demo ticket's
customer — those are the cross-customer reads the permission gate must reject.
"""

# customer_id -> display name
CUSTOMERS = {
    "cust_1001": {"name": "Asha Rao"},
    "cust_2002": {"name": "Rahul Mehta"},
    "cust_3003": {"name": "Meera Iyer"},
    "cust_4004": {"name": "Vikram Singh"},
}

# order_id -> order record. customer_id ties each order to its owner.
ORDERS = {
    "ord_5001": {"customer_id": "cust_1001", "item": "Wireless Earbuds",  "status": "shipped",   "delivery_date": "2026-07-31"},
    "ord_5002": {"customer_id": "cust_1001", "item": "Phone Case",        "status": "delivered", "delivery_date": "2026-07-20"},
    "ord_6002": {"customer_id": "cust_2002", "item": "Laptop Stand",      "status": "delivered", "delivery_date": "2026-07-15"},
    "ord_6003": {"customer_id": "cust_2002", "item": "USB-C Cable",       "status": "shipped",   "delivery_date": "2026-08-02"},
    "ord_7004": {"customer_id": "cust_3003", "item": "Mechanical Keyboard","status": "delayed",  "delivery_date": "2026-07-18"},
    "ord_8005": {"customer_id": "cust_4004", "item": "Standing Desk",     "status": "delivered", "delivery_date": "2026-07-10"},
}

# customer_id -> account record. `orders` mirrors ownership above.
ACCOUNTS = {
    "cust_1001": {"standing": "active",    "orders": ["ord_5001", "ord_5002"]},
    "cust_2002": {"standing": "flagged",   "orders": ["ord_6002", "ord_6003"]},
    "cust_3003": {"standing": "active",    "orders": ["ord_7004"]},
    "cust_4004": {"standing": "suspended", "orders": ["ord_8005"]},
}
