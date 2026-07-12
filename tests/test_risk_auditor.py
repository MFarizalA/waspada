"""Risk-auditor + dispute wiring tests (WA-014 acceptance).

Covers the four mandated paths:
  * dispute-opened   — scripted MockLLM returns a challenge JSON, the Skeptic
                       opens a Dispute and the orchestrator run terminates
                       ``Status.DISPUTED`` with a valid ``agent_dialogue``.
  * no-dispute       — scripted MockLLM agrees with the band; no dispute, run
                       completes ``Status.OK``, ``agent_dialogue`` absent.
  * parse-degrade    — scripted MockLLM returns unparsable prose; no dispute
                       opened, pipeline completes (graceful degrade).
  * agent_dialogue shape — a Dispute serializes to the exact frozen shape
                            (matches ``dashboard/fixtures/sample-payload.json``).

The scripted MockLLM forces each path deterministically (no network). The
data is a small synthetic RawLoans table run through the real ingest →
analytics → risk_model path so the auditor sees a genuine ScoredAccounts table.
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
from waspada.agents.ingest import IngestAgent
from waspada.agents.data_analyst import DataAnalystAgent
from waspada.agents.data_engineer import DataEngineerAgent
from waspada.agents.insight import InsightAgent
from waspada.agents.orchestrator import COLLECTIONS_STEP_ORDER, Orchestrator
from waspada.agents.risk_auditor import RiskAuditorAgent, _parse_view_json
from waspada.agents.risk_model import RiskModelAgent
from waspada.schema import RawLoans, schema_from_dataclass


# --------------------------------------------------------------------------- #
# Synthetic data + a real scored_accounts fixture (ingest→analytics→risk_model).
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
            rate = float(rng.uniform(18, 28)); dti = float(rng.uniform(22, 35))
            grade = "E"; op = float(rng.uniform(0.5, 0.9)); tp = float(rng.uniform(0.0, 0.3))
            status = "Charged Off"
        else:
            rate = float(rng.uniform(4, 10)); dti = float(rng.uniform(2, 12))
            grade = "A"; op = float(rng.uniform(0.0, 0.3)); tp = float(rng.uniform(0.6, 1.0))
            status = "Current"
        rows.append(dict(
            loan_id=f"R{i:04d}", amount=float(rng.uniform(2000, 25000)),
            term=int(rng.choice([36, 60])), rate=rate, grade=grade,
            annual_income=float(rng.uniform(30000, 120000)), dti=dti,
            issue_date=dt.date(iy, im, 1),
            purpose=str(rng.choice(["credit_card", "debt_consolidation", "car", "medical"])),
            region=str(rng.choice(["West", "South", "Midwest", "Northeast"])),
            outstanding_principal=float(rng.uniform(100, 5000)) * op,
            total_paid=float(rng.uniform(100, 5000)) * tp,
            current_status=status,
        ))
    return rows


def _raw_table(rows: list[dict]) -> pa.Table:
    cols = {f.name: [] for f in __import__("dataclasses").fields(RawLoans)}
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


# --------------------------------------------------------------------------- #
# JSON parse helper — tolerant of prose / fences / missing fields.
# --------------------------------------------------------------------------- #
def test_parse_view_json_extracts_valid_blob():
    view, conf, claim, ev = _parse_view_json(
        'prefix ```json\n{"auditor_view":"Low","confidence":0.7,"claim":"x","evidence":["a","b"]}\n```'
    )
    assert view == "Low" and conf == 0.7 and claim == "x" and ev == ["a", "b"]


def test_parse_view_json_rejects_garbage():
    assert _parse_view_json("the score looks fine, nothing to see") is None
    assert _parse_view_json("") is None
    assert _parse_view_json('{"auditor_view":"Maybe"}') is None  # bad vocab


def test_parse_view_json_clamps_confidence():
    _, conf, _, _ = _parse_view_json('{"auditor_view":"High","confidence":5}')
    assert conf == 1.0


# --------------------------------------------------------------------------- #
# Dispute serialization — the frozen shape (matches sample-payload.json).
# --------------------------------------------------------------------------- #
def test_dispute_to_dict_matches_frozen_shape():
    d = Dispute(
        loan_id="LN00961668", opened_by="risk_auditor",
        model_band="Q5", auditor_view="Medium",
        rounds=[DisputeRound(
            round_no=1, speaker="risk_auditor", model="qwen3.6-flash",
            claim="repayment outlier", confidence=0.72,
            evidence=["payment_ratio=0.61", "dti=31.4"],
        )],
        resolution="upheld", resolved_by="arbiter", rationale="score stands",
    )
    obj = d.to_dict()
    # Exact key set + order against the fixture's first dispute.
    assert list(obj.keys()) == [
        "loan_id", "opened_by", "model_band", "auditor_view",
        "rounds", "resolution", "resolved_by", "rationale",
    ]
    r1 = obj["rounds"][0]
    assert list(r1.keys()) == [
        "round_no", "speaker", "model", "claim", "confidence", "evidence",
    ]
    # Round-trips through JSON (the contract is JSON-native).
    json.dumps(obj)


def test_dispute_to_dict_round_trips_through_sample_payload_keys():
    """A serialized dispute carries every key the fixture's disputes carry."""
    d = Dispute(loan_id="X", opened_by="risk_auditor", model_band="Q5", auditor_view="Low")
    keys = set(d.to_dict().keys())
    fixture = json.loads(
        (__import__("pathlib").Path(__file__).resolve().parents[1]
         / "dashboard" / "fixtures" / "sample-payload.json").read_text()
    )
    fixture_keys = {k for d in fixture["agent_dialogue"] for k in d.keys()}
    assert keys == fixture_keys


