"""
MCP-layer sanity check — no LLM. Proves the two protections in server.py work at
the protocol layer, exactly what §2.2 + §3 (Craft) ask to demonstrate:

  1. malformed call (order_id as a list, not a string) -> rejected by FastMCP's
     derived schema, isError=True, no crash, tool body never runs.
  2. cross-customer call (an order owned by someone else) -> rejected by the
     server's scoped ownership re-check, isError=True.
  3. a valid, in-scope call succeeds.

Run: python -m mcp_server.test_server   (from the project root)
"""

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

_SERVER = str(Path(__file__).parent / "server.py")
TICKET_CUSTOMER = "cust_1001"


async def _call(session, name, args):
    result = await session.call_tool(name, args)
    text = " ".join(b.text for b in result.content if getattr(b, "text", None))
    return result.isError, text


async def main():
    params = StdioServerParameters(
        command=sys.executable, args=[_SERVER],
        env={**os.environ, "TICKET_CUSTOMER_ID": TICKET_CUSTOMER},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("1. malformed: lookup_order(order_id=['ord_6002'])  # a list, not a string")
            is_err, text = await _call(session, "lookup_order", {"order_id": ["ord_6002"]})
            print(f"   isError={is_err} -> {text[:120]}")
            assert is_err, "schema should have rejected a list order_id"

            print("2. cross-customer: lookup_order('ord_6002')  # owned by cust_2002, ticket is cust_1001")
            is_err, text = await _call(session, "lookup_order", {"order_id": "ord_6002"})
            print(f"   isError={is_err} -> {text[:120]}")
            assert is_err, "scoped check should reject a cross-customer order"

            print("3. valid, in-scope: lookup_order('ord_5001')  # owned by cust_1001")
            is_err, text = await _call(session, "lookup_order", {"order_id": "ord_5001"})
            print(f"   isError={is_err} -> {text[:120]}")
            assert not is_err, "in-scope call should succeed"

    print("\nAll MCP-layer checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
