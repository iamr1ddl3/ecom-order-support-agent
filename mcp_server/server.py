"""
FastMCP stdio server exposing two read-only tools: lookup_order and
check_account_status.

Two independent protections live here, and they are different things:

1. Schema validation — FREE, from MCP. FastMCP derives a JSON schema from each
   tool's type hints (`order_id: str`). A malformed call (wrong type, missing
   required field) is rejected by the protocol layer before the function body
   runs. We wrote no `if` for this; that's the point (assignment §2.2 "rejected
   by the tool layer itself, not a manual if check you bolted on").

2. Permission scoping — NOT free, and it is deliberately DEFENSE-IN-DEPTH here.
   The primary boundary is the harness gate (agent/harness.py), which rejects a
   cross-customer call *before* it ever reaches this subprocess. This server
   re-checks anyway, so an unscoped call path exists nowhere — not even if some
   future caller forgets the harness. The ticket's customer is passed in at
   subprocess launch via TICKET_CUSTOMER_ID (the harness sets it), standing in
   for "this MCP server instance is scoped to one ticket."

Run as a subprocess over stdio; not invoked directly.
"""

import os

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

# Support both `python -m mcp_server.server` and direct subprocess launch.
try:
    from . import mock_data as data
except ImportError:  # launched as a bare script (sys.path is the package dir)
    import mock_data as data

# Ticket this server instance is scoped to. Empty = unscoped (never used by the
# agent; only a bare manual launch could leave it empty).
TICKET_CUSTOMER_ID = os.environ.get("TICKET_CUSTOMER_ID", "")

mcp = FastMCP("order-support", log_level="WARNING")


@mcp.tool()
def lookup_order(order_id: str) -> dict:
    """Look up an order's status, items, and delivery date by order ID."""
    order = data.ORDERS.get(order_id)
    if not order:
        raise ToolError(f"No such order '{order_id}'.")
    if TICKET_CUSTOMER_ID and order["customer_id"] != TICKET_CUSTOMER_ID:
        raise ToolError(
            f"PermissionError: order '{order_id}' does not belong to customer "
            f"'{TICKET_CUSTOMER_ID}' on this ticket. Request rejected."
        )
    return order


@mcp.tool()
def check_account_status(customer_id: str) -> dict:
    """Look up a customer account's standing and order history by customer ID."""
    if TICKET_CUSTOMER_ID and customer_id != TICKET_CUSTOMER_ID:
        raise ToolError(
            f"PermissionError: customer '{customer_id}' does not match customer "
            f"'{TICKET_CUSTOMER_ID}' on this ticket. Request rejected."
        )
    account = data.ACCOUNTS.get(customer_id)
    if not account:
        raise ToolError(f"No such account '{customer_id}'.")
    return account


if __name__ == "__main__":
    mcp.run(transport="stdio")