# --------------------------------------------------------------------------- #
# Admissibility rule — band vs view ordinal gap ≥ 2.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("band,view,opens", [
    ("Q5", "Low", True), ("Q5", "Medium", True), ("Q5", "High", False),
    ("Q1", "High", True), ("Q3", "Medium", False), ("Q4", "Low", True),
    ("Q2", "Medium", False), ("Q1", "Low", False),
])
def test_should_dispute_rule(band, view, opens):
    assert RiskAuditorAgent._should_dispute(band, view) is opens


def test_should_dispute_unknown_values_never_dispute():
    assert RiskAuditorAgent._should_dispute("??", "Low") is False
    assert RiskAuditorAgent._should_dispute("Q5", "??") is False


# --------------------------------------------------------------------------- #
# The three scripted paths via the auditor agent directly.
# --------------------------------------------------------------------------- #
def test_auditor_opens_dispute_on_divergence(scored_ctx):
    """Scripted challenge JSON → dispute opened on every top-K account."""
    challenge = json.dumps({
        "auditor_view": "Low",      # model is Q5 → |5−1| = 4 ≥ 2 → dispute
        "confidence": 0.8,
        "claim": "near-settled balance contradicts the band",
        "evidence": ["payment_ratio=0.95"],
    })
    auditor = RiskAuditorAgent(MockLLM(script=[challenge]), k=4)
    res = auditor.run(scored_ctx)
    assert res.ok
    disputes = scored_ctx.data_handles["risk_disputes"]
    assert len(disputes) >= 1
    d = disputes[0]
    assert d.opened_by == "risk_auditor"
    assert d.model_band == "Q5" and d.auditor_view == "Low"
    # Round 1 only (WA-014); resolution is OPEN until WA-016.
    assert len(d.rounds) == 1
    assert d.rounds[0].round_no == 1
    assert d.rounds[0].speaker == "risk_auditor"
    assert d.rounds[0].model == "mock"
    assert d.rounds[0].evidence  # claim always cites something
    assert d.resolution == "" and d.resolved_by == ""


def test_auditor_no_dispute_when_bands_agree(scored_ctx):
    """Scripted agreement (Q5 + High) → no dispute opened."""
    agree = json.dumps({
        "auditor_view": "High",  # model Q5 → |5−5| = 0 < 2 → no dispute
        "confidence": 0.9, "claim": "score stands", "evidence": ["dti=30"],
    })
    auditor = RiskAuditorAgent(MockLLM(script=[agree]), k=4)
    res = auditor.run(scored_ctx)
    assert res.ok
    assert scored_ctx.data_handles["risk_disputes"] == []
    # The auditor still completed (parsed all, opened zero).
    assert any(s.action == "audit_done" and "disputes=0" in s.notes for s in auditor.steps)


def test_auditor_parse_failure_degrades_gracefully(scored_ctx):
    """Unparsable LLM reply → no dispute, pipeline-continues, parse-fail logged."""
    auditor = RiskAuditorAgent(MockLLM(script=["the account is fine, trust me"]), k=4)
    res = auditor.run(scored_ctx)
    assert res.ok  # graceful degrade: not an ERROR
    assert scored_ctx.data_handles["risk_disputes"] == []
    # Parse-fail was logged distinctly (audit trail).
    assert any(s.action == "audit_parse_fail" for s in auditor.steps)
    assert any("parse_fail=" in s.notes and int(s.notes.split("parse_fail=")[1].split()[0]) > 0
               for s in auditor.steps if s.action == "audit_done")


