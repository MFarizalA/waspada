"""WASPADA agent framework — the shared multi-agent substrate (WA-008).

Lane-agnostic substrate both decision lanes build on:

  * :class:`Agent` — base class (name/role, tools registry, step log).
  * :class:`ApprovalGate` + :class:`Approved` / :class:`Rejected` — the
    human-in-loop checkpoint, with auto-approve logged distinctly.
  * :class:`MockLLM` / :class:`GeminiLLM` / :class:`QwenLLM` / :func:`get_llm`
    — the mockable reasoning surface (default brain is offline/deterministic).
  * Protocol: :class:`AgentContext`, :class:`AgentResult`, :class:`Handoff`,
    :class:`Step`, :class:`Status`.
  * :class:`ArbiterAgent` — the Round-3 ruling agent (WA-016).

WA-009 (pipeline agents) and WA-010 (orchestrator) cite these names
verbatim. See :mod:`waspada.agents.protocol` for the wire contract.
"""
from __future__ import annotations

from .arbiter import ArbiterAgent
from .base import Agent, ApprovalGate, Approved, Rejected, handoff
from .data_analyst import DataAnalystAgent
from .data_engineer import DataEngineerAgent
from .dispute_memory import (
    DisputeMemory,
    InMemoryMemory,
    LocalFileMemory,
    MemoryBackend,
)
from .ingest import IngestAgent
from .insight import InsightAgent
from .llm import GeminiLLM, LLM, MockLLM, QwenLLM, get_llm
from .protocol import AgentContext, AgentResult, Dispute, DisputeRound, Handoff, Status, Step
from .risk_auditor import RiskAuditorAgent

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
    "QwenLLM",
    "get_llm",
    # protocol
    "AgentContext",
    "AgentResult",
    "Dispute",
    "DisputeRound",
    "Handoff",
    "Step",
    "Status",
    # memory (WA-026)
    "DisputeMemory",
    "MemoryBackend",
    "InMemoryMemory",
    "LocalFileMemory",
    # agents
    "ArbiterAgent",
    "DataAnalystAgent",
    "DataEngineerAgent",
    "IngestAgent",
    "InsightAgent",
    "RiskAuditorAgent",
]
