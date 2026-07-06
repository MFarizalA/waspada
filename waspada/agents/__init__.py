"""WASPADA agent framework — the shared multi-agent substrate (WA-008).

Lane-agnostic substrate both decision lanes build on:

  * :class:`Agent` — base class (name/role, tools registry, step log).
  * :class:`ApprovalGate` + :class:`Approved` / :class:`Rejected` — the
    human-in-loop checkpoint, with auto-approve logged distinctly.
  * :class:`MockLLM` / :class:`GeminiLLM` / :func:`get_llm` — the mockable
    reasoning surface (default brain is offline/deterministic).
  * Protocol: :class:`AgentContext`, :class:`AgentResult`, :class:`Handoff`,
    :class:`Step`, :class:`Status`.

WA-009 (pipeline agents) and WA-010 (orchestrator) cite these names
verbatim. See :mod:`waspada.agents.protocol` for the wire contract.
"""
from __future__ import annotations

from .base import Agent, ApprovalGate, Approved, Rejected, handoff
from .llm import GeminiLLM, LLM, MockLLM, get_llm
from .protocol import AgentContext, AgentResult, Handoff, Status, Step

__all__ = [
    # base
    "Agent",
    "ApprovalGate",
    "Approved",
    "Rejected",
    "handoff",
    # llm
    "LLM",
    "MockLLM",
    "GeminiLLM",
    "get_llm",
    # protocol
    "AgentContext",
    "AgentResult",
    "Handoff",
    "Step",
    "Status",
]
