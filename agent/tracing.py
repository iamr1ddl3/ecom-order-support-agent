"""
LangFuse tracing setup (§2.1).

The agent must produce real, timestamped, correctly-nested spans in a dashboard —
not print statements standing in for them. The existing `[GATE]`/`[RETRIEVER]`
prints stay: they cost nothing and they're the only visibility in CI logs, where
there is no dashboard. Tracing is additive.

Degrading to no-op: the v4 SDK already does this for us. With no keys it logs one
authentication error, disables itself, and returns a client whose spans are
inert — it does not raise. So this module doesn't reimplement that; it only
(a) decides once whether tracing is on, so callers can skip building span payloads,
and (b) silences the SDK's startup error when keys are deliberately absent, which
is the normal case in CI and for anyone running the agent without a LangFuse
account.

Observation types matter. `as_type` drives LangFuse's Agent Graph and its
automatic model/token capture, so the harness uses `retriever`/`generation`/`tool`
rather than tagging everything a generic span.

ponytail: no wrapper class over the SDK client — get_client() is already a
singleton and already no-ops. A wrapper would be a second thing to keep in sync.
"""

from __future__ import annotations

import logging
import os

# Keys are read from the environment by the SDK itself (LANGFUSE_PUBLIC_KEY /
# LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL, falling back to LANGFUSE_HOST). This
# module must therefore be imported AFTER load_dotenv() — see main.py.
_ENABLED: bool | None = None


def tracing_enabled() -> bool:
    """True when LangFuse credentials are present and tracing isn't switched off.

    Cached: the answer can't change within a process, and callers hit this on
    every tool call.
    """
    global _ENABLED
    if _ENABLED is None:
        has_keys = bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))
        off = os.environ.get("LANGFUSE_TRACING_ENABLED", "true").lower() == "false"
        _ENABLED = has_keys and not off
        if not has_keys:
            # Without this the SDK logs a red "Authentication error" on first use.
            # Running untraced is a supported mode here, not a misconfiguration.
            logging.getLogger("langfuse").setLevel(logging.CRITICAL)
    return _ENABLED


def get_tracer():
    """The LangFuse client. Safe to call untraced — spans are inert no-ops."""
    from langfuse import get_client

    return get_client()


def flush() -> None:
    """Push buffered spans before the process exits.

    `main.py --demo` is short-lived. Without this the run looks fine on screen
    and silently delivers nothing to the dashboard — the exact failure that makes
    a demo look complete while producing no evidence.
    """
    if tracing_enabled():
        get_tracer().flush()
