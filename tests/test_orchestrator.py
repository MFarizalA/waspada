"""Orchestrator tests (WA-010 acceptance).

Full run with mock LLM + stubbed data: asserts step order, gate invocation,
payload emitted, report non-empty, and that an ApprovalGate rejection
short-circuits the pipeline.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pyarrow as pa
import pytest

from waspada.agents import AgentContext, ApprovalGate, Approved, MockLLM, Rejected, Status
from waspada.agents.data_analyst import DataAnalystAgent
from waspada.agents.data_engineer import DataEngineerAgent
from waspada.agents.orchestrator import COLLECTIONS_STEP_ORDER, Orchestrator
from waspada.schema import RawLoans, schema_from_dataclass


# --------------------------------------------------------------------------- #
# Synthetic RawLoans fixture (shared shape with the pipeline-agents test).
# --------------------------------------------------------------------------- #
def _raw_table(n: int = 80, seed: int = 11) -> pa.Table:
    import dataclasses
    import numpy as np

    rng = np.random.default_rng(seed)
    issue_years = [2019, 2020, 2021, 2022, 2023]
    rows = []
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
            loan_id=f"R{i:04d}",
            amount=float(rng.uniform(2000, 25000)),
            term=int(rng.choice([36, 60])),
            rate=rate, grade=grade,
            annual_income=float(rng.uniform(30000, 120000)),
            dti=dti,
            issue_date=dt.date(iy, im, 1),
            purpose=str(rng.choice(["credit_card", "debt_consolidation", "car", "medical"])),
            region=str(rng.choice(["West", "South", "Midwest", "Northeast"])),
            outstanding_principal=float(rng.uniform(100, 5000)) * op,
            total_paid=float(rng.uniform(100, 5000)) * tp,
            current_status=status,
        ))
    cols = {f.name: [] for f in dataclasses.fields(RawLoans)}
    for r in rows:
        for name in cols:
            cols[name].append(r[name])
    return pa.table(cols, schema=schema_from_dataclass(RawLoans))


def _orchestrator_with_stub(raw: pa.Table, *, gate: ApprovalGate) -> Orchestrator:
    """Build an orchestrator whose data-engineer fetch is stubbed (offline)."""
    orch = Orchestrator(MockLLM(), gate=gate, as_of=dt.date(2024, 12, 1), top_n=15)
    _stub = (lambda tbl: (lambda *, lane="collections", limit=None: tbl))(raw)
    _orig_build = orch._build_agents
    def _build():
        agents = _orig_build()
        for a in agents:
            if isinstance(a, (DataEngineerAgent, DataAnalystAgent)):
                a.register_tool("fetch", _stub)
                # Fresh mock brain so the Tier-2 loops don't consume the shared
                # debate script (if one is wired on the orchestrator brain).
                a.llm = MockLLM()
        return agents
    orch._build_agents = _build  # type: ignore[method-assign]
    return orch


@pytest.fixture
def raw_table() -> pa.Table:
    return _raw_table()


# --------------------------------------------------------------------------- #
# plan — builds the Collections step sequence.
# --------------------------------------------------------------------------- #
def test_plan_returns_collections_step_order():
    orch = Orchestrator(MockLLM())
    steps = orch.plan("collections")
    assert steps == list(COLLECTIONS_STEP_ORDER)
    assert steps[0] == "data_engineer" and steps[-1] == "insight"


def test_plan_rejects_unknown_lane():
    orch = Orchestrator(MockLLM())
    with pytest.raises(ValueError):
        orch.plan("bogus")


def test_plan_accepts_origination_lane():
    """WA-033 lifted the guard: origination plans the same five-step society.
    (This test previously pinned the raise; the lane is now implemented.)"""
    orch = Orchestrator(MockLLM())
    steps = orch.plan("origination")
    assert steps == ["data_engineer", "data_analyst", "risk_model", "risk_auditor", "insight"]


# --------------------------------------------------------------------------- #
# run — full orchestrated pipeline: step order, gate, payload emitted.
# --------------------------------------------------------------------------- #
def test_run_executes_all_steps_in_order(raw_table):
    gate = ApprovalGate(auto_approve=True)
    orch = _orchestrator_with_stub(raw_table, gate=gate)
    ctx = AgentContext(lane="collections", data_handles={})
    res = orch.run(ctx)

    assert res.ok
    assert res.artifact_ref == "dashboard_payload"
    # Step order logged: one "run" step per agent, in sequence.
    run_notes = [s.notes for s in orch.steps if s.action == "run"]
    # Each run-step names the agent and its artifact handle.
    assert any("data_engineer" in n and "raw_loans" in n for n in run_notes)
    assert any("data_analyst" in n and "feature_frame" in n for n in run_notes)
    assert any("risk_model" in n and "scored_accounts" in n for n in run_notes)
    assert any("risk_auditor" in n and "scored_accounts" in n for n in run_notes)
    assert any("insight" in n and "dashboard_payload" in n for n in run_notes)
    # Handoffs recorded frm→to in order. (risk_auditor sits between risk_model
    # and insight — WA-014.) data_engineer replaced ingest in WA-029.
    assert [h.frm for h in orch.handoffs] == ["data_engineer", "data_analyst", "risk_model", "risk_auditor"]
    assert [h.to for h in orch.handoffs] == ["data_analyst", "risk_model", "risk_auditor", "insight"]


def test_run_invokes_approval_gate_before_payload(raw_table):
    """The gate is invoked (publish_work_list) before the payload is released."""
    gate = ApprovalGate(auto_approve=True)
    orch = _orchestrator_with_stub(raw_table, gate=gate)
    orch.run(AgentContext(lane="collections", data_handles={}))
    assert any(s.action == "publish_work_list" and s.auto is True for s in gate.steps)


def test_run_emits_dashboard_payload(raw_table):
    gate = ApprovalGate(auto_approve=True)
    orch = _orchestrator_with_stub(raw_table, gate=gate)
    ctx = AgentContext(lane="collections", data_handles={})
    res = orch.run(ctx)
    payload = getattr(orch, "_final_ctx", ctx).data_handles[res.artifact_ref]
    # Required contract keys always present; additive optional keys
    # (agent_dialogue, model_card — WA-093) may accompany them.
    assert {"work_list", "portfolio_health", "alerts"} <= set(payload.keys())
    assert set(payload.keys()) <= {"work_list", "portfolio_health", "alerts", "agent_dialogue", "model_card", "policy_card"}


# --------------------------------------------------------------------------- #
# Gate rejection short-circuits the pipeline.
# --------------------------------------------------------------------------- #
def test_run_short_circuits_on_gate_rejection(raw_table):
    """A rejected work-list gate halts the pipeline with a BLOCKED result."""
    def _reject(action, rationale):
        return Rejected(action=action, rationale=rationale, reason="analyst said no")
    gate = ApprovalGate(decide=_reject)
    orch = _orchestrator_with_stub(raw_table, gate=gate)
    res = orch.run(AgentContext(lane="collections", data_handles={}))

    assert res.status == Status.BLOCKED
    assert "insight" in res.notes.lower() or "rejected" in res.notes.lower()
    # The pipeline ran data_engineer→analytics→risk_model but stopped at insight.
    run_agents = [n.split(" ")[0] for n in (s.notes for s in orch.steps if s.action == "run")]
    assert "data_engineer" in run_agents and "risk_model" in run_agents


def test_run_surfaces_stage_failure_not_swallowed():
    """A data_engineer failure (zero rows) surfaces as BLOCKED, not a silent success."""
    import dataclasses
    empty = pa.table(
        {f.name: [] for f in dataclasses.fields(RawLoans)},
        schema=schema_from_dataclass(RawLoans),
    )
    gate = ApprovalGate(auto_approve=True)
    orch = _orchestrator_with_stub(empty, gate=gate)
    res = orch.run(AgentContext(lane="collections", data_handles={}))
    assert res.status == Status.BLOCKED
    assert "data_engineer" in res.notes.lower() or "ingest" in res.notes.lower()


# --------------------------------------------------------------------------- #
# report — plain-language summary, non-empty, mentions the key facts.
# --------------------------------------------------------------------------- #
def test_report_is_non_empty_and_mentions_health(raw_table):
    gate = ApprovalGate(auto_approve=True)
    orch = _orchestrator_with_stub(raw_table, gate=gate)
    ctx = AgentContext(lane="collections", data_handles={})
    res = orch.run(ctx)
    payload = getattr(orch, "_final_ctx", ctx).data_handles[res.artifact_ref]
    text = orch.report(payload)
    assert isinstance(text, str) and len(text) > 20
    assert "NPL" in text
    assert "Alerts" in text


def test_report_handles_empty_payload():
    """An empty payload still produces a readable report (no crash)."""
    orch = Orchestrator(MockLLM())
    text = orch.report({"work_list": [], "portfolio_health": {"npl_ratio": 0.0, "vintage_default_rate": {}, "status_mix": {}}, "alerts": []})
    assert isinstance(text, str) and "0 accounts" in text


# --------------------------------------------------------------------------- #
# CLI — end-to-end offline run writes the payload JSON.
# --------------------------------------------------------------------------- #
def test_cli_writes_dashboard_payload(tmp_path, monkeypatch):
    """`python -m waspada.agents` (offline) writes a valid payload JSON."""
    from waspada.agents.__main__ import main

    out = tmp_path / "payload.json"
    # Ensure offline path (no OSS creds) + auto-approve.
    monkeypatch.delenv("OSS_BUCKET", raising=False)
    code = main(["--lane", "collections", "--auto-approve", "--top-n", "10", "--out", str(out)])
    assert code == 0
    assert out.exists()
    import json
    payload = json.loads(out.read_text())
    # Required contract keys always present; additive optional keys
    # (agent_dialogue, model_card — WA-093) may accompany them.
    assert {"work_list", "portfolio_health", "alerts"} <= set(payload.keys())
    assert set(payload.keys()) <= {"work_list", "portfolio_health", "alerts", "agent_dialogue", "model_card", "policy_card"}
    assert len(payload["work_list"]) <= 10


def test_cli_completes_on_disputed_run(tmp_path, monkeypatch):
    """A DISPUTED run (Skeptic opened disputes) is a *completion*: the CLI
    writes the payload (with ``agent_dialogue``) and returns 0, not 2.

    Exercises the WA-014 fix that OK and DISPUTED are both completions; only
    ERROR/BLOCKED are real CLI failures.
    """
    import json as _json
    from waspada.agents.__main__ import main
    from waspada.agents.llm import MockLLM
    from waspada.agents.orchestrator import Orchestrator
    from waspada.agents.protocol import Status

    # Force a challenge brain: every top-K audit returns a Low view → disputes
    # open on every Very High account the model produced.
    challenge = _json.dumps({
        "auditor_view": "Low", "confidence": 0.8,
        "claim": "balance nearly settled", "evidence": ["payment_ratio=0.95"],
    })
    monkeypatch.setenv("WASPADA_LLM_PROVIDER", "mock")
    monkeypatch.delenv("OSS_BUCKET", raising=False)

    # Inject the scripted brain by patching get_llm used inside main().
    import waspada.agents.llm as _llmmod
    _orig_get_llm = _llmmod.get_llm
    def _scripted(_=None):
        return MockLLM(script=[challenge] * 50)
    monkeypatch.setattr(_llmmod, "get_llm", _scripted)
    # main() imports get_llm by name from its own module → patch there too.
    import waspada.agents.__main__ as _mainmod
    monkeypatch.setattr(_mainmod, "get_llm", _scripted, raising=False)

    out = tmp_path / "disputed.json"
    code = main(["--lane", "collections", "--auto-approve", "--top-n", "10", "--out", str(out)])
    assert code == 0
    payload = _json.loads(out.read_text())
    assert payload.get("agent_dialogue"), "disputed run must serialize agent_dialogue"
    # Every dispute carries the model's band from the frozen vocabulary. (Pre
    # WA-049 this asserted the FIRST dispute was "Very High" — true only because
    # the audit slice was top-K by p_default. The slice is now stratified, so a
    # disputed account may legitimately come from the boundary or contradictory
    # stratum and carry a lower band. That widening IS the fix, not a regression.)
    from waspada.schema import RISK_LEVELS
    bands = [d["model_band"] for d in payload["agent_dialogue"]]
    assert bands and all(b in RISK_LEVELS for b in bands)
