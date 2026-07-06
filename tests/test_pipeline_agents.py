"""Pipeline agents tests (WA-009 acceptance).

Each of the four agents (ingest → analytics → risk-model → insight) produces
its contract artifact on a small stubbed end-to-end run. The BQ fetch is
stubbed (no network); the LLM is the mock brain; data flows through the shared
``AgentContext.data_handles`` store via ``artifact_ref`` handles.
"""
from __future__ import annotations

import datetime as dt
from typing import List

import pyarrow as pa
import pytest

from waspada.agents import AgentContext, ApprovalGate, Approved, MockLLM, Rejected, Status
from waspada.agents.analytics import AnalyticsAgent
from waspada.agents.ingest import IngestAgent
from waspada.agents.insight import InsightAgent
from waspada.agents.risk_model import RiskModelAgent
from waspada.schema import (
    DashboardPayload,
    FeatureFrame,
    RawLoans,
    ScoredAccounts,
    schema_from_dataclass,
    validate_table,
)


# --------------------------------------------------------------------------- #
# Synthetic RawLoans fixture — separable so the model beats chance.
# --------------------------------------------------------------------------- #
def _raw_rows(n: int = 60, seed: int = 11) -> list[dict]:
    """Two classes across multiple vintages (so train+test each have both)."""
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
    return rows


def _raw_table(rows: list[dict]) -> pa.Table:
    cols = {f.name: [] for f in __import__("dataclasses").fields(RawLoans)}
    for r in rows:
        for name in cols:
            cols[name].append(r[name])
    return pa.table(cols, schema=schema_from_dataclass(RawLoans))


@pytest.fixture
def raw_table() -> pa.Table:
    return _raw_table(_raw_rows())


@pytest.fixture
def as_of() -> dt.date:
    return dt.date(2024, 12, 1)


def _stub_fetch(table: pa.Table):
    """A fetch tool that ignores lane/limit and returns the given table."""
    def _fetch(*, lane="collections", limit=None):
        return table
    return _fetch


# --------------------------------------------------------------------------- #
# IngestAgent — produces RawLoans handle; stubbed fetch; freshness/schema OK.
# --------------------------------------------------------------------------- #
def test_ingest_agent_produces_rawloans_handle(raw_table):
    agent = IngestAgent(MockLLM(), limit=999)
    agent.register_tool("fetch", _stub_fetch(raw_table))
    ctx = AgentContext(lane="collections", data_handles={})
    res = agent.run(ctx)
    assert res.ok
    assert res.artifact_ref == "raw_loans"
    # The table is published on the shared store.
    assert ctx.data_handles["raw_loans"].num_rows == raw_table.num_rows
    # Steps recorded.
    assert any(s.action == "fetch_loans" and s.status == Status.OK for s in agent.steps)
    assert any(s.action == "freshness_check" for s in agent.steps)


def test_ingest_agent_blocks_on_zero_rows(raw_table):
    """Zero-row read → BLOCKED (stale/empty source)."""
    empty = _raw_table(_raw_rows())[:0]  # zero-row RawLoans-shaped table
    agent = IngestAgent(MockLLM())
    agent.register_tool("fetch", _stub_fetch(empty))
    res = agent.run(AgentContext(lane="collections", data_handles={}))
    assert res.status == Status.BLOCKED
    assert "zero rows" in res.notes.lower()


def test_ingest_agent_errors_on_schema_drift(raw_table):
    """A table missing a RawLoans field → ERROR (validate_table up front)."""
    bad = raw_table.drop(["grade"])
    agent = IngestAgent(MockLLM())
    agent.register_tool("fetch", _stub_fetch(bad))
    res = agent.run(AgentContext(lane="collections", data_handles={}))
    assert res.status == Status.ERROR
    assert "schema" in res.notes.lower() or "missing" in res.notes.lower()


# --------------------------------------------------------------------------- #
# AnalyticsAgent — consumes RawLoans, produces FeatureFrame, null-rate check.
# --------------------------------------------------------------------------- #
def test_analytics_agent_produces_featureframe(raw_table, as_of):
    ingest = IngestAgent(MockLLM())
    ingest.register_tool("fetch", _stub_fetch(raw_table))
    ctx = AgentContext(lane="collections", data_handles={})
    r1 = ingest.run(ctx)
    ctx = ctx.with_result(r1)  # thread forward so analytics sees raw_loans

    analytics = AnalyticsAgent(MockLLM(), as_of=as_of)
    r2 = analytics.run(ctx)
    assert r2.ok
    assert r2.artifact_ref == "feature_frame"
    frame = ctx.data_handles["feature_frame"]
    validate_table(frame, FeatureFrame, name="analytics frame")
    # Null-rate check recorded (acceptance: "surfaces feature-null rates").
    assert any(s.action == "null_rate_check" for s in analytics.steps)


def test_analytics_agent_errors_without_predecessor():
    analytics = AnalyticsAgent(MockLLM())
    res = analytics.run(AgentContext(lane="collections", data_handles={}))
    assert res.status == Status.ERROR


