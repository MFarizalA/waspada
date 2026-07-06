"""Agent protocol — the lane-agnostic message envelope (WA-008).

Three contracts glue the orchestrator + pipeline agents together, and they
live here so WA-009 (pipeline agents) and WA-010 (orchestrator) cite the
same names verbatim:

  * :class:`AgentContext` — what an agent receives. Carries the decision
    ``lane`` (``"collections"`` | ``"origination"``; mirrors
    :mod:`waspada.config`), opaque data handles for inputs/outputs, and the
    ordered results of prior agents in the run (so a downstream agent can
    read its predecessor's artifact).
  * :class:`AgentResult` — what an agent returns. Carries a terminal
    :class:`Status`, an ``artifact_ref`` (a path / URI / handle string —
    never the blob itself), and freeform ``notes``.
  * :class:`Handoff` — the explicit envelope one agent passes to the next:
    ``from`` agent → ``to`` agent + the result + a rationale. The structured
    step log (:class:`Step`) is how runs are reconstructed for audit.

The data contract types (:class:`~waspada.schema.RawLoans`,
:class:`~waspada.schema.FeatureFrame`, :class:`~waspada.schema.ScoredAccounts`,
:class:`~waspada.schema.DashboardPayload`) flow as *artifacts* referenced by
``artifact_ref`` — agents do not inline them in the protocol, so the wire
envelope stays small and serializable.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
class Status:
    """Terminal states an :class:`AgentResult` may carry.

    Kept as plain string constants (not an Enum) so they serialize trivially
    into the step log and match the ``"approved"`` / ``"rejected"`` vocabulary
    of :class:`~waspada.agents.base.ApprovalGate`.
    """

    OK = "ok"            # agent produced its artifact
    BLOCKED = "blocked"  # agent could not proceed (e.g. approval rejected)
    ERROR = "error"      # agent raised / failed


# --------------------------------------------------------------------------- #
# AgentContext — what an agent receives on run(context)
# --------------------------------------------------------------------------- #
@dataclass
class AgentContext:
    """The bundle handed to ``Agent.run``.

    * ``lane`` — the decision lane (``COLLECTIONS`` | ``ORIGINATION``).
    * ``data_handles`` — opaque input/output handles (paths, URIs, table
      names). Untyped on purpose: a pipeline agent may carry a parquet path,
      an orchestrator a run id. Agents agree on keys out-of-band.
    * ``prior_results`` — ordered list of :class:`AgentResult` from earlier
      agents in the same run (empty for the first agent). A downstream agent
      reads ``prior_results[-1].artifact_ref`` to consume its predecessor's
      output.
    * ``meta`` — freeform per-run metadata (run id, as-of date, etc).
    """

    lane: str
    data_handles: Dict[str, Any] = field(default_factory=dict)
    prior_results: List["AgentResult"] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    def with_result(self, result: "AgentResult") -> "AgentContext":
        """Return a new context with ``result`` appended to ``prior_results``.

        Non-mutating: each agent in the chain sees a snapshot. The
        orchestrator (WA-010) threads these together.
        """
        return AgentContext(
            lane=self.lane,
            data_handles=dict(self.data_handles),
            prior_results=[*self.prior_results, result],
            meta=dict(self.meta),
        )


# --------------------------------------------------------------------------- #
# AgentResult — what an agent returns
# --------------------------------------------------------------------------- #
@dataclass
class AgentResult:
    """An agent's terminal output for one run.

    * ``status`` — one of :class:`Status`.
    * ``artifact_ref`` — a string handle (path / URI / key) to the artifact
      the agent produced. Never the artifact body — keeps the envelope small
      and lets large Arrow tables / model files stay on disk.
    * ``notes`` — short human-readable notes ( surfaced to the dashboard /
      audit log).
    * ``agent`` — name of the producing agent (filled by the base class).
    """

    status: str
    artifact_ref: Optional[str] = None
    notes: str = ""
    agent: str = ""

    @property
    def ok(self) -> bool:
        return self.status == Status.OK


# --------------------------------------------------------------------------- #
# Step + Handoff — the structured run log
# --------------------------------------------------------------------------- #
@dataclass
class Step:
    """One auditable step in a run (agent invocation or approval decision)."""

    agent: str
    action: str
    status: str
    at: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
    notes: str = ""
    rationale: Optional[str] = None
    auto: Optional[bool] = None  # True when an approval was auto-decided


@dataclass
class Handoff:
    """The explicit envelope one agent passes to the next.

    ``frm`` (not ``from`` — shadows a builtin) → ``to`` + the result + a
    rationale. The orchestrator (WA-010) emits one of these per hop; pipeline
    agents (WA-009) consume them.
    """

    frm: str
    to: str
    result: AgentResult
    rationale: str = ""
