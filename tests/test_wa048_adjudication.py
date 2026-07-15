"""WA-048 + WA-049 acceptance — the debate actually decides.

Before WA-048 the Agent Society was *narratively* load-bearing and
*operationally inert*: ``scored_accounts`` was written once by the risk-model
agent and ranked verbatim, so an account whose own model had **conceded** still
shipped with its original band and its original ``call`` action. The debate
produced a transcript beside the decision, not a decision.

So the assertions here deliberately look at **``payload["work_list"]``, never at
``agent_dialogue``**. That the transcript records a concession was already true;
the whole point of WA-048 is that the *work-list* changes. A test that asserts on
the transcript would still have passed against the bug.

Covered:
  * the headline: a conceded dispute changes ``recommended_action`` in the payload
  * the direction rule — escalation auto-applies; de-escalation needs a human
  * ``p_default`` / ``score_band`` are never rewritten (the auditable fact)
  * WA-049: a planted **false negative** reaches the audit slice and is disputed
    (unreachable under the old top-K sampling)
  * ``_select_audit_slice`` — quotas, backfill, and small-K degrading to top-K
"""
from __future__ import annotations

import datetime as dt

import pyarrow as pa
import pytest

from waspada.agents import AgentContext, ApprovalGate, MockLLM
from waspada.agents.base import Rejected
from waspada.agents.insight import InsightAgent
from waspada.agents.orchestrator import Orchestrator
from waspada.agents.protocol import Dispute, DisputeRound
from waspada.agents.risk_auditor import AUDIT_MIX, _select_audit_slice
from waspada.schema import ScoredAccounts, validate_table


# --------------------------------------------------------------------------- #
# A minimal ScoredAccounts-shaped book we fully control.
# --------------------------------------------------------------------------- #
def _scored(rows: list[dict]) -> pa.Table:
    return pa.table({
        "loan_id": pa.array([r["loan_id"] for r in rows], pa.string()),
        "p_default": pa.array([r["p_default"] for r in rows], pa.float64()),
        "score_band": pa.array([r["score_band"] for r in rows], pa.string()),
        "segment": pa.array([{"product": "card", "region": "West"} for _ in rows]),
        "recommended_action": pa.array([r["action"] for r in rows], pa.string()),
        "delinquency_status": pa.array(
            [r.get("delinquency_status", "Current") for r in rows], pa.string()),
    })


BOOK = [
    # The account the society will argue about: the model says CALL.
    {"loan_id": "L1", "p_default": 0.95, "score_band": "Very High", "action": "call"},
    {"loan_id": "L2", "p_default": 0.80, "score_band": "High", "action": "watch"},
    {"loan_id": "L3", "p_default": 0.50, "score_band": "Medium", "action": "watch"},
    {"loan_id": "L4", "p_default": 0.20, "score_band": "Low", "action": "auto-cure"},
    {"loan_id": "L5", "p_default": 0.05, "score_band": "Very Low", "action": "auto-cure"},
]


def _ctx(disputes: list[Dispute]) -> AgentContext:
    ctx = AgentContext(lane="collections", data_handles={})
    ctx.data_handles["scored_accounts"] = _scored(BOOK)
    ctx.data_handles["risk_disputes"] = disputes
    return ctx


def _dispute(loan_id: str, model_band: str, view: str, *,
             resolution: str, resolved_by: str, revised: str) -> Dispute:
    d = Dispute(
        loan_id=loan_id, opened_by="risk_auditor",
        model_band=model_band, auditor_view=view,
        rounds=[DisputeRound(round_no=1, speaker="risk_auditor",
                             claim="challenge", evidence=["payment_ratio=0.95"])],
    )
    d.resolution, d.resolved_by, d.revised_band = resolution, resolved_by, revised
    d.rationale = "near-settled balance contradicts the band"
    return d


def _orch(gate: ApprovalGate) -> Orchestrator:
    return Orchestrator(MockLLM(), gate=gate, top_n=10)


def _payload(ctx: AgentContext, gate: ApprovalGate) -> dict:
    """Run the real InsightAgent over the adjudicated table → dashboard payload."""
    from waspada.agents.protocol import AgentResult, Status
    ctx = ctx.with_result(AgentResult(
        status=Status.OK, agent="risk_auditor", artifact_ref="scored_accounts"))
    res = InsightAgent(MockLLM(), gate=gate, top_n=10).run(ctx)
    return ctx.data_handles[res.artifact_ref]


def _row(payload: dict, loan_id: str) -> dict:
    return next(r for r in payload["work_list"] if r["loan_id"] == loan_id)


