"""Data Analyst agent tests (WA-030 acceptance).

The Tier-2 Data Analyst agent promotes analytics from a deterministic step into
a qwen3.7-plus function-calling loop over DuckDB SQL. These tests pin the
WA-030 contract:

  * real multi-hop tool loop — the brain picks each next exploration from what
    it saw, verifiable in the step log (not a fixed sequence).
  * the deterministic FeatureFrame core stays intact — ``feature_frame`` is
    byte-for-byte identical to ``build_features`` output (regression guard).
  * an unparsable tool step or unavailable brain falls back to the deterministic
    frame + empty/partial aggregates; pipeline never crashes.
  * clean data -> OK and publishes both ``feature_frame`` and
    ``analyst_aggregates`` handles.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json

import pyarrow as pa
import pytest

from waspada.agents import AgentContext, MockLLM, Status
from waspada.agents.data_analyst import DataAnalystAgent, DEFAULT_EXPLORE_BUDGET
from waspada.features.collections import build_features
from waspada.schema import RawLoans, schema_from_dataclass


# --------------------------------------------------------------------------- #
# Synthetic RawLoans fixture (shared shape with the pipeline-agents test).
# --------------------------------------------------------------------------- #
def _raw_rows(n: int = 24, seed: int = 11) -> list[dict]:
    import numpy as np

    rng = np.random.default_rng(seed)
    rows: list[dict] = []
    for i in range(n):
        rows.append(dict(
            loan_id=f"R{i:04d}",
            amount=float(rng.uniform(2000, 25000)),
            term=int(rng.choice([36, 60])),
            rate=float(rng.uniform(4, 10)),
            grade="A",
            annual_income=float(rng.uniform(30000, 120000)),
            dti=float(rng.uniform(2, 12)),
            issue_date=dt.date(2021, 6, 1),
            purpose="car",
            region="West",
            outstanding_principal=float(rng.uniform(100, 5000)),
            total_paid=float(rng.uniform(100, 5000)),
            current_status="Current",
        ))
    return rows


def _raw_table(n: int = 24, seed: int = 11) -> pa.Table:
    rows = _raw_rows(n, seed)
    cols = {f.name: [] for f in dataclasses.fields(RawLoans)}
    for r in rows:
        for name in cols:
            cols[name].append(r[name])
    return pa.table(cols, schema=schema_from_dataclass(RawLoans))


@pytest.fixture
def raw_table() -> pa.Table:
    return _raw_table()


@pytest.fixture
def as_of() -> dt.date:
    return dt.date(2024, 12, 1)


# --------------------------------------------------------------------------- #
# Drop-in replacement for AnalyticsAgent: produces the feature_frame handle.
# --------------------------------------------------------------------------- #
def test_data_analyst_produces_feature_frame_handle(raw_table, as_of):
    agent = DataAnalystAgent(MockLLM(), as_of=as_of)  # unparsable -> no aggregates
    ctx = AgentContext(
        lane="collections",
        data_handles={"raw_loans": raw_table},
        prior_results=[__import__("waspada.agents.protocol", fromlist=["AgentResult"]).AgentResult(status=Status.OK, artifact_ref="raw_loans")],
    )
    res = agent.run(ctx)
    assert res.ok
    assert res.artifact_ref == "feature_frame"
    assert ctx.data_handles["feature_frame"].num_rows == raw_table.num_rows
    assert "analyst_aggregates" in ctx.data_handles


def test_data_analyst_errors_without_predecessor(raw_table, as_of):
    agent = DataAnalystAgent(MockLLM(), as_of=as_of)
    ctx = AgentContext(lane="collections", data_handles={})
    res = agent.run(ctx)
    assert res.status == Status.ERROR


def test_data_analyst_errors_when_handle_missing(raw_table, as_of):
    agent = DataAnalystAgent(MockLLM(), as_of=as_of)
    ctx = AgentContext(
        lane="collections",
        data_handles={},
        prior_results=[__import__("waspada.agents.protocol", fromlist=["AgentResult"]).AgentResult(status=Status.OK, artifact_ref="raw_loans")],
    )
    res = agent.run(ctx)
    assert res.status == Status.ERROR


# --------------------------------------------------------------------------- #
# WA-030 headline regression guard: FeatureFrame identical to build_features().
# --------------------------------------------------------------------------- #
def test_feature_frame_matches_build_features_regression_guard(raw_table, as_of):
    agent = DataAnalystAgent(MockLLM(), as_of=as_of)
    ctx = AgentContext(
        lane="collections",
        data_handles={"raw_loans": raw_table},
        prior_results=[__import__("waspada.agents.protocol", fromlist=["AgentResult"]).AgentResult(status=Status.OK, artifact_ref="raw_loans")],
    )
    agent.run(ctx)

    expected = build_features(raw_table, as_of)
    actual = ctx.data_handles["feature_frame"]

    assert actual.schema.equals(expected.schema)
    assert actual.num_columns == expected.num_columns
    for name in expected.column_names:
        assert actual.column(name).equals(expected.column(name)), f"column {name!r} differs"


# --------------------------------------------------------------------------- #
# WA-030 headline: REAL multi-hop function-calling loop, verifiable in steps.
# --------------------------------------------------------------------------- #
def test_real_multi_hop_loop_runs_explorations_in_sequence(raw_table, as_of):
    """A scripted brain drives query -> correlation -> distribution ->
    build_feature -> done. Each hop's tool call is recorded, in order."""
    script = [
        json.dumps({"tool": "query", "arg": "SELECT grade, COUNT(*) FROM raw_loans GROUP BY grade LIMIT 10"}),
        json.dumps({"tool": "correlation", "arg": '{"a": "dti", "b": "rate"}'}),
        json.dumps({"tool": "distribution", "arg": "dti"}),
        json.dumps({"tool": "build_feature", "arg": "SELECT grade, AVG(payment_ratio) FROM feature_frame GROUP BY grade"}),
        json.dumps({"tool": "done"}),
    ]
    agent = DataAnalystAgent(MockLLM(script=script), as_of=as_of)
    ctx = AgentContext(
        lane="collections",
        data_handles={"raw_loans": raw_table},
        prior_results=[__import__("waspada.agents.protocol", fromlist=["AgentResult"]).AgentResult(status=Status.OK, artifact_ref="raw_loans")],
    )
    res = agent.run(ctx)

    assert res.ok
    # The explorations actually invoked, in order — a real multi-hop trail.
    tools = [s.action.split(":", 1)[1]
             for s in agent.steps if s.action.startswith("da_tool:")]
    assert tools == ["query", "correlation", "distribution", "build_feature"]
    # The brain signalled done (loop terminated cleanly, not by budget).
    assert any(s.action == "da_done" for s in agent.steps)
    # Each hop recorded a thinking step — the loop is auditable.
    think_steps = [s for s in agent.steps if s.action == "da_think"]
    assert len(think_steps) >= 4
    # Aggregates populated.
    aggregates = ctx.data_handles["analyst_aggregates"]
    assert len(aggregates["queries_run"]) == 4


