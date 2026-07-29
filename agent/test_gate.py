"""
Permission-gate unit test — the security boundary, no LLM, no MCP subprocess.

Tests _permission_gate() directly: the exact function whose ALLOW/DENY decides
whether a proposed tool call ever reaches the MCP server. If this logic breaks,
cross-customer reads leak — so it gets a runnable check.

Run: python -m agent.test_gate   (from the project root)
"""

from agent.harness import Harness, Ticket


def demo() -> None:
    h = Harness.__new__(Harness)  # skip __init__ (no provider/key needed)
    from mcp_server.mock_data import ORDERS
    h._orders = ORDERS
    ticket = Ticket("cust_1001")  # ord_5001/ord_5002 are theirs; ord_6002 is cust_2002's

    cases = [
        # (tool, args, expected_allowed, note)
        ("lookup_order", {"order_id": "ord_5001"}, True,  "own order"),
        ("lookup_order", {"order_id": "ord_6002"}, False, "cross-customer order"),
        ("lookup_order", {"order_id": "ord_9999"}, False, "nonexistent order"),
        ("lookup_order", {"order_id": ["ord_5001"]}, False, "malformed (list)"),
        ("lookup_order", {"order_id": "5001"}, False, "malformed (no prefix)"),
        ("check_account_status", {"customer_id": "cust_1001"}, True,  "own account"),
        ("check_account_status", {"customer_id": "cust_2002"}, False, "cross-customer account"),
        ("check_account_status", {"customer_id": "bad"}, False, "malformed customer_id"),
        ("issue_refund", {"order_id": "ord_5001"}, False, "unknown/unoffered tool"),
    ]

    for tool, args, expected, note in cases:
        d = h._permission_gate(ticket, tool, args)
        status = "ok" if d.allowed == expected else "FAIL"
        print(f"[{status}] {tool}({args}) -> {'ALLOW' if d.allowed else 'DENY'}  ({note})")
        assert d.allowed == expected, f"{note}: expected allowed={expected}, got {d.allowed} ({d.reason})"

    print("\nAll permission-gate checks passed.")


if __name__ == "__main__":
    demo()
