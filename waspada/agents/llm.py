"""Thin, mockable LLM wrapper for agent reasoning (WA-008).

Two implementations behind one interface (:class:`LLM`):

  * :class:`MockLLM` — a deterministic, network-free brain. The framework
    runs end-to-end with this; tests and offline dev never touch the network.
    Returns either a canned reply or the next entry from a script you supply.
  * :class:`QwenLLM` — a thin wrapper over Qwen models via Alibaba Cloud's
    OpenAI-compatible DashScope endpoint (the "Agent Society" negotiation
    brain — see :mod:`waspada.agents.risk_auditor`). The SDK is imported
    *lazily inside ``__init__``* so the module imports cleanly even when the
    package isn't installed.

Selection: :func:`get_llm` reads ``WASPADA_LLM_PROVIDER`` (``"mock"`` default,
``"qwen"`` opt-in). Tests and the offline path inject a
``MockLLM`` directly — they never go through the network.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


# --------------------------------------------------------------------------- #
# Native tool-calling types (WA-041)
# --------------------------------------------------------------------------- #
@dataclass
class ToolCall:
    """One native function call emitted by the LLM (OpenAI ``tool_calls`` shape).

    Mirrors the ``response.choices[0].message.tool_calls[i]`` structure from
    the OpenAI-compatible DashScope endpoint. ``arguments`` is a raw JSON
    string (as returned by the API); callers parse it with ``json.loads``.
    """

    id: str = ""
    name: str = ""
    arguments: str = "{}"  # JSON string, never None

    def parsed_arguments(self) -> Dict[str, Any]:
        """Parse ``arguments`` as a dict (empty dict on failure)."""
        import json

        try:
            obj = json.loads(self.arguments or "{}")
            return obj if isinstance(obj, dict) else {}
        except (ValueError, TypeError):
            return {}


@dataclass
class ChatResponse:
    """Structured LLM response carrying text content and/or native tool calls.

    When ``tool_calls`` is non-empty the model wants to call functions — the
    caller executes them and feeds results back in a subsequent ``chat()`` call.
    When ``tool_calls`` is empty the model is done; ``content`` holds the final
    answer (agents that expect JSON parse it from ``content``).
    """

    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)

    @property
    def has_tool_calls(self) -> bool:
        """True if the model emitted one or more tool calls."""
        return bool(self.tool_calls)


class LLM(ABC):
    """The reasoning surface every agent talks to.

    Two methods:

    * ``complete`` — a prompt in, a string out. The legacy surface every
      agent used before WA-041. Agents that need structured output parse it
      themselves (WA-009's pipeline agents).
    * ``chat`` — the native tool-calling surface (WA-041). Returns a
      :class:`ChatResponse` carrying ``content`` and/or ``tool_calls``. The
      default implementation wraps ``complete()`` so every existing brain
      gains tool-call support for free (returns content-only responses).

    The :attr:`name` is logged in the step log so an audit can tell a mock
    run from a Qwen run.
    """

    name: str = "llm"
    model_name: str = "llm"  # specific model id for audit logging (e.g. "qwen3.6-flash")
    # WA-084: does this brain emit NATIVE OpenAI tool_calls (vs prompt-parsed JSON)?
    # Agents that dispatch native-vs-legacy read this to decide WITHOUT consuming a
    # scripted reply. Default False; QwenLLM sets True; MockLLM opts in via native_tools=.
    supports_native_tools: bool = False

    @abstractmethod
    def complete(self, prompt: str, *, history: Optional[Sequence[str]] = None) -> str:
        """Return the model's completion for ``prompt``.

        ``history`` is an optional list of prior turns (for agents that keep
        a conversation); implementations may ignore it.
        """
        raise NotImplementedError

    def chat(
        self,
        prompt: str,
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> ChatResponse:
        """Native tool-calling surface (WA-041).

        Returns a :class:`ChatResponse`. When ``tools`` is provided, a
        supporting backend (QwenLLM) declares them natively and the model
        may emit ``tool_calls`` in the response. When ``tools`` is ``None``
        or the backend doesn't support native function calling, the response
        is content-only (identical to :meth:`complete` wrapped in a
        :class:`ChatResponse`).

        ``messages`` is the full conversation as OpenAI-format message dicts
        (for multi-turn tool-calling loops). When omitted, a single-user
        message is built from ``prompt``.

        The default implementation wraps :meth:`complete` — every brain gets
        tool-call support for free, returning content-only responses. Override
        in subclasses that actually support native ``tools``/``tool_calls``.
        """
        text = self.complete(prompt)
        return ChatResponse(content=text, tool_calls=[])

    def with_model(self, model: str) -> "LLM":
        """Return a brain configured for a specific model tier.

        Default: return ``self`` (single-model brains like MockLLM ignore
        the override). QwenLLM overrides to create a tier-specific clone so
        each debate participant gets the right cognitive-load brain
        (flash < plus < max).
        """
        return self


# --------------------------------------------------------------------------- #
# MockLLM — the offline brain
# --------------------------------------------------------------------------- #
class MockLLM(LLM):
    """Deterministic, network-free brain.

    Two modes:

    * **scripted** — pass ``script=[...]``; each :meth:`complete` call pops
      the next reply in order. Exhausts to the last entry. Useful for tests
      that need specific responses per turn. Each script entry may be a plain
      ``str`` (legacy) or a :class:`ChatResponse` (WA-041 native tool_calls).
    * **canned** — no script: every call returns ``reply`` (default
      ``"mock-llm-ok"``). Useful when an agent just needs *a* string back.

    WA-041: :meth:`chat` now exercises the native tool-calling path. When a
    script entry is a :class:`ChatResponse` with ``tool_calls``, it is
    returned directly by ``chat()``. When it's a plain string, it's wrapped
    in a content-only :class:`ChatResponse`. This lets tests script full
    tool-calling loops (call → result → call → final answer) without a
    network.
    """

    name = "mock"
    model_name = "mock"

    def __init__(self, *, reply: str = "mock-llm-ok", script: Optional[Sequence[Any]] = None,
                 native_tools: bool = False) -> None:
        self._reply = reply
        self._script: List[Any] = list(script) if script else []
        self._i = 0
        self.calls: List[str] = []  # exposed for tests / audit
        # WA-084: opt in to the native tool-calling dispatch (script ChatResponses).
        self.supports_native_tools = native_tools

    def _next_scripted(self) -> Any:
        """Pop the next scripted reply (or exhaust to the last entry)."""
        out = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return out

    def complete(self, prompt: str, *, history: Optional[Sequence[str]] = None) -> str:
        self.calls.append(prompt)
        if self._script:
            out = self._next_scripted()
            # A scripted ChatResponse used in legacy complete() → unwrap content.
            if isinstance(out, ChatResponse):
                return out.content
            return out
        return self._reply

    def chat(
        self,
        prompt: str,
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> ChatResponse:
        """Native tool-calling mock (WA-041).

        When a script entry is a :class:`ChatResponse`, return it as-is (this
        is how tests script tool-call → tool-result → final-answer loops).
        When a script entry is a plain string, wrap it in a content-only
        :class:`ChatResponse`. When no script, return the canned reply.
        """
        self.calls.append(prompt)
        if self._script:
            out = self._next_scripted()
            if isinstance(out, ChatResponse):
                return out
            return ChatResponse(content=str(out), tool_calls=[])
        return ChatResponse(content=self._reply, tool_calls=[])


# --------------------------------------------------------------------------- #
# QwenLLM — Qwen Cloud (qwencloud.com), backed by Alibaba Cloud DashScope
# --------------------------------------------------------------------------- #
class QwenLLM(LLM):
    """Thin wrapper over Qwen models via Qwen Cloud's OpenAI-compatible
    endpoint (docs.qwencloud.com — the hackathon's platform, itself backed by
    Alibaba Cloud DashScope/Model Studio infrastructure).

    Uses the official ``openai`` SDK pointed at the compatible-mode
    ``base_url`` — Qwen Cloud's own documented quickstart path, and the most
    practical route since most agent tooling already assumes an OpenAI-shaped
    client. The SDK is imported lazily in ``__init__``: the module imports
    cleanly with no SDK installed, and a cold container start doesn't pay the
    import cost unless a request actually reaches for Qwen. API keys:
    home.qwencloud.com.
    """

    name = "qwen"
    supports_native_tools = True  # WA-084: real OpenAI tool_calls surface
    DEFAULT_MODEL = "qwen3.7-plus"
    DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

    # WA-045 egress control — the only domains QwenLLM is ever allowed to call.
    # Both the intl (dashscope-intl) and CN (dashscope) variants are valid.
    _ALLOWED_BASE_DOMAINS = (
        "dashscope.aliyuncs.com",       # CN endpoint
        "dashscope-intl.aliyuncs.com",  # international endpoint
    )

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        json_mode: bool = False,
    ) -> None:
        api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "QwenLLM needs DASHSCOPE_API_KEY (see .env.example); for offline "
                "runs use MockLLM or set WASPADA_LLM_PROVIDER=mock."
            )

        # WA-045: validate base_url against the DashScope allowlist BEFORE
        # creating the client. Both the explicit param and the env override
        # are checked — a prompt injection that flips DASHSCOPE_BASE_URL to
        # an attacker-controlled host must be rejected at construction.
        resolved_base = (
            base_url
            or os.environ.get("DASHSCOPE_BASE_URL")
            or self.DEFAULT_BASE_URL
        )
        if not any(d in (resolved_base or "") for d in self._ALLOWED_BASE_DOMAINS):
            raise ValueError(
                f"Blocked: QwenLLM base_url must contain "
                f"{' or '.join(self._ALLOWED_BASE_DOMAINS)!r} (got {resolved_base!r}). "
                f"This is an egress control (WA-045) — loan data must only "
                f"be sent to dashscope.aliyuncs.com."
            )

        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised only with SDK absent
            raise RuntimeError(
                "openai is not installed; pip install openai or run with the "
                "mock brain (WASPADA_LLM_PROVIDER=mock)."
            ) from exc

        self._client = OpenAI(
            api_key=api_key,
            base_url=resolved_base,
        )
        self.model_name = model or os.environ.get("QWEN_MODEL") or self.DEFAULT_MODEL
        self.json_mode = json_mode

    def with_model(self, model: str) -> "QwenLLM":
        """Return a tier-specific clone sharing the same client + base_url.

        Model tiering by cognitive load (HACKATHON.md § Judging rubric):
        Skeptic uses ``qwen3.6-flash`` (cheap), Actuary rebuttal uses
        ``qwen3.7-plus`` (mid), Arbiter uses ``qwen3.7-max`` (top). Each tier
        shares one client + API key; only the model id changes so the audit
        log records the real brain per agent.
        """
        clone = QwenLLM.__new__(QwenLLM)
        clone._client = self._client  # shared HTTP client
        clone.model_name = model
        clone.json_mode = self.json_mode
        return clone

    def complete(
        self,
        prompt: str,
        *,
        history: Optional[Sequence[str]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> str:  # pragma: no cover - network
        kwargs: dict = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if tools:
            kwargs["tools"] = tools
        resp = self._client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""

    def chat(
        self,
        prompt: str,
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> "ChatResponse":  # pragma: no cover - network
        """Native tool-calling surface (WA-041).

        When ``tools`` is provided, the Qwen DashScope endpoint is called with
        the OpenAI-compatible ``tools`` parameter. The model may emit
        ``tool_calls`` in the response message (the caller executes them and
        feeds results back via ``messages`` on the next turn). When ``tools``
        is ``None``, this is identical to :meth:`complete` but returns a
        :class:`ChatResponse`.

        ``messages`` replaces the synthesized single-user-message when provided
        (multi-turn tool-calling loops pass the full conversation).
        """
        msg_list = list(messages) if messages is not None else [
            {"role": "user", "content": prompt}
        ]
        kwargs: dict = {"model": self.model_name, "messages": msg_list}
        if self.json_mode and not tools:
            kwargs["response_format"] = {"type": "json_object"}
        if tools:
            kwargs["tools"] = tools
        resp = self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        content = msg.content or ""
        tool_calls: List[ToolCall] = []
        raw_calls = getattr(msg, "tool_calls", None) or []
        for tc in raw_calls:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            tool_calls.append(ToolCall(
                id=getattr(tc, "id", "") or "",
                name=getattr(fn, "name", "") or "",
                arguments=getattr(fn, "arguments", "{}") or "{}",
            ))
        return ChatResponse(content=content, tool_calls=tool_calls)


# --------------------------------------------------------------------------- #
# Qwen model tiers — configurable via env (fall back to the documented defaults)
# --------------------------------------------------------------------------- #
QWEN_TIER_DEFAULTS = {
    "flash": "qwen3.6-flash",   # cheap triage — Data Engineer, Skeptic
    "plus": "qwen3.7-plus",     # mid — Data Analyst, Actuary rebuttal
    "max": "qwen3.7-max",       # top — Arbiter rulings
}
_QWEN_TIER_ENV = {
    "flash": "QWEN_MODEL_FLASH",
    "plus": "QWEN_MODEL_PLUS",
    "max": "QWEN_MODEL_MAX",
}


def qwen_tier(tier: str) -> str:
    """Resolve a cognitive-load tier (``flash`` | ``plus`` | ``max``) to a model id.

    Precedence: the tier's env override (``QWEN_MODEL_FLASH`` / ``QWEN_MODEL_PLUS``
    / ``QWEN_MODEL_MAX``) → the documented default. This lets an operator point
    the tiers at whatever models their DashScope account actually serves without
    touching code. On :class:`MockLLM` the id is only an audit label
    (``with_model`` returns self); on :class:`QwenLLM` it selects the real model
    per agent. With no env override the return value equals the previous
    hard-coded string, so offline/mock behavior is unchanged.
    """
    if tier not in QWEN_TIER_DEFAULTS:
        raise ValueError(f"unknown Qwen tier {tier!r}; use 'flash', 'plus', or 'max'")
    return os.environ.get(_QWEN_TIER_ENV[tier], "").strip() or QWEN_TIER_DEFAULTS[tier]


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
def get_llm(provider: Optional[str] = None, *, json_mode: bool = False) -> LLM:
    """Pick an :class:`LLM` by provider name, defaulting to the mock brain.

    ``provider`` overrides ``WASPADA_LLM_PROVIDER``; both default to
    ``"mock"`` so the framework never reaches for the network unless a caller
    explicitly opts in. Tests should inject an ``LLM`` directly rather than
    go through here.

    ``json_mode=True`` enables JSON-mode output on providers that support it
    (QwenLLM) — the debate protocol's ``response_format: json_object``
    guarantee with validate-and-retry.
    """
    provider = (provider or os.environ.get("WASPADA_LLM_PROVIDER") or "mock").strip().lower()
    if provider == "mock":
        return MockLLM()
    if provider == "qwen":
        return QwenLLM(json_mode=json_mode)
    raise ValueError(
        f"WASPADA_LLM_PROVIDER={provider!r} is invalid; use 'mock' or 'qwen'."
    )