def test_loop_terminates_on_done_before_budget(raw_table, as_of):
    """A brain that signals done on hop 0 terminates immediately."""
    agent = DataAnalystAgent(
        MockLLM(script=[json.dumps({"tool": "done"})]),
        as_of=as_of,
        explore_budget=DEFAULT_EXPLORE_BUDGET,
    )
    ctx = AgentContext(
        lane="collections",
        data_handles={"raw_loans": raw_table},
        prior_results=[__import__("waspada.agents.protocol", fromlist=["AgentResult"]).AgentResult(status=Status.OK, artifact_ref="raw_loans")],
    )
    res = agent.run(ctx)
    assert res.ok
    assert ctx.data_handles["analyst_aggregates"]["queries_run"] == []


def test_loop_returns_partial_on_unparsable_brain(raw_table, as_of):
    """An unparsable brain (canned MockLLM) -> frame OK, aggregates empty."""
    agent = DataAnalystAgent(MockLLM(), as_of=as_of)  # reply="mock-llm-ok" -> unparsable
    ctx = AgentContext(
        lane="collections",
        data_handles={"raw_loans": raw_table},
        prior_results=[__import__("waspada.agents.protocol", fromlist=["AgentResult"]).AgentResult(status=Status.OK, artifact_ref="raw_loans")],
    )
    res = agent.run(ctx)
    assert res.ok
    # Frame still produced.
    assert ctx.data_handles["feature_frame"].num_rows == raw_table.num_rows
    # Aggregates empty because loop stopped on first unparsable turn.
    assert ctx.data_handles["analyst_aggregates"]["queries_run"] == []
    assert any(s.action == "da_unparsable" and s.status == Status.ERROR for s in agent.steps)


