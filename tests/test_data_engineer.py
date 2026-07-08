"""Data Engineer agent tests (WA-029 acceptance).

The Tier-2 Data Engineer agent promotes ingest from a deterministic step into a
qwen3.6-flash function-calling loop over data quality. These tests pin the
WA-029 contract:

  * real multi-hop tool loop — the brain picks each next check from what it
    saw, verifiable in the step log (not a fixed sequence).
  * the deterministic freshness + schema gate stays the core — dirty/malformed
    data -> ERROR/BLOCKED loud; zero rows -> BLOCKED.
  * an unparsable tool step falls back to the default check set — validation is
    never skipped.
  * clean data -> OK and publishes the RawLoans handle (drop-in for IngestAgent).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json

import pyarrow as pa
import pytest

from waspada.agents import AgentContext, MockLLM, Status
from waspada.agents.data_engineer import (
    DEFAULT_CHECK_BUDGET,
    DEFAULT_CHECK_SET,
    DataEngineerAgent,
)
from waspada.schema import RawLoans, schema_from_dataclass


# --------------------------------------------------------------------------- #
# Synthetic RawLoans fixture (shared shape with the pipeline-agents test).
# --------------------------------------------------------------------------- #
def _raw_rows(n: int = 24, seed: int = 11, *, dirty: bool = False) -> list[dict]:
    import numpy as np

    rng = np.random.default_rng(seed)
    rows: list[dict] = []
    for i in range(n):
        dti = float(rng.uniform(2, 12))
        if dirty and i == 0:
            dti = 150.0  # anomaly: dti > 100
        rows.append(dict(
            loan_id=f"R{i:04d}",
            amount=float(rng.uniform(2000, 25000)),
            term=int(rng.choice([36, 60])),
            rate=float(rng.uniform(4, 10)),
            grade="A",
            annual_income=float(rng.uniform(30000, 120000)),
            dti=dti,
            issue_date=dt.date(2021, 6, 1),
            purpose="car",
            region="West",
            outstanding_principal=float(rng.uniform(100, 5000)),
            total_paid=float(rng.uniform(100, 5000)),
            current_status="Current",
        ))
    return rows


def _raw_table(n: int = 24, seed: int = 11, *, dirty: bool = False) -> pa.Table:
    rows = _raw_rows(n, seed, dirty=dirty)
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
def raw_table() -> pa.Table:
    return _raw_table()


# --------------------------------------------------------------------------- #
# Drop-in replacement for IngestAgent: produces the raw_loans handle on OK.
# --------------------------------------------------------------------------- #
def test_data_engineer_produces_rawloans_handle(raw_table):
    agent = DataEngineerAgent(MockLLM())  # unparsable -> default checks
    agent.register_tool("fetch", _stub_fetch(raw_table))
    ctx = AgentContext(lane="collections", data_handles={})
    res = agent.run(ctx)
    assert res.ok
    assert res.artifact_ref == "raw_loans"
    assert ctx.data_handles["raw_loans"].num_rows == raw_table.num_rows
    # The deterministic gate steps are recorded.
    assert any(s.action == "fetch_loans" and s.status == Status.OK for s in agent.steps)
    assert any(s.action == "freshness_check" for s in agent.steps)


# --------------------------------------------------------------------------- #
# The deterministic gate stays the core.
# --------------------------------------------------------------------------- #
def test_data_engineer_errors_on_schema_drift(raw_table):
    bad = raw_table.drop(["grade"])
    agent = DataEngineerAgent(MockLLM())
    agent.register_tool("fetch", _stub_fetch(bad))
    res = agent.run(AgentContext(lane="collections", data_handles={}))
    assert res.status == Status.ERROR
    assert "schema" in res.notes.lower() or "missing" in res.notes.lower()


def test_data_engineer_blocks_on_zero_rows(raw_table):
    empty = raw_table[:0]
    agent = DataEngineerAgent(MockLLM())
    agent.register_tool("fetch", _stub_fetch(empty))
    res = agent.run(AgentContext(lane="collections", data_handles={}))
    assert res.status == Status.BLOCKED
    assert "zero rows" in res.notes.lower()


# --------------------------------------------------------------------------- #
# WA-029 headline: REAL multi-hop function-calling loop, verifiable in steps.
# --------------------------------------------------------------------------- #
def test_real_multi_hop_loop_picks_checks_in_sequence(raw_table):
    """A scripted brain drives validate_schema -> null_rates -> profile_column
    -> detect_anomalies -> done. Each hop's tool call is recorded, in order."""
    script = [
        json.dumps({"tool": "validate_schema"}),
        json.dumps({"tool": "null_rates"}),
        json.dumps({"tool": "profile_column", "arg": "dti"}),
        json.dumps({"tool": "detect_anomalies"}),
        json.dumps({"tool": "done"}),
    ]
    agent = DataEngineerAgent(MockLLM(script=script))
    agent.register_tool("fetch", _stub_fetch(raw_table))
    res = agent.run(AgentContext(lane="collections", data_handles={}))

    assert res.ok
    # The checks actually invoked, in order — a real multi-hop trail, not a
    # fixed sequence the agent runs blindly.
    checks = [s.action.split(":", 1)[1]
              for s in agent.steps if s.action.startswith("de_tool:")]
    assert checks == ["validate_schema", "null_rates", "profile_column", "detect_anomalies"]
    # The brain signalled done (loop terminated cleanly, not by budget).
    assert any(s.action == "de_done" for s in agent.steps)
    # Each hop recorded a thinking step (the raw reply) — the loop is auditable.
    think_steps = [s for s in agent.steps if s.action == "de_think"]
    assert len(think_steps) >= 4


