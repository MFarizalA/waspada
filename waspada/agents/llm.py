"""Thin, mockable LLM wrapper for agent reasoning (WA-008).

Two implementations behind one interface (:class:`LLM`):

  * :class:`MockLLM` — a deterministic, network-free brain. The framework
    runs end-to-end with this; tests and offline dev never touch the network.
    Returns either a canned reply or the next entry from a script you supply.
  * :class:`GeminiLLM` — a thin wrapper over the ``google-generativeai`` SDK
    (Gemini free tier). The SDK is imported *lazily inside ``__init__``* so
    the module imports cleanly even when the package isn't installed (the
    mock is the default brain in CI / offline).

Selection: :func:`get_llm` reads ``WASPADA_LLM_PROVIDER`` (``"mock"`` default,
``"gemini"`` opt-in). Tests and the offline path inject a ``MockLLM``
directly — they never go through the network.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import List, Optional, Sequence


class LLM(ABC):
    """The reasoning surface every agent talks to.

    One method, ``complete``: a prompt in, a string out. Kept minimal on
    purpose — agents that need structured output parse it themselves
    (WA-009's pipeline agents). The :attr:`name` is logged in the step log so
    an audit can tell a mock run from a Gemini run.
    """

    name: str = "llm"

    @abstractmethod
    def complete(self, prompt: str, *, history: Optional[Sequence[str]] = None) -> str:
        """Return the model's completion for ``prompt``.

        ``history`` is an optional list of prior turns (for agents that keep
        a conversation); implementations may ignore it.
        """
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# MockLLM — the offline brain
# --------------------------------------------------------------------------- #
class MockLLM(LLM):
    """Deterministic, network-free brain.

    Two modes:

    * **scripted** — pass ``script=[...]``; each :meth:`complete` call pops
      the next reply in order. Exhausts to the last entry. Useful for tests
      that need specific responses per turn.
    * **canned** — no script: every call returns ``reply`` (default
      ``"mock-llm-ok"``). Useful when an agent just needs *a* string back.
    """

    name = "mock"

    def __init__(self, *, reply: str = "mock-llm-ok", script: Optional[Sequence[str]] = None) -> None:
        self._reply = reply
        self._script: List[str] = list(script) if script else []
        self._i = 0
        self.calls: List[str] = []  # exposed for tests / audit

    def complete(self, prompt: str, *, history: Optional[Sequence[str]] = None) -> str:
        self.calls.append(prompt)
        if self._script:
            out = self._script[min(self._i, len(self._script) - 1)]
            self._i += 1
            return out
        return self._reply


# --------------------------------------------------------------------------- #
# GeminiLLM — the real brain (lazy SDK import)
# --------------------------------------------------------------------------- #
class GeminiLLM(LLM):
    """Thin wrapper over ``google-generativeai`` (Gemini free tier).

    The SDK is imported lazily in ``__init__`` so this module — and the whole
    ``waspada.agents`` framework — imports cleanly even when the package
    isn't installed. A missing SDK or API key raises a clear ``RuntimeError``
    at construction, never an opaque ``ImportError`` at import time.
    """

    name = "gemini"
    DEFAULT_MODEL = "gemini-1.5-flash"  # free-tier friendly default

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "GeminiLLM needs GEMINI_API_KEY (see .env.example); for offline "
                "runs use MockLLM or set WASPADA_LLM_PROVIDER=mock."
            )
        try:
            import google.generativeai as genai  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised only with SDK absent
            raise RuntimeError(
                "google-generativeai is not installed; pip install google-generativeai "
                "or run with the mock brain (WASPADA_LLM_PROVIDER=mock)."
            ) from exc

        self._genai = genai
        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel(model or self.DEFAULT_MODEL)
        self.model_name = model or self.DEFAULT_MODEL

    def complete(self, prompt: str, *, history: Optional[Sequence[str]] = None) -> str:  # pragma: no cover - network
        resp = self._model.generate_content(prompt)
        # The SDK returns an object with .text; guard for safety.
        return getattr(resp, "text", str(resp))


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
def get_llm(provider: Optional[str] = None) -> LLM:
    """Pick an :class:`LLM` by provider name, defaulting to the mock brain.

    ``provider`` overrides ``WASPADA_LLM_PROVIDER``; both default to
    ``"mock"`` so the framework never reaches for the network unless a caller
    explicitly opts in. Tests should inject an ``LLM`` directly rather than
    go through here.
    """
    provider = (provider or os.environ.get("WASPADA_LLM_PROVIDER") or "mock").strip().lower()
    if provider == "mock":
        return MockLLM()
    if provider == "gemini":
        return GeminiLLM()
    raise ValueError(
        f"WASPADA_LLM_PROVIDER={provider!r} is invalid; use 'mock' or 'gemini'."
    )