def test_loop_returns_partial_on_brain_error(raw_table, as_of):
    """A brain that raises -> frame OK, aggregates partial/empty."""
    class RaisingLLM:
        def complete(self, _prompt):
            raise RuntimeError("simulated llm failure")

    agent = DataAnalystAgent(RaisingLLM(), as_of=as_of)
    ctx = AgentContext(
        lane="collections",
        data_handles={"raw_loans": raw_table},
        prior_results=[__import__("waspada.agents.protocol", fromlist=["AgentResult"]).AgentResult(status=Status.OK, artifact_ref="raw_loans")],
    )
    res = agent.run(ctx)
    assert res.ok
    assert ctx.data_handles["feature_frame"].num_rows == raw_table.num_rows
    assert any(s.action == "da_loop" and s.status == Status.ERROR for s in agent.steps)


# --------------------------------------------------------------------------- #
# Tool safety: SELECT-only, row caps, no chained statements.
# --------------------------------------------------------------------------- #
def test_query_tool_rejects_non_select(raw_table, as_of):
    agent = DataAnalystAgent(MockLLM(), as_of=as_of)
    ctx = AgentContext(
        lane="collections",
        data_handles={"raw_loans": raw_table},
        prior_results=[__import__("waspada.agents.protocol", fromlist=["AgentResult"]).AgentResult(status=Status.OK, artifact_ref="raw_loans")],
    )
    agent.run(ctx)

    script = [json.dumps({"tool": "query", "arg": "DROP TABLE raw_loans"})]
    agent2 = DataAnalystAgent(MockLLM(script=script), as_of=as_of)
    ctx2 = AgentContext(
        lane="collections",
        data_handles={"raw_loans": raw_table},
        prior_results=[__import__("waspada.agents.protocol", fromlist=["AgentResult"]).AgentResult(status=Status.OK, artifact_ref="raw_loans")],
    )
    agent2.run(ctx2)
    tool_steps = [s for s in agent2.steps if s.action == "da_tool:query"]
    assert tool_steps
    assert "error" in tool_steps[0].notes.lower()


def test_query_tool_caps_rows(raw_table, as_of):
    script = [json.dumps({"tool": "query", "arg": "SELECT * FROM raw_loans"}), json.dumps({"tool": "done"})]
    agent = DataAnalystAgent(MockLLM(script=script), as_of=as_of)
    ctx = AgentContext(
        lane="collections",
        data_handles={"raw_loans": raw_table},
        prior_results=[__import__("waspada.agents.protocol", fromlist=["AgentResult"]).AgentResult(status=Status.OK, artifact_ref="raw_loans")],
    )
    agent.run(ctx)
    aggregates = ctx.data_handles["analyst_aggregates"]
    reply = json.loads(aggregates["queries_run"][0]["reply"])
    assert reply["count"] <= 200
