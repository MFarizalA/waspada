"""WA-016 acceptance — Round 2 (defend_score) + Round 3 (Arbiter) + the four
terminal dispute resolutions wired into the orchestrator.

Covers every mandated path with a scripted MockLLM (no network):

  * defend_score unit  — uphold / concede / unparsable / brain-unreachable
  * ArbiterAgent.rule   — uphold / override / escalate (low-conf) / unparsable
  * orchestrator end-to-end — the four terminal resolutions:
        upheld / overridden / escalated_approved / escalated_rejected
  * CUT LINE — enable_arbiter=False routes upheld rebuttals straight to the gate
  * JSON parsers — tolerant of prose / fences / bad vocab / clamping

The scripted MockLLM forces each path deterministically. The data path is a
real ingest→analytics→risk_model run (reusing the WA-014 fixture shape) so the
debate runs against genuine ScoredAccounts + FeatureFrame tables.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import List

import pyarrow as pa
import pytest

from waspada.agents import (
    AgentContext, ApprovalGate, MockLLM, Status, Dispute, DisputeRound,
)
from waspada.agents.analytics import AnalyticsAgent
from waspada.agents.arbiter import (
    ArbiterAgent, ARBITER_CONFIDENCE_THRESHOLD, _parse_ruling_json,
)
from waspada.agents.ingest import IngestAgent
from waspada.agents.data_analyst import DataAnalystAgent
from waspada.agents.data_engineer import DataEngineerAgent
from waspada.agents.orchestrator import Orchestrator
from waspada.agents.risk_auditor import RiskAuditorAgent
from waspada.agents.risk_model import RiskModelAgent, _parse_verdict_json
from waspada.agents.base import Approved, Rejected
from waspada.schema import RawLoans, schema_from_dataclass


# --------------------------------------------------------------------------- #
# Shared synthetic data (mirrors test_risk_auditor so the scored table is real).
# --------------------------------------------------------------------------- #
def _raw_rows(n: int = 60, seed: int = 11) -> list[dict]:
    import numpy as np
    rng = np.random.default_rng(seed)
    issue_years = [2019, 2020, 2021, 2022, 2023]
    rows: list[dict] = []
    for i in range(n):
        iy = int(issue_years[i % len(issue_years)])
        im = int(rng.integers(1, 13))
        risky = rng.random() < 0.5
        if risky:
            rate = float(rng.uniform(18, 28)); dti_ = float(rng.uniform(22, 35))
            grade = "E"; op = float(rng.uniform(0.5, 0.9)); tp = float(rng.uniform(0.0, 0.3))
            status = "Charged Off"
        else:
            rate = float(rng.uniform(4, 10)); dti_ = float(rng.uniform(2, 12))
            grade = "A"; op = float(rng.uniform(0.0, 0.3)); tp = float(rng.uniform(0.6, 1.0))
            status = "Current"
        rows.append(dict(
            loan_id=f"R{i:04d}", amount=float(rng.uniform(2000, 25000)),
            term=int(rng.choice([36, 60])), rate=rate, grade=grade,
            annual_income=float(rng.uniform(30000, 120000)), dti=dti_,
            issue_date=dt.date(iy, im, 1),
            purpose=str(rng.choice(["credit_card", "debt_consolidation", "car", "medical"])),
            region=str(rng.choice(["West", "South", "Midwest", "Northeast"])),
            outstanding_principal=float(rng.uniform(100, 5000)) * op,
            total_paid=float(rng.uniform(100, 5000)) * tp,
            current_status=status,
        ))
    return rows


def _raw_table(rows: list[dict]) -> pa.Table:
    import dataclasses
    cols = {f.name: [] for f in dataclasses.fields(RawLoans)}
    for r in rows:
        for name in cols:
            cols[name].append(r[name])
    return pa.table(cols, schema=schema_from_dataclass(RawLoans))


def _stub_fetch(table: pa.Table):
    def _fetch(*, lane="collections", limit=None):
        return table
    return _fetch


@pytest.fixture
def scored_ctx():
    """Run ingest→analytics→risk_model; return a ctx holding scored_accounts."""
    raw = _raw_table(_raw_rows())
    ctx = AgentContext(lane="collections", data_handles={})
    ingest = IngestAgent(MockLLM())
    ingest.register_tool("fetch", _stub_fetch(raw))
    ctx = ctx.with_result(ingest.run(ctx))
    ctx = ctx.with_result(AnalyticsAgent(MockLLM(), as_of=dt.date(2024, 12, 1)).run(ctx))
    ctx = ctx.with_result(RiskModelAgent(MockLLM()).run(ctx))
    assert ctx.data_handles["scored_accounts"].num_rows > 0
    return ctx


def _open_dispute(loan_id: str = "L1") -> Dispute:
    """A Round-1-only open dispute (the state WA-016 receives from WA-014)."""
    return Dispute(
        loan_id=loan_id, opened_by="risk_auditor",
        model_band="Q5", auditor_view="Low",
        rounds=[DisputeRound(
            round_no=1, speaker="risk_auditor", model="qwen3.6-flash",
            claim="near-settled balance contradicts the band",
            confidence=0.8, evidence=["payment_ratio=0.95"],
        )],
    )


# --------------------------------------------------------------------------- #
# JSON parsers — tolerance (parity with the WA-014 parser tests).
# --------------------------------------------------------------------------- #
def test_parse_verdict_json_extracts_valid_blob():
    v, conf, claim, ev = _parse_verdict_json(
        'prose ```json\n{"verdict":"uphold","confidence":0.7,"claim":"stands","evidence":["a"]}\n```'
    )
    assert v == "uphold" and conf == 0.7 and claim == "stands" and ev == ["a"]


def test_parse_verdict_json_rejects_garbage():
    assert _parse_verdict_json("the band is fine") is None
    assert _parse_verdict_json("") is None
    assert _parse_verdict_json('{"verdict":"maybe"}') is None  # bad vocab


def test_parse_verdict_json_clamps_confidence():
    _, conf, _, _ = _parse_verdict_json('{"verdict":"concede","confidence":-3}')
    assert conf == 0.0


def test_parse_ruling_json_extracts_valid_blob():
    r, conf, rat, ev = _parse_ruling_json(
        'noise {"ruling":"override","confidence":0.66,"rationale":"skeptic wins","evidence":["x"]} tail'
    )
    assert r == "override" and conf == 0.66 and rat == "skeptic wins" and ev == ["x"]


def test_parse_ruling_json_rejects_garbage():
    assert _parse_ruling_json("can't decide") is None
    assert _parse_ruling_json('{"ruling":"maybe"}') is None  # bad vocab


def test_parse_ruling_json_clamps_confidence():
    _, conf, _, _ = _parse_ruling_json('{"ruling":"uphold","confidence":9}')
    assert conf == 1.0


# --------------------------------------------------------------------------- #
# Round 2 — RiskModelAgent.defend_score (the Actuary speaks).
# --------------------------------------------------------------------------- #
def test_defend_score_uphold_returns_round2_with_verdict_token():
    uphold = json.dumps({"verdict": "uphold", "confidence": 0.72,
                         "claim": "band stands", "evidence": ["dti=30"]})
    rm = RiskModelAgent(MockLLM(script=[uphold]))
    r2 = rm.defend_score(_open_dispute())
    assert r2.round_no == 2 and r2.speaker == "risk_model"
    assert r2.claim.startswith("UPHOLD:")
    assert r2.confidence == 0.72
    assert r2.evidence  # always cites something


def test_defend_score_concede_embeds_concede_token():
    concede = json.dumps({"verdict": "concede", "confidence": 0.6,
                          "claim": "auditor right", "evidence": []})
    rm = RiskModelAgent(MockLLM(script=[concede]))
    r2 = rm.defend_score(_open_dispute())
    assert r2.claim.startswith("CONCEDE:")
    # No features table passed → no feature-fact backfill; the round is still
    # well-formed (claim carries the verdict token, confidence recorded).
    assert r2.confidence == 0.6


def test_defend_score_unparsable_returns_unparsable_round():
    rm = RiskModelAgent(MockLLM(script=["the band is fine, trust me"]))
    r2 = rm.defend_score(_open_dispute())
    assert r2.claim.startswith("UNPARSABLE:")
    assert r2.confidence is None
    assert any(s.action == "defend_parse_fail" for s in rm.steps)


def test_defend_score_brain_unreachable_is_safe_degrade():
    class _BoomBrain(MockLLM):
        def complete(self, prompt, *, history=None):
            raise RuntimeError("network down")
    rm = RiskModelAgent(_BoomBrain())
    r2 = rm.defend_score(_open_dispute())
    assert r2.claim.startswith("UNPARSABLE:")  # safe degrade, never a crash
    assert any(s.action == "defend_call" and s.status == Status.ERROR for s in rm.steps)


def test_defend_score_uses_with_model_plus_tier():
    """defend_score tiers the brain to qwen3.7-plus; on MockLLM the override is
    a no-op so model_name stays 'mock' — but the call must not raise."""
    rm = RiskModelAgent(MockLLM(script=[
        json.dumps({"verdict": "uphold", "confidence": 0.7, "claim": "x", "evidence": []})
    ]))
    rm.defend_score(_open_dispute())
    # MockLLM.with_model returns self → model_name unchanged. The contract is
    # that defend_score never raises on a single-model brain.
    assert rm.llm.model_name == "mock"


# --------------------------------------------------------------------------- #
# Round 3 — ArbiterAgent.rule (the Arbiter rules).
# --------------------------------------------------------------------------- #
def test_arbiter_rule_uphold():
    arb = ArbiterAgent(MockLLM(script=[
        json.dumps({"ruling": "uphold", "confidence": 0.85,
                    "rationale": "actuary stronger", "evidence": ["e"]})
    ]))
    ruling, rat, conf, r3 = arb.rule(_open_dispute())
    assert ruling == "uphold" and conf == 0.85
    assert "actuary stronger" in rat
    assert r3.round_no == 3 and r3.speaker == "arbiter"
    assert r3.claim.startswith("UPHOLD:")


def test_arbiter_rule_override():
    arb = ArbiterAgent(MockLLM(script=[
        json.dumps({"ruling": "override", "confidence": 0.7,
                    "rationale": "skeptic wins", "evidence": []})
    ]))
    ruling, rat, conf, r3 = arb.rule(_open_dispute())
    assert ruling == "override" and r3.claim.startswith("OVERRIDE:")


def test_arbiter_rule_low_confidence_forces_escalate():
    """A confident-ish uphold below the threshold → escalate (borderline → human)."""
    arb = ArbiterAgent(MockLLM(script=[
        json.dumps({"ruling": "uphold", "confidence": ARBITER_CONFIDENCE_THRESHOLD - 0.01,
                    "rationale": "meh", "evidence": []})
    ]))
    ruling, rat, conf, r3 = arb.rule(_open_dispute())
    assert ruling == "escalate"
    assert r3.claim.startswith("ESCALATE:")
    assert any(s.action == "rule_low_confidence" for s in arb.steps)


def test_arbiter_rule_explicit_escalate():
    arb = ArbiterAgent(MockLLM(script=[
        json.dumps({"ruling": "escalate", "confidence": 0.4,
                    "rationale": "genuinely unsure", "evidence": []})
    ]))
    ruling, rat, conf, r3 = arb.rule(_open_dispute())
    assert ruling == "escalate"


def test_arbiter_rule_unparsable_escalates():
    arb = ArbiterAgent(MockLLM(script=["I can't decide this one"]))
    ruling, rat, conf, r3 = arb.rule(_open_dispute())
    assert ruling == "escalate"
    assert r3.claim.startswith("ESCALATE:")
    assert any(s.action == "rule_parse_fail" for s in arb.steps)


def test_arbiter_rule_brain_unreachable_escalates():
    class _BoomBrain(MockLLM):
        def complete(self, prompt, *, history=None):
            raise RuntimeError("503")
    arb = ArbiterAgent(_BoomBrain())
    ruling, rat, conf, r3 = arb.rule(_open_dispute())
    assert ruling == "escalate"
    assert any(s.action == "rule_call" and s.status == Status.ERROR for s in arb.steps)


def test_arbiter_threshold_is_tunable():
    """The brief: keep the confidence threshold tunable."""
    arb = ArbiterAgent(MockLLM(script=[
        json.dumps({"ruling": "uphold", "confidence": 0.8, "rationale": "x", "evidence": []})
    ]), threshold=0.9)
    ruling, *_ = arb.rule(_open_dispute())
    # 0.8 < 0.9 threshold → escalate even though the brain said uphold.
    assert ruling == "escalate"


def test_arbiter_uses_with_model_max_tier():
    """rule() tiers the brain to qwen3.7-max; on MockLLM the override is a
    no-op so model_name stays 'mock' — but the call must not raise."""
    arb = ArbiterAgent(MockLLM(script=[
        json.dumps({"ruling": "uphold", "confidence": 0.9, "rationale": "x", "evidence": []})
    ]))
    arb.rule(_open_dispute())
    assert arb.llm.model_name == "mock"


def test_arbiter_run_raises_not_implemented():
    """ArbiterAgent is not a pipeline step; .run() is a programmer error."""
    arb = ArbiterAgent(MockLLM())
    with pytest.raises(NotImplementedError):
        arb.run(AgentContext(lane="collections"))


# --------------------------------------------------------------------------- #
# Orchestrator end-to-end — the four terminal resolutions.
# --------------------------------------------------------------------------- #
def _orch_with_brain(raw: pa.Table, brain: MockLLM, *, gate=None,
                     enable_arbiter: bool = True) -> Orchestrator:
    g = gate if gate is not None else ApprovalGate(auto_approve=True)
    orch = Orchestrator(brain, gate=g, as_of=dt.date(2024, 12, 1),
                        top_n=10, audit_k=4, enable_arbiter=enable_arbiter)
    _orig = orch._build_agents
    def _build():
        agents = _orig()
        for a in agents:
            if isinstance(a, (DataEngineerAgent, DataAnalystAgent)):
                # Offline fetch stub + a FRESH brain so the Tier-2 function-calling
                # loops don't consume the shared debate script. They run their
                # default/fallback paths (unparsable brain) — data quality and
                # deterministic features still happen; downstream debate replies
                # stay aligned.
                a.register_tool("fetch", _stub_fetch(raw))
                a.llm = MockLLM()
        return agents
    orch._build_agents = _build  # type: ignore[method-assign]
    return orch


# The shared brain script for a full debate run. The orchestrator shares one
# brain across all agents; calls consume the script in order:
#   ingest(no llm) → analytics(no llm) → risk_model(no llm)
#   → risk_auditor: 1 complete()/account  (Round 1 challenge)
#   → _resolve_disputes: defend_score (1) + arbiter.rule (1) per dispute
# We script enough entries to cover audit_k=4 accounts + the debate rounds.
_CHALLENGE = json.dumps({
    "auditor_view": "Low", "confidence": 0.8,   # Q5 vs Low → |5-1|=4 ≥ 2 → dispute
    "claim": "balance nearly settled", "evidence": ["payment_ratio=0.95"],
})
_UPHOLD_REBUTTAL = json.dumps({
    "verdict": "uphold", "confidence": 0.75,
    "claim": "band stands", "evidence": ["dti=30"],
})
_ARBITER_UPHOLD = json.dumps({
    "ruling": "uphold", "confidence": 0.85,
    "rationale": "actuary stronger", "evidence": ["e"],
})


def _debate_script(*rounds_per_dispute, n_audits: int = 4) -> list[str]:
    """Build a flat brain script in true call order.

    The shared brain consumes replies in call order across ALL agents:
    first the auditor's per-account challenges (n_audits calls), then per
    dispute the Actuary rebuttal + Arbiter ruling interleaved. Each item in
    ``rounds_per_dispute`` is a ``(rebuttal_reply, arbiter_reply)`` tuple; a
    concession/unparsable rebuttal takes ``None`` for the arbiter slot (no
    Round 3 call). The list is repeated to cover ``n_audits`` disputes.
    """
    script: list[str] = [_CHALLENGE] * n_audits
    for rebuttal, arbiter in rounds_per_dispute:
        script.append(rebuttal)
        if arbiter is not None:
            script.append(arbiter)
    return script


def test_resolution_upheld_end_to_end():
    """Full debate: challenge → uphold rebuttal → arbiter uphold → 'upheld'."""
    raw = _raw_table(_raw_rows())
    # Shared brain consumes replies in true call order: 4 challenges, then per
    # dispute the rebuttal + arbiter ruling interleaved. Flat grouping
    # ([rebuttal]*4 + [arbiter]*4) misaligns — the arbiter would receive
    # verdict JSON and parse-fail.
    brain = MockLLM(script=[_CHALLENGE] * 4 + [_UPHOLD_REBUTTAL, _ARBITER_UPHOLD] * 4)
    orch = _orch_with_brain(raw, brain)
    res = orch.run(AgentContext(lane="collections", data_handles={}))

    assert res.status == Status.DISPUTED  # disputes existed → completion w/ DISPUTED
    counts = orch._resolution_counts
    assert counts["upheld"] == 4
    assert counts["overridden"] == 0
    assert counts["escalated_approved"] == 0
    assert counts["escalated_rejected"] == 0
    # Every dispute closed with 3 rounds and the right terminal state.
    for d in orch._final_ctx.data_handles["risk_disputes"]:
        assert len(d.rounds) == 3
        assert d.resolution == "upheld"
        assert d.resolved_by == "arbiter"


def test_resolution_overridden_via_concession():
    """Concession at Round 2 → 'overridden', no Round 3."""
    raw = _raw_table(_raw_rows())
    concede = json.dumps({"verdict": "concede", "confidence": 0.6,
                          "claim": "auditor right", "evidence": []})
    brain = MockLLM(script=[_CHALLENGE] * 4 + [concede] * 4)
    orch = _orch_with_brain(raw, brain)
    orch.run(AgentContext(lane="collections", data_handles={}))
    counts = orch._resolution_counts
    assert counts["overridden"] == 4
    assert counts["upheld"] == 0
    for d in orch._final_ctx.data_handles["risk_disputes"]:
        assert len(d.rounds) == 2  # Round 3 skipped on concession
        assert d.resolution == "overridden"
        assert d.resolved_by == "risk_model"


def test_resolution_overridden_via_arbiter_override():
    """Uphold rebuttal + arbiter override → 'overridden' (resolved_by=arbiter)."""
    raw = _raw_table(_raw_rows())
    override = json.dumps({"ruling": "override", "confidence": 0.8,
                           "rationale": "skeptic wins", "evidence": []})
    # Rebuttal + arbiter ruling interleaved per dispute (true call order).
    brain = MockLLM(script=[_CHALLENGE] * 4 + [_UPHOLD_REBUTTAL, override] * 4)
    orch = _orch_with_brain(raw, brain)
    orch.run(AgentContext(lane="collections", data_handles={}))
    counts = orch._resolution_counts
    assert counts["overridden"] == 4
    for d in orch._final_ctx.data_handles["risk_disputes"]:
        assert len(d.rounds) == 3
        assert d.resolution == "overridden"
        assert d.resolved_by == "arbiter"


def test_resolution_escalated_approved():
    """Arbiter escalate (low confidence) → human gate auto-approves → 'escalated_approved'."""
    raw = _raw_table(_raw_rows())
    low_conf_uphold = json.dumps({
        "ruling": "uphold", "confidence": ARBITER_CONFIDENCE_THRESHOLD - 0.1,
        "rationale": "meh", "evidence": [],
    })
    # Rebuttal + arbiter ruling interleaved per dispute (true call order).
    brain = MockLLM(script=[_CHALLENGE] * 4 + [_UPHOLD_REBUTTAL, low_conf_uphold] * 4)
    orch = _orch_with_brain(raw, brain)  # gate auto-approves
    orch.run(AgentContext(lane="collections", data_handles={}))
    counts = orch._resolution_counts
    assert counts["escalated_approved"] == 4
    for d in orch._final_ctx.data_handles["risk_disputes"]:
        assert d.resolution == "escalated_approved"
        assert d.resolved_by == "human"


def test_resolution_escalated_rejected():
    """Arbiter escalate → human gate rejects → 'escalated_rejected'."""
    raw = _raw_table(_raw_rows())
    low_conf_uphold = json.dumps({
        "ruling": "uphold", "confidence": ARBITER_CONFIDENCE_THRESHOLD - 0.1,
        "rationale": "meh", "evidence": [],
    })
    # Rebuttal + arbiter ruling interleaved per dispute (true call order).
    brain = MockLLM(script=[_CHALLENGE] * 4 + [_UPHOLD_REBUTTAL, low_conf_uphold] * 4)
    rejecting_gate = ApprovalGate(decide=lambda action, rationale: Rejected(
        action=action, rationale=rationale, reason="human said no"))
    orch = _orch_with_brain(raw, brain, gate=rejecting_gate)
    orch.run(AgentContext(lane="collections", data_handles={}))
    counts = orch._resolution_counts
    assert counts["escalated_rejected"] == 4
    for d in orch._final_ctx.data_handles["risk_disputes"]:
        assert d.resolution == "escalated_rejected"
        assert d.resolved_by == "human"


def test_unparsable_rebuttal_escalates_to_gate():
    """Unparsable Round 2 → auto-escalate (safe degrade) → gate decides."""
    raw = _raw_table(_raw_rows())
    brain = MockLLM(script=[_CHALLENGE] * 4 + ["garbage not json"] * 4)
    orch = _orch_with_brain(raw, brain)
    orch.run(AgentContext(lane="collections", data_handles={}))
    counts = orch._resolution_counts
    # All 4 escalated (auto-approve gate → escalated_approved).
    assert counts["escalated_approved"] == 4
    assert counts["upheld"] == 0 and counts["overridden"] == 0
    for d in orch._final_ctx.data_handles["risk_disputes"]:
        assert d.resolution == "escalated_approved"
        # Round 2 was appended (the unparsable round) but no Round 3.
        assert len(d.rounds) == 2


def test_cut_line_upheld_rebuttal_straight_to_gate():
    """enable_arbiter=False (the CUT LINE): upheld rebuttal skips Round 3 and
    escalates straight to the human gate. Documented in the task result."""
    raw = _raw_table(_raw_rows())
    brain = MockLLM(script=[_CHALLENGE] * 4 + [_UPHOLD_REBUTTAL] * 4)
    orch = _orch_with_brain(raw, brain, enable_arbiter=False)
    orch.run(AgentContext(lane="collections", data_handles={}))
    counts = orch._resolution_counts
    assert counts["escalated_approved"] == 4  # auto-approve gate
    for d in orch._final_ctx.data_handles["risk_disputes"]:
        assert len(d.rounds) == 2  # no Round 3 (arbiter cut)
        assert d.resolution == "escalated_approved"


# --------------------------------------------------------------------------- #
# Model-tiering audit — DisputeRound.model carries the real model_name.
# --------------------------------------------------------------------------- #
def test_dispute_round_model_field_records_brain():
    """On the mock brain the model field is 'mock' (with_model is a no-op); the
    contract is that the field is populated, not left None. A real QwenLLM
    would carry 'qwen3.7-plus' / 'qwen3.7-max' (live-tested separately)."""
    rm = RiskModelAgent(MockLLM(script=[_UPHOLD_REBUTTAL]))
    r2 = rm.defend_score(_open_dispute())
    assert r2.model == "mock"  # mock brain → model name recorded as 'mock'

    arb = ArbiterAgent(MockLLM(script=[_ARBITER_UPHOLD]))
    _, _, _, r3 = arb.rule(_open_dispute())
    assert r3.model == "mock"
