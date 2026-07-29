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
from dataclasses import dataclass, field

from .tools_schema import TOOLS_BY_PROVIDER

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


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str | None = None):
        import anthropic  # imported lazily so a Groq-only user needs no anthropic key

        self._client = anthropic.Anthropic()
        self._model = model or DEFAULT_MODELS["anthropic"]

    def create(self, system: str, messages: list, tools: list) -> Response:
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=system,
            messages=messages,
            tools=tools or [],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        tool_calls = [
            ToolCall(id=b.id, name=b.name, input=b.input)
            for b in resp.content
            if b.type == "tool_use"
        ]
        return Response(text=text, stop_reason=resp.stop_reason, tool_calls=tool_calls, raw=resp)

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
        resp = self._client.chat.completions.create(
            model=self._model,
            max_tokens=1024,
            messages=full,
            tools=tools or None,  # empty list is rejected; None means "no tools"
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
        return Response(text=msg.content or "", stop_reason=stop_reason, tool_calls=tool_calls, raw=msg)

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


def get_provider(name: str | None = None):
    """Factory. Defaults to $LLM_PROVIDER, then 'anthropic'. The harness calls this
    once; everything downstream is provider-agnostic."""
    name = (name or os.environ.get("LLM_PROVIDER") or "anthropic").lower()
    if name not in _VALID:
        raise ValueError(f"Unknown provider '{name}'. Choose from {sorted(_VALID)}.")
    if name == "anthropic":
        return AnthropicProvider()
    return OpenAICompatibleProvider(name)


def tools_for(provider_name: str) -> list:
    """Tool specs in the shape the given provider expects. Groq and GLM share the
    OpenAI shape, so both map to the 'groq' spec set."""
    key = "anthropic" if provider_name == "anthropic" else "groq"
    return TOOLS_BY_PROVIDER[key]
