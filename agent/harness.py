"""
The harness — the whole point of the assignment.

The model PROPOSES tool calls; this code DECIDES whether each one runs. That
decision is `_permission_gate()` below, and it happens BEFORE the call is
dispatched to the MCP server. This is the line the assignment asks you to be able
to point at (§7: "can you point to the exact line where a proposed action gets
checked before it runs?"). It is `gate_ok, reason = self._permission_gate(...)` in
`_run_tool_calls`, and the dispatch on the next lines only happens when gate_ok.

Flow per ticket (multi-turn, §2.1): classify the turn, retrieve policy if the
turn is policy-shaped, then loop: model proposes -> GATE -> dispatch to MCP ->
feed result back -> repeat until the model stops proposing tools -> return text.

Permission model: a Ticket is opened for exactly one customer. Every tool call's
ID argument must resolve to THAT customer. The MCP server (launched scoped to the
same customer via TICKET_CUSTOMER_ID) re-checks too, so no unscoped path exists —
but the harness gate is the primary boundary and is what rejects the call without
ever spending a subprocess round-trip on it.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from agent.memory import Memory
from agent.providers import get_provider, tools_for
from rag.retriever import Retriever

_SERVER = str(Path(__file__).parent.parent / "mcp_server" / "server.py")

_ORDER_ID_RE = re.compile(r"^ord_\d+$")
_CUSTOMER_ID_RE = re.compile(r"^cust_\d+$")

# Which argument on each tool is the identity that must match the ticket, and
# whether it's an order (owner looked up via ORDERS) or a customer id (direct).
_TOOL_ID_ARG = {"lookup_order": ("order_id", "order"), "check_account_status": ("customer_id", "customer")}

TICKET_TYPES = ["order_status", "delivery_issue", "refund_request", "subscription_account"]

_SYSTEM_PROMPT = """You are an order-support assistant for an e-commerce company.
A support ticket is open for customer {customer_id}. Only ever act on behalf of
this customer.

Use the tools to look up real order and account data — never invent order status,
delivery dates, or account standing. When policy context is provided below, answer
policy questions using ONLY that context; if it says no policy covers the question,
say so honestly and offer to escalate rather than guessing.