# --------------------------------------------------------------------------- #
# RiskModelAgent — consumes FeatureFrame, produces ScoredAccounts, flags bands.
# --------------------------------------------------------------------------- #
def test_risk_model_agent_produces_scored_accounts(raw_table, as_of):
    ctx = AgentContext(lane="collections", data_handles={})
    i = IngestAgent(MockLLM()); i.register_tool("fetch", _stub_fetch(raw_table))
    ctx = ctx.with_result(i.run(ctx))
    a = AnalyticsAgent(MockLLM(), as_of=as_of)
    ctx = ctx.with_result(a.run(ctx))
    agent = RiskModelAgent(MockLLM())
    r3 = agent.run(ctx)

    assert r3.ok
    assert r3.artifact_ref == "scored_accounts"
    scored = ctx.data_handles["scored_accounts"]
    validate_table(scored, ScoredAccounts, name="risk_model scored")
    # Flags the highest band (acceptance: "flags score bands").
    assert any(s.action == "score_bands" and "Q5" in s.notes for s in agent.steps)


def test_risk_model_agent_errors_without_predecessor():
    agent = RiskModelAgent(MockLLM())
    res = agent.run(AgentContext(lane="collections", data_handles={}))
    assert res.status == Status.ERROR


# --------------------------------------------------------------------------- #
# InsightAgent — produces DashboardPayload + alert summary; uses ApprovalGate.
# --------------------------------------------------------------------------- #
def test_insight_agent_produces_payload_and_summary(raw_table, as_of):
    ctx = AgentContext(lane="collections", data_handles={})
    gate = ApprovalGate(auto_approve=True)  # smoke run
    i = IngestAgent(MockLLM()); i.register_tool("fetch", _stub_fetch(raw_table))
    ctx = ctx.with_result(i.run(ctx))
    ctx = ctx.with_result(AnalyticsAgent(MockLLM(), as_of=as_of).run(ctx))
    ctx = ctx.with_result(RiskModelAgent(MockLLM()).run(ctx))
    insight = InsightAgent(MockLLM(), gate=gate, top_n=10)
    r4 = insight.run(ctx)

    assert r4.ok
    assert r4.artifact_ref == "dashboard_payload"
    payload = ctx.data_handles["dashboard_payload"]
    assert set(payload.keys()) == {"work_list", "portfolio_health", "alerts"}
    # Always emits ≥1 human-readable alert summary string.
    summary = ctx.data_handles["alert_summary"]
    assert isinstance(summary, str) and summary
    # Approval gate was invoked before the work-list was released.
    assert any(s.action == "publish_work_list" for s in gate.steps)


def test_insight_agent_rejection_blocks_work_list(raw_table, as_of):
    """ApprovalGate rejection → insight agent returns BLOCKED."""
    ctx = AgentContext(lane="collections", data_handles={})
    gate = ApprovalGate(decide=lambda a, r: Rejected(action=a, rationale=r, reason="not now"))
    i = IngestAgent(MockLLM()); i.register_tool("fetch", _stub_fetch(raw_table))
    ctx = ctx.with_result(i.run(ctx))
    ctx = ctx.with_result(AnalyticsAgent(MockLLM(), as_of=as_of).run(ctx))
    ctx = ctx.with_result(RiskModelAgent(MockLLM()).run(ctx))
    r4 = InsightAgent(MockLLM(), gate=gate).run(ctx)

    assert r4.status == Status.BLOCKED
    assert "rejected" in r4.notes.lower()
    # No payload published.
    assert "dashboard_payload" not in ctx.data_handles


def test_insight_agent_errors_without_predecessor():
    agent = InsightAgent(MockLLM(), gate=ApprovalGate(auto_approve=True))
    res = agent.run(AgentContext(lane="collections", data_handles={}))
    assert res.status == Status.ERROR


# --------------------------------------------------------------------------- #
# Full chain — the four agents hand off end-to-end on stubbed data.
# --------------------------------------------------------------------------- #
def test_full_pipeline_chain_end_to_end(raw_table, as_of):
    ctx = AgentContext(lane="collections", data_handles={})
    gate = ApprovalGate(auto_approve=True)
    agents = [
        IngestAgent(MockLLM()),
        AnalyticsAgent(MockLLM(), as_of=as_of),
        RiskModelAgent(MockLLM()),
        InsightAgent(MockLLM(), gate=gate, top_n=15),
    ]
    agents[0].register_tool("fetch", _stub_fetch(raw_table))

    results = []
    for agent in agents:
        res = agent.run(ctx)
        results.append(res)
        if not res.ok:
            break
        ctx = ctx.with_result(res)

    assert all(r.ok for r in results), [r.notes for r in results]
    # Artifact handles thread through the chain.
    assert results[0].artifact_ref == "raw_loans"
    assert results[1].artifact_ref == "feature_frame"
    assert results[2].artifact_ref == "scored_accounts"
    assert results[3].artifact_ref == "dashboard_payload"
    # Final payload is the DashboardPayload the frontend consumes.
    payload = ctx.data_handles["dashboard_payload"]
    assert isinstance(payload["work_list"], list)
    assert len(payload["work_list"]) <= 15
