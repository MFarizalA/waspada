"""Insight agent (WA-009) — work-list, portfolio health, alerts, summary.

Wraps :mod:`waspada.insight.ranking`. Reads the ScoredAccounts the risk-model
agent published, builds the ranked work-list + portfolio health + alerts, and
assembles the :class:`~waspada.schema.DashboardPayload`. Calls the
:class:`~waspada.agents.base.ApprovalGate` before publishing the work-list
(humans in control). Always emits ≥1 human-readable alert string.
"""
from __future__ import annotations

from typing import Any, Optional

import pyarrow as pa

from ..insight.ranking import (
    alerts as _alerts,
    rank as _rank,
    segment_health as _segment_health,
    summarize_alerts,
    to_dashboard_payload,
)
from .base import Agent, ApprovalGate, Approved
from .protocol import AgentContext, AgentResult, Status

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
    ) -> None:
        super().__init__(llm=llm)
        self.gate = gate or ApprovalGate()
        self.top_n = top_n

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

        # 5. Assemble the payload + the always-present alert summary string.
        payload = to_dashboard_payload(work_list, health, alert_list)
        summary = summarize_alerts(alert_list)
        self.step("payload_assembled", notes=f"alerts_summary='{summary[:60]}'")

        handle = "dashboard_payload"
        context.data_handles[handle] = payload
        context.data_handles["alert_summary"] = summary
        return AgentResult(
            status=Status.OK, agent=self.name, artifact_ref=handle,
            notes=f"payload ready; alerts={len(alert_list)}; '{summary}'",
        )