{prior_tickets}{policy_context}"""


@dataclass
class GateDecision:
    allowed: bool
    reason: str


class Ticket:
    """One support conversation, bound to one customer at open time. The binding is
    what makes permission scoping enforceable — there is no 'current customer'
    ambiguity for the gate to get wrong."""

    def __init__(self, customer_id: str, ticket_type: str = "order_status"):
        if not _CUSTOMER_ID_RE.match(customer_id):
            raise ValueError(f"malformed customer_id at ticket open: {customer_id!r}")
        self.customer_id = customer_id
        self.ticket_type = ticket_type
        self.memory = Memory(customer_id)


class Harness:
    def __init__(self, provider_name: str | None = None):
        self.provider = get_provider(provider_name)
        self.tools = tools_for(self.provider.name)
        self.retriever = Retriever()
        # Loaded once so the gate can resolve an order_id -> owning customer without
        # a round-trip. This mirrors the server's ORDERS; in a real system the gate
        # would consult the same ownership source of truth the tools do.
        from mcp_server.mock_data import ORDERS
        self._orders = ORDERS

    # --- THE GATE -----------------------------------------------------------
    def _permission_gate(self, ticket: Ticket, tool_name: str, args: dict) -> GateDecision:
        """Decide whether a proposed tool call is allowed to run, BEFORE dispatch.

        Two checks, both here, both before the MCP server is ever contacted:
          1. shape — the ID argument is present and well-formed (defense against a
             malformed proposal; FastMCP would also reject, this fails faster).
          2. ownership — the ID resolves to the ticket's bound customer.
        A denied call never reaches the tool. This is the assignment's core line.
        """
        spec = _TOOL_ID_ARG.get(tool_name)
        if spec is None:
            return GateDecision(False, f"unknown tool '{tool_name}' — not offered on this ticket")
        arg_name, kind = spec
        value = args.get(arg_name)

        if kind == "order":
            if not isinstance(value, str) or not _ORDER_ID_RE.match(value):
                return GateDecision(False, f"malformed order_id {value!r}")
            owner = self._orders.get(value, {}).get("customer_id")
            if owner is None:
                return GateDecision(False, f"no such order '{value}'")
            if owner != ticket.customer_id:
                return GateDecision(False, f"order '{value}' belongs to {owner}, not ticket customer {ticket.customer_id} — rejected")
        else:  # customer
            if not isinstance(value, str) or not _CUSTOMER_ID_RE.match(value):
                return GateDecision(False, f"malformed customer_id {value!r}")
            if value != ticket.customer_id:
                return GateDecision(False, f"customer '{value}' does not match ticket customer {ticket.customer_id} — rejected")

        return GateDecision(True, "allowed")

    # --- classify + retrieve ------------------------------------------------
    def classify(self, message: str) -> str:
        """Cheap keyword classification into a ticket type. Kept deterministic and
        local (no LLM call) — it only decides whether to pre-retrieve policy, and
        being wrong just means the model asks for a tool instead. ponytail: keyword
        map, swap for an LLM classifier if types grow."""
        m = message.lower()
        if any(w in m for w in ("refund", "money back", "return")):
            return "refund_request"
        if any(w in m for w in ("delay", "late", "arriv", "shipping", "ship")):
            return "delivery_issue"
        if any(w in m for w in ("suspend", "cancel", "subscription", "account", "appeal")):
            return "subscription_account"
        return "order_status"

    def _retrieve_policy(self, message: str) -> tuple[str, list]:
        """Returns (context_block, hits). Empty hits => honest gap; the context
        block tells the model to say so instead of guessing."""
        hits = self.retriever.retrieve(message, k=2)
        if hits:
            for doc_id, score, _ in hits:
                print(f"  [RETRIEVER] used chunk: {doc_id} (score={score:.2f})")
            context = "\n\n".join(f"[{doc_id}] {text}" for doc_id, _, text in hits)
            return f"\n\nRetrieved policy context:\n{context}", hits
        print("  [RETRIEVER] no policy doc cleared the coverage threshold — honest gap")
        return "\n\nRetrieved policy context:\n(no policy doc covers this question)", hits

    # --- the loop -----------------------------------------------------------
    async def _run_tool_calls(self, ticket: Ticket, session: ClientSession, response) -> dict:
        """Run each proposed tool call through the gate, then dispatch the allowed
        ones to the MCP server. Denied calls return a rejection string as their tool
        result, so the model sees the refusal and can respond to the customer."""
        results = {}
        for tc in response.tool_calls:
            decision = self._permission_gate(ticket, tc.name, tc.input)
            print(f"  [GATE] {tc.name}({tc.input}) -> {'ALLOW' if decision.allowed else 'DENY'}: {decision.reason}")
            if not decision.allowed:
                results[tc.id] = f"REJECTED by harness: {decision.reason}"
                continue
            # Only reached for allowed calls — dispatch to the scoped MCP server.
            mcp_result = await session.call_tool(tc.name, tc.input)
            text = " ".join(b.text for b in mcp_result.content if getattr(b, "text", None))
            results[tc.id] = text or str(mcp_result.content)
        return results

    async def _converse(self, ticket: Ticket, user_message: str) -> str:
        ticket.ticket_type = self.classify(user_message)
        policy_context, _ = self._retrieve_policy(user_message)
        system = _SYSTEM_PROMPT.format(
            customer_id=ticket.customer_id,
            prior_tickets=(f"This customer's prior tickets:\n{ticket.memory.prior_tickets_summary()}"
                           if ticket.memory.prior_tickets_summary() else "This customer has no prior tickets."),
            policy_context=policy_context,
        )
        ticket.memory.add_user(user_message)
        messages = ticket.memory.history()

        # Launch the MCP server as a stdio subprocess, scoped to THIS ticket's
        # customer. The server re-checks ownership; the harness gate is primary.
        server_params = StdioServerParameters(
            command=sys.executable, args=[_SERVER],
            env={**os.environ, "TICKET_CUSTOMER_ID": ticket.customer_id},
        )
        async with AsyncExitStack() as stack:
            read, write = await stack.enter_async_context(stdio_client(server_params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()

            response = self.provider.create(system, messages, self.tools)
            while response.stop_reason == "tool_use":
                self.provider.append_assistant_turn(messages, response)
                results = await self._run_tool_calls(ticket, session, response)
                self.provider.append_tool_results(messages, response, results)
                response = self.provider.create(system, messages, self.tools)

        # Record the assistant's final answer into the short-term buffer so the next
        # turn in this ticket sees it (multi-turn memory).
        messages.append({"role": "assistant", "content": response.text})
        return response.text

    def send(self, ticket: Ticket, user_message: str) -> str:
        """Synchronous entry point for one user turn. Returns the agent's reply."""
        return asyncio.run(self._converse(ticket, user_message))