# --------------------------------------------------------------------------- #
# End-to-end via the orchestrator — DISPUTED routing + agent_dialogue.
# --------------------------------------------------------------------------- #
def _orch_with_stub_brain(raw: pa.Table, brain: MockLLM) -> Orchestrator:
    orch = Orchestrator(brain, gate=ApprovalGate(auto_approve=True),
                        as_of=dt.date(2024, 12, 1), top_n=10, audit_k=4)
    _orig = orch._build_agents
    def _build():
        agents = _orig()
        for a in agents:
            if isinstance(a, (DataEngineerAgent, DataAnalystAgent)):
                a.register_tool("fetch", _stub_fetch(raw))
                # Fresh brain — Tier-2 loops must not eat the shared debate script.
                a.llm = MockLLM()
        return agents
    orch._build_agents = _build  # type: ignore[method-assign]
    return orch


def test_orchestrator_routes_disputed_when_auditor_opens_dispute():
    raw = _raw_table(_raw_rows())
    challenge = json.dumps({
        "auditor_view": "Low", "confidence": 0.8,
        "claim": "balance nearly settled", "evidence": ["payment_ratio=0.95"],
    })
    # Script repeats the challenge for every top-K audit call.
    brain = MockLLM(script=[challenge] * 20)
    orch = _orch_with_stub_brain(raw, brain)
    ctx = AgentContext(lane="collections", data_handles={})
    res = orch.run(ctx)

    # DISPUTED is a *completion* — payload still produced.
    assert res.status == Status.DISPUTED
    assert res.artifact_ref == "dashboard_payload"
    payload = orch._final_ctx.data_handles["dashboard_payload"]
    # agent_dialogue present + non-empty + matches frozen shape.
    assert "agent_dialogue" in payload and payload["agent_dialogue"]
    d = payload["agent_dialogue"][0]
    assert {"loan_id", "opened_by", "model_band", "auditor_view",
            "rounds", "resolution", "resolved_by", "rationale"} <= set(d.keys())
    # The gate saw BOTH actions: publish_work_list AND resolve_risk_dispute.
    actions = [s.action for s in orch.gate.steps]
    assert "publish_work_list" in actions
    assert "resolve_risk_dispute" in actions


def test_orchestrator_ok_when_no_dispute_opened():
    raw = _raw_table(_raw_rows())
    agree = json.dumps({
        "auditor_view": "High", "confidence": 0.9,
        "claim": "score stands", "evidence": ["dti=30"],
    })
    brain = MockLLM(script=[agree] * 20)
    orch = _orch_with_stub_brain(raw, brain)
    res = orch.run(AgentContext(lane="collections", data_handles={}))
    assert res.status == Status.OK
    payload = orch._final_ctx.data_handles["dashboard_payload"]
    # No disputes → agent_dialogue is additive-optional and ABSENT (older shape
    # stays valid). This is the "shape must be valid either way" acceptance.
    assert "agent_dialogue" not in payload
    # resolve_risk_dispute was NOT requested (no disputes to resolve).
    assert "resolve_risk_dispute" not in [s.action for s in orch.gate.steps]


def test_orchestrator_parse_fail_completes_ok_with_empty_dialogue():
    raw = _raw_table(_raw_rows())
    brain = MockLLM(script=["garbage, not json"] * 20)
    orch = _orch_with_stub_brain(raw, brain)
    res = orch.run(AgentContext(lane="collections", data_handles={}))
    # Parse failures degrade to no-dispute → OK (not DISPUTED, not ERROR).
    assert res.status == Status.OK
    assert orch._final_ctx.data_handles["risk_disputes"] == []


# --------------------------------------------------------------------------- #
# DashboardPayload contract — agent_dialogue round-trips through json.dumps.
# --------------------------------------------------------------------------- #
def test_payload_with_agent_dialogue_json_round_trips():
    payload = {
        "work_list": [],
        "portfolio_health": {"npl_ratio": 0.0, "vintage_default_rate": {}, "status_mix": {}},
        "alerts": [],
        "agent_dialogue": [Dispute(
            loan_id="L1", opened_by="risk_auditor", model_band="Q5", auditor_view="Low",
            rounds=[DisputeRound(1, "risk_auditor", "c", 0.5, "mock", ["e"])],
        ).to_dict()],
    }
    s = json.dumps(payload)
    back = json.loads(s)
    assert back["agent_dialogue"][0]["model_band"] == "Q5"
