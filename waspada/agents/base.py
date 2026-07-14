"""Agent base + the human-in-loop approval gate (WA-008).

The substrate every pipeline (WA-009) and orchestrator (WA-010) agent is
built from:

  * :class:`Agent` — abstract base. Each agent has a ``name``/``role``, a
    ``tools`` registry (string keys → callables), and a structured
    ``run(context) -> AgentResult`` it must implement. The base records a
    :class:`~waspada.agents.protocol.Step` per run for audit.
  * :class:`ApprovalGate` — the human-in-loop checkpoint.
    ``request(action, rationale)`` returns an :class:`Approved` or
    :class:`Rejected` decision. ``WASPADA_AUTO_APPROVE=1`` (the non-prod
    default path) short-circuits to approve *but* logs it distinctly
    (``auto=True``), so an audit can tell a rubber-stamp from a real human
    sign-off. This is the "humans in control" rubric requirement.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .llm import LLM, MockLLM, get_llm
from .protocol import AgentContext, AgentResult, Handoff, Status, Step


# --------------------------------------------------------------------------- #
# Decision outcomes from the approval gate
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Approved:
    """The gate approved the action. ``auto=True`` means it was auto-approved."""

    action: str
    rationale: str
    auto: bool = False


@dataclass(frozen=True)
class Rejected:
    """The gate rejected the action. ``auto=True`` means auto-rejected."""

    action: str
    rationale: str
    reason: str = ""  # human/legateer's reason for rejection
    auto: bool = False


# --------------------------------------------------------------------------- #
# Agent base
# --------------------------------------------------------------------------- #
class Agent(ABC):
    """Base class for every WASPADA agent.

    Subclasses set ``name`` and ``role`` and implement :meth:`run`. The base
    wires the shared machinery:

    * a ``tools`` registry (string key → callable) an agent can populate and
      call; the registry is plain and unopinionated so a pipeline agent can
      register a cuML call while an orchestrator agent registers an OSS read.
    * a per-agent ``llm`` (defaults to a :class:`MockLLM` — the framework
      runs offline by default; WA-009/010 inject a real brain when needed).
    * an ``steps`` audit log: each :meth:`_run` records a :class:`Step`.
    """

    name: str = "agent"
    role: str = ""

    def __init__(self, llm: Optional[LLM] = None) -> None:
        self.llm: LLM = llm if llm is not None else MockLLM()
        self.tools: Dict[str, Callable[..., Any]] = {}
        self.steps: List[Step] = []

    def register_tool(self, key: str, fn: Callable[..., Any]) -> None:
        """Register a callable under ``key``. Overwrites silently by design."""
        self.tools[key] = fn

    @abstractmethod
    def run(self, context: AgentContext) -> AgentResult:
        """Produce an :class:`AgentResult` from ``context``.

        Implementations should call :meth:`step` to record auditable steps
        and return a terminal :class:`AgentResult` (see :class:`Status`).
        """
        raise NotImplementedError

    def step(self, action: str, *, status: str = Status.OK, notes: str = "",
             rationale: Optional[str] = None, auto: Optional[bool] = None) -> Step:
        """Record one auditable :class:`Step` and return it."""
        s = Step(agent=self.name, action=action, status=status, notes=notes,
                 rationale=rationale, auto=auto)
        self.steps.append(s)
        return s


# --------------------------------------------------------------------------- #
# ApprovalGate — humans in control
# --------------------------------------------------------------------------- #
class ApprovalGate:
    """The human-in-loop checkpoint.

    Call :meth:`request` with the action an agent wants to take and the
    rationale for it. The gate returns :class:`Approved` or :class:`Rejected`.

    Two decision modes:

    * **interactive** (default, ``auto_approve=False``) — a ``decide``
      callable decides approve/reject. In production this is the human
      reviewer (a webhook / UI callback in WA-010); in tests it's an
      injected stub. ``decide(action, rationale) -> Approved|Rejected``.
    * **auto-approve** (``auto_approve=True``, or ``WASPADA_AUTO_APPROVE=1``)
      — the gate short-circuits to approve **without** calling ``decide``,
      and the recorded :class:`Step` carries ``auto=True`` so an audit can
      distinguish a rubber-stamp from a real human sign-off. Intended for
      non-prod / smoke runs.

    Every decision (manual or auto) is appended to :attr:`steps` as a
    :class:`Step` for audit.
    """

    def __init__(
        self,
        *,
        decide: Optional[Callable[[str, str], Any]] = None,
        auto_approve: Optional[bool] = None,
    ) -> None:
        # Env override: WASPADA_AUTO_APPROVE=1 forces auto-approve. Default off
        # (production-safe: the gate blocks unless a human says yes).
        if auto_approve is None:
            auto_approve = os.environ.get("WASPADA_AUTO_APPROVE", "").strip() in ("1", "true", "yes")
        self.auto_approve = bool(auto_approve)
        self._decide = decide
        self.steps: List[Step] = []

    def request(self, action: str, rationale: str) -> Any:
        """Ask the gate to approve/reject ``action``.

        Returns :class:`Approved` or :class:`Rejected` and logs a
        :class:`Step`. On auto-approve the step is flagged ``auto=True``.
        """
        if self.auto_approve:
            decision = Approved(action=action, rationale=rationale, auto=True)
        else:
            if self._decide is None:
                # No human channel wired — fail safe (block) rather than guess.
                decision = Rejected(
                    action=action, rationale=rationale,
                    reason="no decide channel wired; gate cannot ask a human",
                    auto=True,
                )
            else:
                decision = self._decide(action, rationale)

        if isinstance(decision, Approved):
            self._log(action, rationale, Status.OK, decision.auto)
        else:
            self._log(action, rationale, Status.BLOCKED, decision.auto,
                      reason=getattr(decision, "reason", ""))
        return decision

    def _log(self, action: str, rationale: str, status: str, auto: bool,
             reason: str = "") -> None:
        self.steps.append(Step(
            agent="ApprovalGate",
            action=action,
            status=status,
            rationale=rationale,
            auto=auto,
            notes=reason,
        ))


# --------------------------------------------------------------------------- #
# Handoff helper — the explicit from→to envelope
# --------------------------------------------------------------------------- #
def handoff(frm: Agent, to: "Agent", result: AgentResult, rationale: str = "") -> Handoff:
    """Build a :class:`Handoff` envelope from one agent to the next.

    The orchestrator (WA-010) records these to reconstruct the run; pipeline
    agents (WA-009) consume them. ``Handoff.frm`` shadows nothing (``from``
    is a keyword).
    """
    return Handoff(frm=frm.name, to=to.name, result=result, rationale=rationale)
