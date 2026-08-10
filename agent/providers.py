"""
Provider abstraction — one interface, three backends (Anthropic Claude, Groq,
and GLM via Z.ai).

Assignment §7 asks for exactly this: "isolate [the LLM] behind a small provider
abstraction (one function that takes a system prompt, messages, and tools, and
returns a normalized response)." Everything above this file (harness, RAG demos)
talks to a `Provider` and never imports a vendor SDK directly, so swapping
providers is a one-env-var change, not a code change scattered everywhere.

Two request/response shapes, not three: Groq and GLM are both OpenAI-compatible,
so they share ONE class (OpenAICompatibleProvider) differing only in base_url,
model, and env key — that's the whole payoff of the abstraction. Anthropic has
its own shape and its own class. The differences the class hides:
  - tools: Anthropic wants {name, description, input_schema}; OpenAI shape wants
    {type: "function", function: {...}}.  -> tools_schema.TOOLS_BY_PROVIDER
  - tool calls back: Anthropic returns content blocks with .input (already a dict);
    OpenAI shape returns tool_calls with .function.arguments (a JSON *string*).
  - conversation replay: Anthropic threads tool_use/tool_result content blocks;
    OpenAI shape uses role:"tool" messages keyed by tool_call_id.
This module hides all of that behind one normalized `Response` + three methods.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

from .tools_schema import tools_by_provider

# Per-provider config for the OpenAI-compatible backends. base_url + api key env
# are the only things that differ between Groq and GLM.
OPENAI_COMPATIBLE = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "default_model": "openai/gpt-oss-120b",
    },
    "glm": {
        "base_url": "https://api.z.ai/api/paas/v4",
        "key_env": "ZAI_API_KEY",
        "default_model": "glm-4.6",
    },
}

DEFAULT_MODELS = {"anthropic": "claude-sonnet-4-5"}


# Longest pause worth sleeping through inside a retry. Above this it isn't a
# burst limit, it's an exhausted quota, and blocking CI for minutes to fail
# anyway is worse than reporting it.
MAX_RETRY_WAIT = 90.0


def _retry_after(message: str) -> float | None:
    """Seconds the provider asked us to wait, or None if it didn't say.

    Providers write this several ways — Groq alone emits both "try again in
    7.08s" and "try again in 4m42.528s". Parsing only the first form (as this
    originally did) silently discards the hint for the minutes-long case and
    falls back to a 2s backoff against a quota that needs four minutes.
    """
    match = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", message)
    if match:
        minutes = float(match.group(1) or 0)
        return minutes * 60 + float(match.group(2)) + 0.5
    match = re.search(r"retry[- ]after[\"']?[:=]\s*[\"']?([\d.]+)", message, re.IGNORECASE)
    return float(match.group(1)) + 0.5 if match else None


def _with_retry(call, attempts: int = 5):
    """Retry a provider call on rate limits, honouring the server's own wait hint.

    Free tiers are token-per-minute capped (Groq's is 8k TPM), and a 13-ticket
    eval sweep exceeds that comfortably. Without this, a 429 surfaces as a failed
    ticket and the regression gate reports "regression" for what is really a
    quota pause — a gate that is red for the wrong reason is one people learn to
    ignore, which is the failure §2.4 exists to prevent.

    Sleeps for the interval the error names when it gives one (Groq and Anthropic
    both do), otherwise backs off exponentially. Retries only rate limits;
    everything else is a real error and is raised immediately.
    """
    for attempt in range(attempts):
        try:
            return call()
        except Exception as exc:
            is_rate_limit = type(exc).__name__ == "RateLimitError" or "429" in str(exc)
            if not is_rate_limit or attempt == attempts - 1:
                raise
            delay = _retry_after(str(exc))
            if delay is None:
                delay = 2.0 * (2 ** attempt)
            elif delay > MAX_RETRY_WAIT:
                # A multi-minute wait means a daily/hourly quota, not a
                # per-minute burst. Sleeping it out would hang CI for minutes and
                # still fail; surfacing it immediately is the honest outcome.
                print(f"  [PROVIDER] rate limited for {delay:.0f}s — quota exhausted, not a burst")
                raise
            print(f"  [PROVIDER] rate limited, retrying in {delay:.1f}s "
                  f"(attempt {attempt + 1}/{attempts})")
            time.sleep(delay)


@dataclass
class ToolCall:
    """One tool call the model proposed. `input` is always a parsed dict, whichever
    provider produced it — the harness never has to know who serialized it."""
    id: str
    name: str
    input: dict


@dataclass
class Response:
    """Normalized model response. `stop_reason == "tool_use"` means the model wants
    to call at least one tool; the harness inspects `tool_calls` before running any."""
    text: str
    stop_reason: str  # "tool_use" | "end_turn"
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: object = None  # provider-native object, kept so append_*_turn can replay it
    # {"input": n, "output": n} — normalized here rather than dug out of `raw`
    # downstream, because `raw` is a different shape per provider (Anthropic
    # stores the whole response, the OpenAI path stores just the message, which
    # carries no usage at all). Tracing reads this.
    usage: dict = field(default_factory=dict)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str | None = None):
        import anthropic  # imported lazily so a Groq-only user needs no anthropic key

        self._client = anthropic.Anthropic()
        self._model = model or DEFAULT_MODELS["anthropic"]

    def create(self, system: str, messages: list, tools: list) -> Response:
        resp = _with_retry(
            lambda: self._client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=system,
                messages=messages,
                tools=tools or [],
            )
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        tool_calls = [
            ToolCall(id=b.id, name=b.name, input=b.input)
            for b in resp.content
            if b.type == "tool_use"
        ]
        u = getattr(resp, "usage", None)
        usage = {"input": u.input_tokens, "output": u.output_tokens} if u else {}
        return Response(text=text, stop_reason=resp.stop_reason, tool_calls=tool_calls,
                        raw=resp, usage=usage)

    def append_assistant_turn(self, messages: list, response: Response) -> None:
        # Replay the assistant's exact content blocks (text + tool_use) so the next
        # call sees the tool calls it's about to answer.
        messages.append({"role": "assistant", "content": response.raw.content})

    def append_tool_results(self, messages: list, response: Response, results: dict) -> None:
        # results: {tool_call_id: result_string}. Anthropic wants these as
        # tool_result content blocks inside a single user message.
        content = [
            {"type": "tool_result", "tool_use_id": tc_id, "content": str(result)}
            for tc_id, result in results.items()
        ]
        messages.append({"role": "user", "content": content})


class OpenAICompatibleProvider:
    """Groq and GLM (Z.ai): same OpenAI wire format, different base_url + key.
    One class serves both — the point of the abstraction."""

    def __init__(self, name: str, model: str | None = None):
        from openai import OpenAI  # lazy: an Anthropic-only user needs no openai key

        cfg = OPENAI_COMPATIBLE[name]
        self.name = name
        self._client = OpenAI(base_url=cfg["base_url"], api_key=os.environ.get(cfg["key_env"]))
        self._model = model or cfg["default_model"]

    def create(self, system: str, messages: list, tools: list) -> Response:
        full = [{"role": "system", "content": system}, *messages]
        resp = _with_retry(
            lambda: self._client.chat.completions.create(
                model=self._model,
                max_tokens=1024,
                messages=full,
                tools=tools or None,  # empty list is rejected; None means "no tools"
            )
        )
        msg = resp.choices[0].message
        raw_calls = msg.tool_calls or []
        tool_calls = [
            # OpenAI shape hands arguments back as a JSON string — parse it so the
            # harness always sees a dict, like Anthropic's already-parsed .input.
            ToolCall(id=c.id, name=c.function.name, input=json.loads(c.function.arguments or "{}"))
            for c in raw_calls
        ]
        stop_reason = "tool_use" if tool_calls else "end_turn"
        # Usage lives on the response, not the message — and `raw` below is the
        # MESSAGE (append_assistant_turn needs it), so it has to be read here or
        # it's lost.
        u = getattr(resp, "usage", None)
        usage = {"input": u.prompt_tokens, "output": u.completion_tokens} if u else {}
        return Response(text=msg.content or "", stop_reason=stop_reason,
                        tool_calls=tool_calls, raw=msg, usage=usage)

    def append_assistant_turn(self, messages: list, response: Response) -> None:
        entry = {"role": "assistant", "content": response.text or None}
        if response.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.input)},
                }
                for tc in response.tool_calls
            ]
        messages.append(entry)

    def append_tool_results(self, messages: list, response: Response, results: dict) -> None:
        for tc_id, result in results.items():
            messages.append({"role": "tool", "tool_call_id": tc_id, "content": str(result)})


_VALID = {"anthropic", "groq", "glm"}


def _trace_create(provider):
    """Wrap provider.create() in a LangFuse `generation` observation (§2.1).

    Applied at the factory, not inside each class, so all three backends are
    instrumented from one place and a future provider can't quietly ship
    untraced. `as_type="generation"` (rather than a generic span) is what makes
    LangFuse capture model and token usage automatically.
    """
    from agent.tracing import get_tracer, tracing_enabled

    if not tracing_enabled():
        return provider

    inner = provider.create

    def create(system: str, messages: list, tools: list) -> Response:
        with get_tracer().start_as_current_observation(
            as_type="generation",
            name=f"{provider.name}-create",
            model=provider._model,
            input=messages,
        ) as gen:
            resp = inner(system, messages, tools)
            gen.update(
                output=resp.text or [{"tool": tc.name, "input": tc.input} for tc in resp.tool_calls],
                metadata={"stop_reason": resp.stop_reason},
            )
            # Read the normalized field, not resp.raw: `raw` holds the full
            # response for Anthropic but only the message for the OpenAI-shaped
            # providers, and a message carries no usage — so this silently
            # reported zero tokens for Groq and GLM.
            if resp.usage:
                gen.update(usage_details=resp.usage)
            return resp

    provider.create = create
    return provider


def get_provider(name: str | None = None):
    """Factory. Defaults to $LLM_PROVIDER, then 'anthropic'. The harness calls this
    once; everything downstream is provider-agnostic."""
    name = (name or os.environ.get("LLM_PROVIDER") or "anthropic").lower()
    if name not in _VALID:
        raise ValueError(f"Unknown provider '{name}'. Choose from {sorted(_VALID)}.")
    provider = AnthropicProvider() if name == "anthropic" else OpenAICompatibleProvider(name)
    return _trace_create(provider)


def tools_for(provider_name: str) -> list:
    """Tool specs in the shape the given provider expects. Groq and GLM share the
    OpenAI shape, so both map to the 'groq' spec set.

    Built per call rather than read from the module-level mapping so that
    AGENT_REGRESSION (§2.4) applies even when it's set after import — which is
    exactly what eval/before_after.py does when it runs both variants in one
    process."""
    key = "anthropic" if provider_name == "anthropic" else "groq"
    return tools_by_provider()[key]
