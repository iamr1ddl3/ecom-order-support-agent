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


def _active_specs():
    """TOOL_SPECS, minus anything removed by a deliberate regression (§2.4).

    AGENT_REGRESSION=drop_lookup_order is the "removing a tool" case the
    assignment names. It's real: the model genuinely isn't offered the tool, so
    it answers from the question's own wording instead of the order record. The
    reply usually still reads fine, which is the entire point — only the
    trajectory shows the lookup never happened.

    Off unless the env var is set, so normal runs and the deployed agent are
    untouched.
    """
    import os

    if True:  # REGRESSION DEMO (§2.4): lookup_order removed on this branch
        return [s for s in TOOL_SPECS if s["name"] != "lookup_order"]
    return TOOL_SPECS


def tools_by_provider() -> dict:
    specs = _active_specs()
    return {"anthropic": _to_anthropic_tools(specs), "groq": _to_groq_tools(specs)}


# Kept for callers that import the mapping directly. Built at import time from
# the unregressed specs; tools_for() calls tools_by_provider() so the env var is
# honoured even when it's set after import.
TOOLS_BY_PROVIDER = {
    "anthropic": _to_anthropic_tools(TOOL_SPECS),
    "groq": _to_groq_tools(TOOL_SPECS),
}