# --------------------------------------------------------------------------- #
# THE HEADLINE — a concession changes the work-list, not just the transcript.
# --------------------------------------------------------------------------- #
def test_conceded_dispute_changes_the_recommended_action_in_the_payload():
    """The Actuary concedes on a Very High account the Skeptic reads as Low.

    This is the exact scenario that shipped broken in sample-payload.json: the
    rationale said "...instead of a collector call" while the work-list row still
    said action=call. It must now say auto-cure.
    """
    gate = ApprovalGate(auto_approve=True)   # the human approves the de-escalation
    d = _dispute("L1", "Very High", "Low",
                 resolution="overridden", resolved_by="risk_model", revised="Very Low")
    ctx = _ctx([d])

    _orch(gate)._apply_adjudications(ctx)
    payload = _payload(ctx, gate)
    row = _row(payload, "L1")

    # The decision moved.
    assert row["recommended_action"] == "auto-cure", "the concession never reached the work-list"
    assert row["final_band"] == "Very Low"
    assert row["override_reason"]           # the analyst can see WHY
    assert d.applied is True

    # ...and the model's own numbers are untouched — they stay the auditable fact.
    assert row["score_band"] == "Very High"
    assert row["p_default"] == pytest.approx(0.95)


def test_upheld_dispute_leaves_the_work_list_alone():
    gate = ApprovalGate(auto_approve=True)
    d = _dispute("L1", "Very High", "Medium",
                 resolution="upheld", resolved_by="arbiter", revised="")
    ctx = _ctx([d])

    _orch(gate)._apply_adjudications(ctx)
    row = _row(_payload(ctx, gate), "L1")

    assert row["recommended_action"] == "call"
    assert row["final_band"] == "Very High"
    assert "override_reason" not in row     # nothing was overridden
    assert d.applied is False


# --------------------------------------------------------------------------- #
# THE DIRECTION RULE — asymmetric error costs get asymmetric governance.
# --------------------------------------------------------------------------- #
def test_escalation_applies_with_no_human_gate():
    """Society RAISES risk → auto-apply. Worst case is a wasted collector call."""
    gate = ApprovalGate(auto_approve=False, decide=lambda a, r: pytest.fail(
        f"an escalation must not ask the gate (asked for {a!r})"))
    d = _dispute("L5", "Very Low", "High",
                 resolution="overridden", resolved_by="arbiter", revised="Very High")
    ctx = _ctx([d])

    _orch(gate)._apply_adjudications(ctx)

    assert d.applied is True
    scored = ctx.data_handles["scored_accounts"]
    i = scored.column("loan_id").to_pylist().index("L5")
    assert scored.column("final_band")[i].as_py() == "Very High"
    assert scored.column("recommended_action")[i].as_py() == "call"


def test_deescalation_is_withheld_until_a_human_approves():
    """Society LOWERS risk → gated. Worst case is a real default walking away."""
    asked: list[str] = []

    def _refuse(action: str, rationale: str):
        asked.append(action)
        return Rejected(action=action, rationale=rationale)

    gate = ApprovalGate(auto_approve=False, decide=_refuse)
    d = _dispute("L1", "Very High", "Low",
                 resolution="overridden", resolved_by="risk_model", revised="Very Low")
    ctx = _ctx([d])

    _orch(gate)._apply_adjudications(ctx)
    # The de-escalation gate already fired above; render the settled table with a
    # neutral gate so insight's own work-list-publish approval isn't what we test.
    payload = _payload(ctx, ApprovalGate(auto_approve=True))
    row = _row(payload, "L1")

    assert "approve_deescalation" in asked, "a de-escalation must ask a human"
    assert d.applied is False
    # The collector call STANDS, because the human refused to cancel it.
    assert row["recommended_action"] == "call"
    assert row["final_band"] == "Very High"


def test_human_settled_disputes_are_not_re_gated():
    """A dispute already ruled on at the gate is honoured, not asked about twice."""
    gate = ApprovalGate(auto_approve=False, decide=lambda a, r: pytest.fail(
        f"escalated_approved must not be re-gated (asked for {a!r})"))
    d = _dispute("L1", "Very High", "Low",
                 resolution="escalated_approved", resolved_by="human", revised="Medium")
    ctx = _ctx([d])

    _orch(gate)._apply_adjudications(ctx)

    assert d.applied is True
    row = _row(_payload(ctx, ApprovalGate(auto_approve=True)), "L1")
    assert row["final_band"] == "Medium"
    assert row["recommended_action"] == "watch"


def test_escalated_rejected_never_applies():
    gate = ApprovalGate(auto_approve=True)
    d = _dispute("L1", "Very High", "Low",
                 resolution="escalated_rejected", resolved_by="human", revised="Very Low")
    ctx = _ctx([d])

    _orch(gate)._apply_adjudications(ctx)

    assert d.applied is False
    assert _row(_payload(ctx, gate), "L1")["recommended_action"] == "call"


