"""Orchestrator agent (WA-010 + WA-016) — the primary agent.

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

WA-016 adds the **3-round dispute resolution** between the auditor (Round 1)
and insight (serialization): after the Skeptic opens disputes the orchestrator
runs the Actuary's rebuttal (Round 2) and, if it upholds, the Arbiter's ruling
(Round 3), closing each dispute with one of the four terminal resolutions
(``upheld`` / ``overridden`` / ``escalated_approved`` / ``escalated_rejected``).
A concession or an arbiter ``override`` short-circuits Round 3; an arbiter
``escalate`` (or any unparsable turn) routes to the human gate.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pyarrow as pa

from ..config import COLLECTIONS, LANES
from .analytics import AnalyticsAgent
from .arbiter import ArbiterAgent
from .base import Agent, ApprovalGate, Approved
from .ingest import IngestAgent
from .insight import InsightAgent
from .protocol import AgentContext, AgentResult, Dispute, Handoff, Status
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
        arbiter: Optional[ArbiterAgent] = None,
        enable_arbiter: bool = True,
    ) -> None:
        super().__init__(llm=llm)
        self.gate = gate or ApprovalGate()
        self.as_of = as_of
        self.top_n = top_n
        self.ingest_limit = ingest_limit
        self.audit_k = audit_k  # Skeptic audits top-K riskiest accounts
        # The Arbiter (Round 3). Defaults to an ArbiterAgent sharing this
        # orchestrator's brain (the orchestrator tiers it to qwen3.7-max
        # via with_model). Pass ``arbiter=`` to inject a custom one for tests.
        # Pass ``enable_arbiter=False`` to take the CUT LINE (upheld rebuttal
        # → straight to gate, no Round 3) — documented in WA-016.
        self.arbiter: Optional[ArbiterAgent] = arbiter
        self.enable_arbiter = bool(enable_arbiter)
        self.handoffs: List[Handoff] = []
        self._steps_order: List[str] = []
        self._pipeline_agents: List[Agent] = []
        # Agents used in the post-audit dispute resolution (exposed for audit
        # / tests). Set during run().
        self._risk_model_agent: Optional[RiskModelAgent] = None
        self._arbiter_agent: Optional[ArbiterAgent] = None
        # Counts for the run report / tests.
        self._resolution_counts: Dict[str, int] = {}

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
        risk_model = RiskModelAgent(self.llm)
        # Remember the risk-model agent so the dispute-resolution step can
        # call its defend_score() (the Actuary speaks Round 2).
        self._risk_model_agent = risk_model
        if self.arbiter is not None:
            self._arbiter_agent = self.arbiter
        elif self.enable_arbiter:
            self._arbiter_agent = ArbiterAgent(self.llm)
        else:
            self._arbiter_agent = None
        return [
            IngestAgent(self.llm, limit=self.ingest_limit),
            AnalyticsAgent(self.llm, as_of=self.as_of),
            risk_model,
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

        WA-016: between the auditor (Round 1) and insight, the orchestrator
        resolves every open dispute through the 3-round debate (rebuttal +
        arbiter + gate) so insight serializes *closed* disputes with their
        terminal ``resolution`` set.
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

            # After the auditor opens disputes (and before insight consumes
            # them), run the 3-round debate resolution. This is the WA-016
            # seam: the dispute list is live on data_handles["risk_disputes"]
            # but their resolutions are still open ("").
            if agent.name == "risk_auditor" and res.ok:
                self._resolve_disputes(ctx)

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
                # Expose the last context for audit/tests even on a halted run
                # (e.g. a gate rejecting the work-list release) — the disputes
                # resolved upstream are still on ``ctx`` and worth inspecting.
                self._final_ctx = ctx
                return AgentResult(
                    status=res.status, agent=self.name,
                    notes=f"pipeline halted at {agent.name}: {res.notes}",
                    artifact_ref=res.artifact_ref,
                )

            last = res
            ctx = ctx.with_result(res)
            self.step("run", notes=f"{agent.name} → {res.artifact_ref}")
            prev_agent = agent  # advance so the next iteration records the handoff

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

    # ----------------------------------------------- dispute resolution (WA-016)
    def _resolve_disputes(self, ctx: AgentContext) -> None:
        """Run the 3-round debate on every open dispute the auditor opened.

        For each dispute: Round 2 (Actuary rebuttal) → if concede, close as
        ``overridden``; if uphold, Round 3 (Arbiter) → ``upheld`` /
        ``overridden`` / escalate-to-gate. Unparsable rebuttal or ruling, or
        arbiter low-confidence, routes to the human gate
        (``escalated_approved`` / ``escalated_rejected``). Mutates each
        :class:`Dispute` in place (appends rounds, sets resolution/
        resolved_by/rationale).
        """
        disputes: List[Dispute] = list(ctx.data_handles.get("risk_disputes") or [])
        if not disputes:
            return
        scored = self._table(ctx, "scored_accounts")
        features = self._table(ctx, "feature_frame")
        counts: Dict[str, int] = {"upheld": 0, "overridden": 0,
                                  "escalated_approved": 0, "escalated_rejected": 0}
        escalations: List[Dispute] = []

        for d in disputes:
            # --- Round 2: the Actuary rebuts. ---
            r2 = self._risk_model_agent.defend_score(d, scored, features)
            d.rounds.append(r2)
            verdict = self._rebuttal_verdict(r2.claim)
            if verdict == "concede":
                # Concession closes the dispute — auditor's critique wins.
                d.resolution = "overridden"
                d.resolved_by = "risk_model"
                d.rationale = r2.claim.split(":", 1)[-1].strip() or r2.claim
                counts["overridden"] += 1
                self.step("dispute_resolved",
                          notes=f"{d.loan_id} overridden (risk_model conceded)")
                continue
            if verdict == "unparsable":
                # Unparsable rebuttal → safe degrade to the gate.
                escalations.append(d)
                continue
            # verdict == "uphold" → proceed to Round 3 (Arbiter), unless cut.
            if self._arbiter_agent is None:
                # CUT LINE: upheld rebuttal escalates straight to the gate.
                escalations.append(d)
                continue

            # --- Round 3: the Arbiter rules. ---
            ruling, rationale, _conf, r3 = self._arbiter_agent.rule(d)
            d.rounds.append(r3)
            if ruling == "uphold":
                d.resolution = "upheld"
                d.resolved_by = "arbiter"
                d.rationale = rationale
                counts["upheld"] += 1
                self.step("dispute_resolved",
                          notes=f"{d.loan_id} upheld (arbiter)")
            elif ruling == "override":
                d.resolution = "overridden"
                d.resolved_by = "arbiter"
                d.rationale = rationale
                counts["overridden"] += 1
                self.step("dispute_resolved",
                          notes=f"{d.loan_id} overridden (arbiter)")
            else:  # escalate
                escalations.append(d)

        # --- Escalations → human gate. ---
        if escalations:
            self._route_escalations(escalations, ctx, counts)

        self._resolution_counts = counts
        self.step(
            "dispute_resolution_done",
            notes=(f"upheld={counts['upheld']} overridden={counts['overridden']} "
                   f"escalated_approved={counts['escalated_approved']} "
                   f"escalated_rejected={counts['escalated_rejected']} "
                   f"of {len(disputes)}"),
        )

    def _route_escalations(
        self, escalations: List[Dispute], ctx: AgentContext, counts: Dict[str, int],
    ) -> None:
        """Send each escalated dispute to the human gate and close it.

        The gate's ``resolve_risk_dispute`` action returns
        :class:`Approved` (→ ``escalated_approved``) or
        :class:`~waspada.agents.base.Rejected` (→ ``escalated_rejected``).
        The terminal ``resolved_by`` is ``"human"``. The insight agent will
        re-request ``resolve_risk_dispute`` for any still-open disputes, but
        WA-016 closes them here so the transcript + resolution are complete
        before serialization.
        """
        decision = self.gate.request(
            "resolve_risk_dispute",
            rationale=f"{len(escalations)} dispute(s) escalated for a human ruling",
        )
        approved = isinstance(decision, Approved)
        outcome = "escalated_approved" if approved else "escalated_rejected"
        self.step(
            "escalation_gate",
            notes=f"resolve_risk_dispute {'approved' if approved else 'rejected'} "
                  f"(auto={getattr(decision, 'auto', False)}) for {len(escalations)} dispute(s)",
        )
        for d in escalations:
            d.resolution = outcome
            d.resolved_by = "human"
            d.rationale = (d.rationale or "") + f" escalated; gate {outcome}."
            counts[outcome] += 1

    @staticmethod
    def _rebuttal_verdict(claim: str) -> str:
        """Extract the Round-2 verdict token embedded in the Actuary's claim.

        The Actuary prefixes its claim with ``UPHOLD:`` / ``CONCEDE:`` /
        ``UNPARSABLE:``. Returns the lower-cased verdict (``uphold`` /
        ``concede``) or ``"unparsable"``.
        """
        head = (claim or "").strip().split(":", 1)[0].strip().lower()
        if head == "uphold":
            return "uphold"
        if head == "concede":
            return "concede"
        return "unparsable"

    @staticmethod
    def _table(ctx: AgentContext, handle: str) -> Optional[pa.Table]:
        tbl = ctx.data_handles.get(handle)
        return tbl if isinstance(tbl, pa.Table) else None

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
