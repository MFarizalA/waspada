"""Orchestrator agent (WA-010) — the primary agent.

Plans the Collections run, coordinates the four pipeline agents (WA-009) in
order, holds the **human approval gate** before the work-list is released, and
reports to the analyst in plain language. The agent the demo leads with.

Plan → run → report:

  * :meth:`Orchestrator.plan` — build the step sequence for a lane.
  * :meth:`Orchestrator.run` — execute ingest→analytics→risk-model→insight,
    threading artifacts via :class:`~waspada.agents.protocol.AgentContext`.
    A failure in any stage surfaces (not swallowed); a gate rejection
    short-circuits to a clear message.
  * :meth:`Orchestrator.report` — plain-language analyst summary (top risks,
    portfolio health, alert count).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..config import COLLECTIONS, LANES
from .analytics import AnalyticsAgent
from .base import Agent, ApprovalGate
from .ingest import IngestAgent
from .insight import InsightAgent
from .protocol import AgentContext, AgentResult, Handoff, Status
from .risk_auditor import RiskAuditorAgent
from .risk_model import RiskModelAgent

__all__ = ["Orchestrator", "COLLECTIONS_STEP_ORDER"]


# The canonical Collections-lane step order (agent names). The risk_auditor
# (WA-014, the Skeptic) runs AFTER the classical-ML model scores the book and
# BEFORE insight packages the payload — it audits the top-K riskiest accounts
# and opens Disputes where its view diverges from the model's band.
COLLECTIONS_STEP_ORDER = ("ingest", "analytics", "risk_model", "risk_auditor", "insight")


class Orchestrator(Agent):
    """The primary agent: plans, coordinates the four agents, reports."""

    name = "orchestrator"
    role = "plan, coordinate, and report the Collections run"

    def __init__(
        self,
        llm: Optional[Any] = None,
        *,
        gate: Optional[ApprovalGate] = None,
        as_of=None,
        top_n: int = 50,
        ingest_limit: Optional[int] = None,
        audit_k: int = 8,
    ) -> None:
        super().__init__(llm=llm)
        self.gate = gate or ApprovalGate()
        self.as_of = as_of
        self.top_n = top_n
        self.ingest_limit = ingest_limit
        self.audit_k = audit_k  # Skeptic audits top-K riskiest accounts
        self.handoffs: List[Handoff] = []
        self._steps_order: List[str] = []
        self._pipeline_agents: List[Agent] = []

    # ------------------------------------------------------------------ plan
    def plan(self, lane: str = COLLECTIONS) -> List[str]:
        """Return the ordered agent-name sequence for ``lane``.

        Only the Collections lane is wired (Origination is deferred per the
        HACKATHON sequencing). An unknown lane raises ``ValueError``.
        """
        if lane not in LANES:
            raise ValueError(f"lane={lane!r} invalid; must be one of {LANES}")
        if lane != COLLECTIONS:
            raise ValueError(
                f"lane={lane!r} orchestrator not implemented yet (Origination deferred)."
            )
        self._steps_order = list(COLLECTIONS_STEP_ORDER)
        self.step("plan", notes=f"lane={lane} steps={self._steps_order}")
        return list(self._steps_order)

    # ------------------------------------------------------------------- run
    def _build_agents(self) -> List[Agent]:
        """Construct the pipeline agents with shared config.

        Model tiering by cognitive load (HACKATHON.md § Judging rubric): the
        shared ``llm`` is the default tier; the Skeptic gets a cheaper
        ``qwen3.6-flash`` clone (it only challenges — one-shot JSON), and the
        rebuttal/arbiter tiers (plus/max) land in WA-016 via the same
        :meth:`~waspada.agents.llm.LLM.with_model` mechanism. Single-model
        brains (MockLLM) ignore the override and return ``self``.
        """
        # Skeptic challenges with flash — the cheapest tier sufficient for a
        # one-shot structured challenge. ``with_model`` is a no-op on MockLLM.
        auditor_brain = self.llm.with_model("qwen3.6-flash")
        return [
            IngestAgent(self.llm, limit=self.ingest_limit),
            AnalyticsAgent(self.llm, as_of=self.as_of),
            RiskModelAgent(self.llm),
            RiskAuditorAgent(auditor_brain, k=self.audit_k),
            InsightAgent(self.llm, gate=self.gate, top_n=self.top_n),
        ]

    def run(self, context: AgentContext) -> AgentResult:
        """Execute the planned sequence, threading artifacts via context.

        Returns the terminal :class:`AgentResult` (the insight agent's on
        success, or an ERROR/BLOCKED result on failure). A run where the
        Skeptic opened disputes completes with :class:`Status.DISPUTED`
        (a *completion*, not a failure — the pipeline still produces its
        payload; the disputes are flagged for the human gate). Each hop is
        recorded as a :class:`Handoff` for audit.
        """
        if not self._steps_order:
            self.plan(context.lane)
        self.step("run_start", notes=f"lane={context.lane}")

        agents = self._build_agents()
        self._pipeline_agents = agents  # surfaced for the API audit trail
        ctx = context
        last: Optional[AgentResult] = None
        prev_agent: Optional[Agent] = None

        for i, agent in enumerate(agents):
            try:
                res = agent.run(ctx)
            except Exception as exc:  # pragma: no cover - defensive
                self.step("run", status=Status.ERROR, notes=f"{agent.name} raised: {exc}")
                return AgentResult(
                    status=Status.ERROR, agent=self.name,
                    notes=f"{agent.name} raised: {exc}", artifact_ref=last.artifact_ref if last else None,
                )

            # Record the handoff (frm→to) for audit.
            rationale = res.notes
            if prev_agent is not None:
                self.handoffs.append(Handoff(
                    frm=prev_agent.name, to=agent.name, result=res, rationale=rationale,
                ))
            prev_agent = agent

            # ERROR / BLOCKED are failures → short-circuit. DISPUTED is a
            # *completion* with live disputes: it is NOT a failure — the
            # terminal agent (insight) still produced a payload. We keep
            # stepping here only because insight is last; if a non-terminal
            # agent ever returns DISPUTED we treat it like OK for flow control
            # (the status is re-surfaced from the final result below).
            if res.status in (Status.ERROR, Status.BLOCKED):
                self.step(
                    "run", status=res.status,
                    notes=f"{agent.name} did not produce artifact: {res.notes}",
                )
                return AgentResult(
                    status=res.status, agent=self.name,
                    notes=f"pipeline halted at {agent.name}: {res.notes}",
                    artifact_ref=res.artifact_ref,
                )

            last = res
            ctx = ctx.with_result(res)
            self.step("run", notes=f"{agent.name} → {res.artifact_ref}")

        # Stash the final payload on the context for the CLI / report.
        payload_handle = last.artifact_ref if last else None
        self._final_ctx = ctx
        # Terminal status mirrors the last agent's: DISPUTED if disputes were
        # opened, OK otherwise. Both are completions (a payload exists).
        terminal = last.status if last is not None else Status.OK
        self.step("run_done", status=terminal, notes=f"payload={payload_handle} status={terminal}")
        return AgentResult(
            status=terminal, agent=self.name,
            artifact_ref=payload_handle,
            notes=("orchestrated run complete" if terminal != Status.DISPUTED
                   else f"orchestrated run complete with {len(ctx.data_handles.get('risk_disputes') or [])} open dispute(s)"),
        )

    # --------------------------------------------------------------- report
    def report(self, payload: Dict[str, Any]) -> str:
        """Plain-language analyst summary (top risks, health, alert count)."""
        work_list = payload.get("work_list", []) or []
        health = payload.get("portfolio_health", {}) or {}
        alert_list = payload.get("alerts", []) or []
        npl = float(health.get("npl_ratio", 0.0))
        vintage = health.get("vintage_default_rate", {}) or {}

        top = work_list[:3] if work_list else []
        top_desc = ", ".join(
            f"{r.get('loan_id', '?')} (p={float(r.get('p_default', 0.0)):.2f}, {r.get('recommended_action', '?')})"
            for r in top
        ) or "none"

        # Worst vintage by default rate (if any).
        worst_vintage = ""
        if vintage:
            wy, wr = max(vintage.items(), key=lambda kv: kv[1])
            worst_vintage = f" Worst vintage: {wy} at {float(wr):.1%} default."

        lines = [
            f"WASPADA Collections run — {len(work_list)} accounts on the work-list.",
            f"Top risks: {top_desc}.",
            f"Portfolio NPL ratio: {npl:.1%}.{worst_vintage}",
            f"Alerts: {len(alert_list)}.",
        ]
        return " ".join(lines)
