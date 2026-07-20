"""WA-083 — real dlt load path acceptance.

dlt was a declared-but-unused dependency; this makes it the real load engine (opt-in). The
load runs an Arrow table through a dlt pipeline into DuckDB with merge dedup on ``loan_id``, a
schema contract, and ``_dlt_loads`` lineage the Data Engineer can cite. The in-memory path stays
the default so offline/CI is byte-for-byte unchanged.
"""
from __future__ import annotations

import pyarrow as pa
import pytest

from waspada.agents import AgentContext, MockLLM, Status
from waspada.agents.data_engineer import DataEngineerAgent, _use_dlt
from waspada.agents.__main__ import _sample_raw_table
from waspada.data.lakehouse import Lakehouse, load_via_dlt

import importlib.util
# dlt is an OPT-IN load engine, not in the deploy manifest (api/requirements.txt) — so the
# CI pytest gate + the FC image run without it and take the in-memory fallback. Tests that
# exercise the dlt-present branch skip when it's absent; the fallback/flag tests still run
# (they ARE the production path).
_HAS_DLT = importlib.util.find_spec("dlt") is not None
_needs_dlt = pytest.mark.skipif(not _HAS_DLT, reason="dlt not installed (opt-in load engine)")


def _stub_fetch(table: pa.Table):
    def _fetch(*, lane="collections", limit=None):
        return table
    return _fetch


# --------------------------------------------------------------------------- #
# load_via_dlt — the engine.
# --------------------------------------------------------------------------- #
@_needs_dlt
def test_load_via_dlt_returns_queryable_lakehouse_with_lineage():
    lh = load_via_dlt(_sample_raw_table(n=200, seed=3), table="raw_loans")
    assert isinstance(lh, Lakehouse)
    assert lh.scalar(f"SELECT count(*) FROM {lh.table}") == 200
    # clean RawLoans table — Arrow loads carry no _dlt_* row columns
    cols = lh.arrow(f"SELECT * FROM {lh.table} LIMIT 0").column_names
    assert not [c for c in cols if c.startswith("_dlt")]
    # lineage the Data Engineer cites
    lin = lh.lineage
    assert lin["engine"] == "dlt" and lin["rows_loaded"] == 200
    assert lin["load_id"] and lin["primary_key"] == "loan_id"


@_needs_dlt
def test_load_via_dlt_merge_dedups_on_loan_id(tmp_path):
    book = _sample_raw_table(n=300, seed=9)
    d = str(tmp_path / "pipe")
    lh1 = load_via_dlt(book, pipelines_dir=d)
    lh1.con.close()  # release the read-only lock so the next merge-load can open the file for write
    lh2 = load_via_dlt(book, pipelines_dir=d)  # same 300 loan_ids again -> merge, not append
    assert lh2.scalar(f"SELECT count(*) FROM {lh2.table}") == 300  # deduped, not 600
    assert lh2.lineage["loads_recorded"] >= 2                      # multiple loads recorded
    lh2.con.close()


# --------------------------------------------------------------------------- #
# Data Engineer opt-in — WASPADA_USE_DLT.
# --------------------------------------------------------------------------- #
def test_use_dlt_flag(monkeypatch):
    monkeypatch.delenv("WASPADA_USE_DLT", raising=False)
    assert _use_dlt() is False
    monkeypatch.setenv("WASPADA_USE_DLT", "1")
    assert _use_dlt() is True
    monkeypatch.setenv("WASPADA_USE_DLT", "true")
    assert _use_dlt() is True


@_needs_dlt
def test_data_engineer_uses_dlt_when_opted_in(monkeypatch):
    monkeypatch.setenv("WASPADA_USE_DLT", "1")
    raw = _sample_raw_table(n=150, seed=4)
    agent = DataEngineerAgent(MockLLM())          # canned brain -> default check set
    agent.register_tool("fetch", _stub_fetch(raw))
    res = agent.run(AgentContext(lane="collections", data_handles={}))

    assert res.ok                                   # gate still passes over the dlt-loaded table
    # the dlt load ran + was logged with lineage
    dlt_steps = [s for s in agent.steps if s.action == "dlt_load"]
    assert dlt_steps and "rows=150" in dlt_steps[0].notes
    assert agent.lakehouse.lineage and agent.lakehouse.lineage["engine"] == "dlt"


def test_data_engineer_default_is_in_memory(monkeypatch):
    monkeypatch.delenv("WASPADA_USE_DLT", raising=False)
    raw = _sample_raw_table(n=100, seed=4)
    agent = DataEngineerAgent(MockLLM())
    agent.register_tool("fetch", _stub_fetch(raw))
    res = agent.run(AgentContext(lane="collections", data_handles={}))
    assert res.ok
    assert not any(s.action == "dlt_load" for s in agent.steps)   # no dlt path
    assert agent.lakehouse.lineage is None                        # in-memory registration
