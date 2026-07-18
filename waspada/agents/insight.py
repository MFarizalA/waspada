"""Insight agent (WA-009 + WA-014 + WA-016) — work-list, portfolio health, alerts, summary.

Wraps :mod:`waspada.insight.ranking`. Reads the ScoredAccounts the risk-model
agent published, builds the ranked work-list + portfolio health + alerts, and
assembles the :class:`~waspada.schema.DashboardPayload`. Calls the
:class:`~waspada.agents.base.ApprovalGate` before publishing the work-list
(humans in control). Always emits ≥1 human-readable alert string.

WA-014 added the dispute wiring: when the upstream :class:`RiskAuditorAgent`
opened any :class:`~waspada.agents.protocol.Dispute`, they are serialized into
``payload["agent_dialogue"]`` (the frozen shape) and the agent additionally
requests the ``resolve_risk_dispute`` gate action (distinct in the audit log
from ``publish_work_list``). A run with open disputes returns
:class:`~waspada.agents.protocol.Status.DISPUTED` so the orchestrator routes
the run accordingly.

WA-016 refinement: the orchestrator now closes every dispute (3-round debate)
BEFORE insight runs. Insight therefore only requests ``resolve_risk_dispute``
for genuinely OPEN disputes (those still carrying an empty ``resolution``) —
closed disputes are serialized as-is into ``agent_dialogue`` without a
redundant gate call. A caller running insight standalone with open disputes
(the pre-WA-016 path) still gates them.
"""
from __future__ import annotations

from typing import Any, List, Optional

import pyarrow as pa

from ..insight.ranking import (
    alerts as _alerts,
    rank as _rank,
    segment_health as _segment_health,
    summarize_alerts,
    to_dashboard_payload,
)
from ..policy import RiskPolicy
from .base import Agent, ApprovalGate, Approved
from .protocol import AgentContext, AgentResult, Dispute, Status

__all__ = ["InsightAgent"]


class InsightAgent(Agent):
    """Build the dashboard payload + alert summary from scored accounts."""

    name = "insight"
    role = "rank, segment, alert, and assemble the dashboard payload"

    def __init__(
        self,
        llm: Optional[Any] = None,
        *,
        gate: Optional[ApprovalGate] = None,
        top_n: int = 50,
        policy: Optional["RiskPolicy"] = None,
    ) -> None:
        super().__init__(llm=llm)
        self.gate = gate or ApprovalGate()
        self.top_n = top_n
        # WA-032: the human-configurable decision matrix. ``None`` → the module
        # constants (behaviour unchanged); a RiskPolicy overrides the band→action
        # map, alert thresholds, and NPL buckets from a committed JSON file.
        self.policy = policy

    def run(self, context: AgentContext) -> AgentResult:
        if not context.prior_results:
            self.step("rank", status=Status.ERROR, notes="no predecessor")
            return AgentResult(status=Status.ERROR, agent=self.name, notes="no ScoredAccounts input")
        scored_handle = context.prior_results[-1].artifact_ref
        scored: Optional[pa.Table] = context.data_handles.get(scored_handle) if scored_handle else None
        if scored is None:
            self.step("rank", status=Status.ERROR, notes=f"handle {scored_handle!r} missing")
            return AgentResult(
                status=Status.ERROR, agent=self.name,
                notes=f"ScoredAccounts handle {scored_handle!r} not found",
            )

        # 1. Rank + 2. segment health + 3. alerts (pure-CPU insight layer).
        # WA-032: apply the RiskPolicy when one is wired; otherwise the calls use
        # their module-constant defaults (byte-identical to the pre-policy path).
        pol = self.policy
        if pol is not None:
            work_list = _rank(scored, top_n=self.top_n, action_by_band=pol.band_to_action)
            health = _segment_health(scored, npl_buckets=pol.npl_buckets)
            alert_list = _alerts(health, npl_threshold=pol.npl_threshold,
                                 vintage_threshold=pol.vintage_threshold)
        else:
            work_list = _rank(scored, top_n=self.top_n)
            health = _segment_health(scored)
            alert_list = _alerts(health)
        self.step(
            "build_insight",
            notes=f"work_list={len(work_list)} alerts={len(alert_list)} npl={health['npl_ratio']:.3f}",
        )

        # 4. Human approval BEFORE the work-list is released (humans in control).
        decision = self.gate.request(
            "publish_work_list",
            rationale=f"{len(work_list)} accounts queued; top p={work_list[0]['p_default']:.2f}" if work_list else "empty work-list",
        )
        if not isinstance(decision, Approved):
            self.step("approval_gate", status=Status.BLOCKED, notes="work-list release rejected")
            return AgentResult(
                status=Status.BLOCKED, agent=self.name,
                notes="work-list release rejected by approval gate",
            )
        self.step("approval_gate", notes=f"work-list approved (auto={decision.auto})")

        # 5. Serialize all disputes (open or closed) into agent_dialogue.
        disputes: List[Dispute] = list(context.data_handles.get("risk_disputes") or [])
        dialogue = [d.to_dict() for d in disputes]

        # 6. Gate only the genuinely OPEN disputes — those still carrying an
        #    empty ``resolution`` (WA-016 closes disputes in the orchestrator
        #    before insight runs; a standalone insight call with open disputes
        #    still gates them). Closed disputes are already decided; re-gating
        #    them would be a redundant audit-log entry.
        open_disputes = [d for d in disputes if not d.resolution]
        if open_disputes:
            disp_decision = self.gate.request(
                "resolve_risk_dispute",
                rationale=f"{len(open_disputes)} open dispute(s) need a human ruling",
            )
            self.step(
                "dispute_gate",
                notes=f"resolve_risk_dispute {'approved' if isinstance(disp_decision, Approved) else 'left open'} (auto={getattr(disp_decision, 'auto', False)})",
            )
        elif disputes:
            n_closed = len(disputes)
            self.step(
                "dispute_gate",
                notes=f"{n_closed} dispute(s) already resolved upstream; no gate call",
            )

        # 7. Assemble the payload + the always-present alert summary string.
        payload = to_dashboard_payload(work_list, health, alert_list)
        if dialogue:
            payload["agent_dialogue"] = dialogue
        summary = summarize_alerts(alert_list)
        self.step("payload_assembled", notes=f"alerts_summary='{summary[:60]}' disputes={len(disputes)}")

        handle = "dashboard_payload"
        context.data_handles[handle] = payload
        context.data_handles["alert_summary"] = summary
        # A run with disputes is DISPUTED (the orchestrator routes it);
        # otherwise OK. WA-016 closes disputes but they remain on the
        # transcript (agent_dialogue) so the run is still flagged DISPUTED
        # for routing — the payload carries the closed negotiation record.
        terminal = Status.DISPUTED if disputes else Status.OK
        return AgentResult(
            status=terminal, agent=self.name, artifact_ref=handle,
            notes=f"payload ready; alerts={len(alert_list)}; disputes={len(disputes)}; '{summary}'",
        )
