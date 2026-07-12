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

WA-026 adds **cross-run dispute memory**: before opening a debate the
orchestrator consults :class:`~waspada.agents.dispute_memory.DisputeMemory`. A
prior HUMAN ruling on the same loan short-circuits the debate (the prior
resolution is reused, no LLM calls spent); any other prior ruling is injected
as context for the Arbiter/Skeptic. Resolved disputes are persisted after the
run so the next run of the same book spends measurably fewer calls (the second
headline efficiency axis). This is decision consistency / institutional memory,
NOT self-improvement.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

import pyarrow as pa

from ..config import COLLECTIONS, LANES
from .analytics import AnalyticsAgent
from .arbiter import ArbiterAgent
from .data_analyst import DataAnalystAgent
from .base import Agent, ApprovalGate, Approved
from .data_engineer import DataEngineerAgent
from .dispute_memory import DisputeMemory, MemoryBackend
from .ingest import IngestAgent
from .insight import InsightAgent
from .protocol import AgentContext, AgentResult, Dispute, DisputeRound, Handoff, Status
from .risk_auditor import RiskAuditorAgent
from .risk_model import RiskModelAgent

__all__ = ["Orchestrator", "COLLECTIONS_STEP_ORDER"]


# The canonical Collections-lane step order (agent names). WA-029 promotes the
# deterministic ingest step into a Tier-2 Data Engineer agent (``data_engineer``)
# that runs the same freshness/schema gate INSIDE it, then adds a qwen3.6-flash
# function-calling reasoning loop over data quality. The risk_auditor
# (WA-014, the Skeptic) runs AFTER the classical-ML model scores the book and
# BEFORE insight packages the payload — it audits the top-K riskiest accounts
# and opens Disputes where its view diverges from the model's band.
COLLECTIONS_STEP_ORDER = ("data_engineer", "data_analyst", "risk_model", "risk_auditor", "insight")


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
        memory: Optional[DisputeMemory] = None,
        memory_backend: Optional[MemoryBackend] = None,
        on_round_complete: Optional[Callable[["Dispute", "DisputeRound"], None]] = None,
        on_dispute_resolved: Optional[Callable[["Dispute"], None]] = None,
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
        # WA-026: cross-run dispute memory. Accept either a fully-built
        # :class:`DisputeMemory` (tests inject an InMemory one) or a bare
        # backend (the API wires a LocalFileMemory). ``None`` defaults to an
        # in-process memory so an unconfigured orchestrator still runs — the
        # memory just never persists across processes. NOTE: use ``is not
        # None`` (not truthiness) — DisputeMemory defines __len__, so an empty
        # memory is falsy and ``memory or ...`` would discard it.
        self.memory: DisputeMemory = (
            memory if memory is not None else DisputeMemory(memory_backend))
        # WA-022: streaming hooks (default None → no behavior change). The
        # /api/run/stream SSE endpoint sets these so each debate round /
        # terminal resolution is emitted as a Server-Sent Event as it happens.
        # Both callbacks receive the in-progress Dispute (mutated in place —
        # read but don't mutate). Kept off the hot path: a None callback is a
        # pure no-op, so every existing run is byte-for-byte unchanged.
        self.on_round_complete = on_round_complete
        self.on_dispute_resolved = on_dispute_resolved

    def _emit_round(self, d: Dispute, r: DisputeRound) -> None:
        """Fire ``on_round_complete`` if wired (no-op otherwise)."""
        cb = self.on_round_complete
        if cb is None:
            return
        try:
            cb(d, r)
        except Exception as exc:  # a stream hook never fails the run
            self.step("stream_hook_error", status=Status.ERROR,
                      notes=f"on_round_complete raised: {exc}")

    def _emit_resolved(self, d: Dispute) -> None:
        """Fire ``on_dispute_resolved`` if wired (no-op otherwise)."""
        cb = self.on_dispute_resolved
        if cb is None:
            return
        try:
            cb(d)
        except Exception as exc:  # a stream hook never fails the run
            self.step("stream_hook_error", status=Status.ERROR,
                      notes=f"on_dispute_resolved raised: {exc}")

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
        # Data Engineer (WA-029) also reasons on flash — same cheap tier, a
        # function-calling loop over data quality. Tiered separately from the
        # debate brain so a shared MockLLM script isn't consumed by the DE's
        # loop (tests inject a fresh MockLLM for the DE; production gets a
        # flash clone sharing the Qwen client).
        de_brain = self.llm.with_model("qwen3.6-flash")
        # Data Analyst (WA-030) reasons on qwen3.7-plus — a function-calling
        # loop over DuckDB SQL explorations. The deterministic FeatureFrame is
        # still built by build_features() inside the agent.
        da_brain = self.llm.with_model("qwen3.7-plus")
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
            DataEngineerAgent(de_brain, limit=self.ingest_limit),
            DataAnalystAgent(da_brain, as_of=self.as_of),
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
        # WA-026: persist the cross-run dispute memory now that every dispute
        # has been resolved + recorded. Best-effort: a persist failure never
        # fails the run (the memory is an accelerator, not a correctness
        # dependency) — surface it as an audit step instead.
        try:
            self.memory.persist()
        except Exception as exc:  # pragma: no cover - backend-dependent
            self.step("memory_persist_error", status=Status.ERROR,
                      notes=f"dispute memory persist failed: {exc}")
        else:
            self.step("memory_persisted",
                      notes=f"dispute memory now holds {self.memory.size} account(s)")
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

        WA-026: BEFORE opening a debate, the cross-run memory is consulted. A
        prior **human** ruling on the same loan short-circuits the debate (the
        prior resolution is reused, no LLM calls spent) — the strongest
        precedent. Any other prior ruling (arbiter/model) is injected as
        context so the Arbiter/Skeptic see precedent; the debate still runs
        (the memory INFORMS, it never silences — the demo keeps showing
        disputes). After resolution, every freshly-settled dispute is recorded
        to the memory so the next run sees it.

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
        counts: Dict[str, int] = {key: 0 for key in (
            "upheld", "overridden", "escalated_approved", "escalated_rejected")}
        escalations: List[Dispute] = []
        # WA-026: per-run efficiency bookkeeping (surfaced in the report).
        self.memory.reset_counters()

        for d in disputes:
            # --- WA-022: stream the auditor's Round-1 challenge (always present;
            # it's what opened the dispute). Emitted before the memory check so
            # even a short-circuited dispute shows why it was opened. ---
            if d.rounds:
                self._emit_round(d, d.rounds[0])

            # --- WA-026: consult cross-run memory BEFORE debating. ---
            recalled = self.memory.short_circuit(d)
            if recalled is not None:
                # Human precedent → reuse the prior ruling, skip the debate.
                self._apply_recalled(d, recalled, counts)
                continue
            # Non-short-circuiting precedent (arbiter/model, or none) → inject
            # as context so the debate sees it, then run the debate normally.
            self._inject_precedent(d)

            # --- Round 2: the Actuary rebuts. ---
            r2 = self._risk_model_agent.defend_score(d, scored, features)
            d.rounds.append(r2)
            self._emit_round(d, r2)
            verdict = self._rebuttal_verdict(r2.claim)
            if verdict == "concede":
                # Concession closes the dispute — auditor's critique wins.
                d.resolution = "overridden"
                d.resolved_by = "risk_model"
                d.rationale = r2.claim.split(":", 1)[-1].strip() or r2.claim
                counts["overridden"] += 1
                self.step("dispute_resolved",
                          notes=f"{d.loan_id} overridden (risk_model conceded)")
                self._emit_resolved(d)
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
            self._emit_round(d, r3)
            if ruling == "uphold":
                d.resolution = "upheld"
                d.resolved_by = "arbiter"
                d.rationale = rationale
                counts["upheld"] += 1
                self.step("dispute_resolved",
                          notes=f"{d.loan_id} upheld (arbiter)")
                self._emit_resolved(d)
            elif ruling == "override":
                d.resolution = "overridden"
                d.resolved_by = "arbiter"
                d.rationale = rationale
                counts["overridden"] += 1
                self.step("dispute_resolved",
                          notes=f"{d.loan_id} overridden (arbiter)")
                self._emit_resolved(d)
            else:  # escalate
                escalations.append(d)

        # --- Escalations → human gate. ---
        if escalations:
            self._route_escalations(escalations, ctx, counts)

        # --- WA-026: remember every freshly-resolved dispute for the next run. ---
        n_remembered = self.memory.record_many(disputes)

        self._resolution_counts = counts
        self.step(
            "dispute_resolution_done",
            notes=(f"upheld={counts['upheld']} overridden={counts['overridden']} "
                   f"escalated_approved={counts['escalated_approved']} "
                   f"escalated_rejected={counts['escalated_rejected']} "
                   f"of {len(disputes)}; "
                   f"memory: short_circuited={self.memory.short_circuited} "
                   f"precedent_hits={self.memory.precedent_hits} "
                   f"misses={self.memory.misses} remembered={n_remembered}"),
        )

    # ----------------------------------------------- WA-026 memory helpers
    def _apply_recalled(self, d: Dispute, recalled: Dict[str, Any],
                        counts: Dict[str, int]) -> None:
        """Stamp a short-circuited dispute with the recalled human ruling.

        The dispute is closed with its prior resolution/resolved_by, and a
        synthetic Round-3 note records that the ruling was recalled from
        memory (no Actuary/Arbiter calls were spent). Counts the outcome so
        :attr:`_resolution_counts` stays consistent with a fresh debate.
        """
        outcome = str(recalled.get("resolution", ""))
        if outcome not in counts:
            outcome = "escalated_approved"  # defensive — should not happen
        d.resolution = outcome
        d.resolved_by = str(recalled.get("resolved_by", "human"))
        rat = str(recalled.get("rationale", "")).strip()
        d.rationale = (rat + " (recalled from dispute memory)").strip()
        d.rounds.append(DisputeRound(
            round_no=3, speaker="orchestrator",
            claim=(f"RECALLED: prior human ruling '{outcome}' reused; "
                   "debate skipped (cross-run memory short-circuit)."),
            confidence=None, model=None,
            evidence=[f"prior_resolution={outcome}",
                      f"prior_resolved_by={d.resolved_by}"],
        ))
        counts[outcome] += 1
        self.step("dispute_recalled",
                  notes=f"{d.loan_id} {outcome} (recalled from memory)")
        # WA-022: stream the synthetic recall round + the reused resolution so
        # the live view shows the memory short-circuit, not a silent skip.
        self._emit_round(d, d.rounds[-1])
        self._emit_resolved(d)

    def _inject_precedent(self, d: Dispute) -> None:
        """Stamp any non-short-circuiting prior ruling onto the dispute.

        The Arbiter/Skeptic prompts read the dispute's rounds, so we append a
        lightweight context note carrying the precedent. The debate still runs
        in full — the memory INFORMS, it does not silence. No-op when the
        account is unseen (the common first-run case).
        """
        prior = self.memory.precedent(d)
        if prior is None:
            return
        ctx_note = DisputeRound(
            round_no=1, speaker="orchestrator",
            claim=(f"PRECEDENT: a prior run resolved this account as "
                   f"'{prior.get('resolution','')}' "
                   f"(resolved_by={prior.get('resolved_by','')}); "
                   f"weigh this precedent."),
            confidence=None, model=None,
            evidence=[f"prior_resolution={prior.get('resolution','')}",
                      f"prior_resolved_by={prior.get('resolved_by','')}"],
        )
        # Insert as the leading context so the debate reads it first, but keep
        # the Skeptic's Round-1 challenge as the substantive opener.
        d.rounds.insert(0, ctx_note)

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
            # WA-022: stream the human-gate resolution (no new round is appended
            # here — the rounds were already streamed in the main loop).
            self._emit_resolved(d)

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
