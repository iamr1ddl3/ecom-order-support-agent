"""
CLI entry point.

Two modes:
  python main.py --demo          run the 5 canned scenarios end-to-end (use first;
                                 this is what the video demo records).
  python main.py --customer cust_1001    interactive multi-turn chat as one customer.

Provider is chosen by --provider or $LLM_PROVIDER (default anthropic). Every run
prints the retriever chunk trace and the harness gate decision for each tool call,
so grounding and the permission boundary are visible on screen, not just claimed.
"""

import argparse

from dotenv import load_dotenv

from agent.harness import Harness, Ticket
from scenarios import SCENARIOS

load_dotenv()


def run_demo(provider_name: str) -> None:
    harness = Harness(provider_name)
    for label, customer_id, messages in SCENARIOS:
        print("\n" + "=" * 72)
        print(f"{label}   [ticket customer: {customer_id}]")
        print("=" * 72)
        ticket = Ticket(customer_id)
        for msg in messages:
            print(f"\nCustomer: {msg}")
            reply = harness.send(ticket, msg)
            print(f"\nAgent: {reply}")


def run_chat(provider_name: str, customer_id: str) -> None:
    harness = Harness(provider_name)
    ticket = Ticket(customer_id)
    print(f"Support ticket open for {customer_id}. Type your message (Ctrl-C to quit).\n")
    while True:
        try:
            msg = input("Customer: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nTicket closed.")
            return
        if not msg:
            continue
        reply = harness.send(ticket, msg)
        print(f"\nAgent: {reply}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="E-commerce order-support agent.")
    parser.add_argument("--demo", action="store_true", help="run the 5 canned scenarios")
    parser.add_argument("--customer", help="customer ID for interactive chat, e.g. cust_1001")
    parser.add_argument("--provider", choices=["anthropic", "groq", "glm"], default=None,
                        help="LLM provider (default: $LLM_PROVIDER or anthropic)")
    args = parser.parse_args()

    if args.demo:
        run_demo(args.provider)
    elif args.customer:
        run_chat(args.provider, args.customer)
    else:
        parser.error("choose one of --demo or --customer <id>")
