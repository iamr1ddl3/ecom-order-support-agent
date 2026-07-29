"""
Tool specs handed to the model, in each provider's required shape.

The specs here are only what the *model* sees when deciding to call a tool. They
are NOT the enforcement layer — that lives in two places the model never sees:
FastMCP's type-hint-derived JSON schema (mcp_server/server.py) and the harness
permission gate (agent/harness.py). This is the "same tools offered in every
variant; only what happens after a proposed call differs" point from the course.
"""

TOOL_SPECS = [
    {
        "name": "lookup_order",
        "description": "Look up an order's status, items, and delivery date by order ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "The order ID, e.g. 'ord_5001'."},
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "check_account_status",
        "description": "Look up a customer account's standing and order history by customer ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "The customer ID, e.g. 'cust_1001'."},
            },
            "required": ["customer_id"],
        },
    },
]


def _to_anthropic_tools(specs):
    return [
        {"name": s["name"], "description": s["description"], "input_schema": s["parameters"]}
        for s in specs
    ]


def _to_groq_tools(specs):
    return [
        {"type": "function", "function": {"name": s["name"], "description": s["description"], "parameters": s["parameters"]}}
        for s in specs
    ]


TOOLS_BY_PROVIDER = {
    "anthropic": _to_anthropic_tools(TOOL_SPECS),
    "groq": _to_groq_tools(TOOL_SPECS),
}