def test_loop_terminates_on_done_before_budget(raw_table):
    """A brain that signals done on hop 0 terminates immediately (no budget burn)."""
    agent = DataEngineerAgent(MockLLM(script=[json.dumps({"tool": "done"})]),
                              check_budget=DEFAULT_CHECK_BUDGET)
    agent.register_tool("fetch", _stub_fetch(raw_table))
    agent.run(AgentContext(lane="collections", data_handles={}))
    # Default checks run because the loop did no work before signalling done.
    assert any(s.action == "de_default:null_rates" for s in agent.steps)


def test_loop_runs_default_set_on_unparsable_brain(raw_table):
    """An unparsable brain (canned MockLLM) -> default check set runs anyway."""
    agent = DataEngineerAgent(MockLLM())  # reply="mock-llm-ok" -> unparsable
    agent.register_tool("fetch", _stub_fetch(raw_table))
    res = agent.run(AgentContext(lane="collections", data_handles={}))
    assert res.ok
    # Default checks recorded, and validation was NOT skipped.
    for tool in DEFAULT_CHECK_SET:
        assert any(s.action == f"de_default:{tool}" for s in agent.steps), tool
    assert any(s.action == "de_unparsable" and s.status == Status.ERROR for s in agent.steps)


# --------------------------------------------------------------------------- #
# Dirty data -> BLOCKED (gate still fails loud).
# --------------------------------------------------------------------------- #
def test_dirty_data_blocks_via_anomaly_detection():
    """A dti>100 row is flagged by detect_anomalies -> gate BLOCKS."""
    raw = _raw_table(dirty=True)
    script = [
        json.dumps({"tool": "null_rates"}),
        json.dumps({"tool": "detect_anomalies"}),
        json.dumps({"tool": "done"}),
    ]
    agent = DataEngineerAgent(MockLLM(script=script))
    agent.register_tool("fetch", _stub_fetch(raw))
    res = agent.run(AgentContext(lane="collections", data_handles={}))
    assert res.status == Status.BLOCKED
    assert "quality_gate" in res.notes.lower() or "data_engineer gate failed" in res.notes


def test_dirty_data_blocks_even_on_unparsable_brain():
    """The default check set (run on unparsable brain) still catches anomalies."""
    raw = _raw_table(dirty=True)
    agent = DataEngineerAgent(MockLLM())  # unparsable -> defaults
    agent.register_tool("fetch", _stub_fetch(raw))
    res = agent.run(AgentContext(lane="collections", data_handles={}))
    assert res.status == Status.BLOCKED


# --------------------------------------------------------------------------- #
# Quality tools are stubbable (same register_tool pattern as ingest fetch).
# --------------------------------------------------------------------------- #
def test_quality_tools_are_stubbbable(raw_table):
    """A caller can override the quality tools via register_tool."""
    calls: list[str] = []
    def _fake_null_rates(lh, *_a):
        calls.append("null_rates")
        return {"n_rows": 1, "null_rates": {"loan_id": 0.0}}

    agent = DataEngineerAgent(MockLLM(script=[
        json.dumps({"tool": "null_rates"}),
        json.dumps({"tool": "done"}),
    ]))
    agent.register_tool("fetch", _stub_fetch(raw_table))
    agent.register_tool("null_rates", _fake_null_rates)
    res = agent.run(AgentContext(lane="collections", data_handles={}))
    assert res.ok
    assert "null_rates" in calls


# --------------------------------------------------------------------------- #
# Hop budget terminates a brain stuck in a loop.
# --------------------------------------------------------------------------- #
def test_budget_terminates_looping_brain(raw_table):
    """A brain that never says done and keeps picking null_rates is bounded."""
    agent = DataEngineerAgent(
        MockLLM(script=[json.dumps({"tool": "null_rates"})]),  # repeats last
        check_budget=3,
    )
    agent.register_tool("fetch", _stub_fetch(raw_table))
    res = agent.run(AgentContext(lane="collections", data_handles={}))
    assert res.ok  # clean data, just a looping brain
    # The budget step is recorded — the loop was bounded, not infinite.
    assert any(s.action == "de_budget_exhausted" for s in agent.steps)
    # No more than check_budget tool invocations.
    tool_steps = [s for s in agent.steps if s.action.startswith("de_tool:")]
    assert len(tool_steps) <= 3


# --------------------------------------------------------------------------- #
# Lakehouse is exposed for audit after run().
# --------------------------------------------------------------------------- #
def test_lakehouse_exposed_after_run(raw_table):
    agent = DataEngineerAgent(MockLLM())
    agent.register_tool("fetch", _stub_fetch(raw_table))
    agent.run(AgentContext(lane="collections", data_handles={}))
    assert agent.lakehouse is not None
    assert agent.lakehouse.table == "raw_loans"
    # The Lakehouse reads the same row count as the source table.
    assert agent.lakehouse.scalar("SELECT COUNT(*) FROM raw_loans") == raw_table.num_rows