# --------------------------------------------------------------------------- #
# Contract safety.
# --------------------------------------------------------------------------- #
def test_adjudicated_table_still_satisfies_the_frozen_contract():
    """final_band / override_reason are additive — validate_table allows supersets."""
    gate = ApprovalGate(auto_approve=True)
    ctx = _ctx([_dispute("L1", "Very High", "Low", resolution="overridden",
                         resolved_by="risk_model", revised="Very Low")])
    _orch(gate)._apply_adjudications(ctx)
    validate_table(ctx.data_handles["scored_accounts"], ScoredAccounts, name="adjudicated")


def test_apply_adjudications_is_idempotent():
    """A second pass replaces the columns rather than duplicating them."""
    gate = ApprovalGate(auto_approve=True)
    ctx = _ctx([_dispute("L1", "Very High", "Low", resolution="overridden",
                         resolved_by="risk_model", revised="Very Low")])
    orch = _orch(gate)
    orch._apply_adjudications(ctx)
    orch._apply_adjudications(ctx)
    names = ctx.data_handles["scored_accounts"].column_names
    assert names.count("final_band") == 1 and names.count("override_reason") == 1


def test_no_disputes_leaves_the_table_untouched():
    gate = ApprovalGate(auto_approve=True)
    ctx = _ctx([])
    before = ctx.data_handles["scored_accounts"]
    _orch(gate)._apply_adjudications(ctx)
    assert ctx.data_handles["scored_accounts"] is before


def test_rank_without_final_band_behaves_exactly_as_before():
    """A pre-WA-048 table (no adjudication columns) ranks unchanged."""
    from waspada.insight.ranking import rank
    rows = rank(_scored(BOOK), top_n=10)
    assert rows[0]["loan_id"] == "L1" and rows[0]["recommended_action"] == "call"
    assert "final_band" not in rows[0]


# --------------------------------------------------------------------------- #
# WA-049 — the audit slice can finally see a false negative.
# --------------------------------------------------------------------------- #
def test_planted_false_negative_reaches_the_audit_slice():
    """A delinquent account the model bands "Very Low" is THE expensive miss.

    Under the old top-K-by-p_default sampling it could never be audited, which
    made the symmetric half of the admissibility rule (Very Low + High → dispute)
    unreachable dead code. It must now be drawn.
    """
    book = [
        {"loan_id": f"HI{i}", "p_default": 0.90 - i * 0.01, "score_band": "Very High",
         "action": "call", "delinquency_status": "Default"} for i in range(10)
    ] + [
        # The plant: model says safest account on the book; it is in default.
        {"loan_id": "MISS", "p_default": 0.01, "score_band": "Very Low",
         "action": "auto-cure", "delinquency_status": "Default"},
    ]
    idx, strata = _select_audit_slice(_scored(book), 8)
    picked = [_scored(book).column("loan_id")[i].as_py() for i in idx]

    assert "MISS" in picked, "the false negative was never audited"
    assert strata["contradictory"] >= 1
    assert len(idx) <= 8, "the audit must not exceed its K budget"


def test_audit_slice_respects_k_and_backfills_a_short_stratum():
    """A clean book has no contradictory accounts → the quota spills to riskiest."""
    clean = [
        {"loan_id": f"C{i}", "p_default": 0.9 - i * 0.05, "score_band": "Very High",
         "action": "call", "delinquency_status": "Current"} for i in range(20)
    ]
    idx, strata = _select_audit_slice(_scored(clean), 8)
    assert len(idx) == 8, "a short stratum must backfill, not shrink the audit"
    assert strata.get("contradictory", 0) == 0
    assert len(set(idx)) == 8, "no account audited twice"


def test_small_k_degrades_to_plain_top_k():
    """K=1 must spend its only slot on the riskiest account, not a side stratum."""
    idx, strata = _select_audit_slice(_scored(BOOK), 1)
    assert idx == [0]                    # L1, the highest p_default
    assert strata["riskiest"] == 1


def test_audit_slice_never_exceeds_the_book():
    idx, _ = _select_audit_slice(_scored(BOOK[:2]), 8)
    assert len(idx) == 2


def test_audit_mix_is_the_documented_split_at_the_default_k():
    """The k=8 default allocates exactly the documented 3/2/3."""
    book = [
        {"loan_id": f"X{i}", "p_default": 0.99 - i * 0.02,
         "score_band": "Very Low" if i % 4 == 3 else "Very High",
         "action": "call",
         "delinquency_status": "Default" if i % 4 == 3 else "Current"}
        for i in range(40)
    ]
    _, strata = _select_audit_slice(_scored(book), 8)
    assert strata == {"riskiest": AUDIT_MIX["riskiest"],
                      "contradictory": AUDIT_MIX["contradictory"],
                      "boundary": AUDIT_MIX["boundary"]}
