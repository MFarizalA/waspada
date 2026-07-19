"""WA-080 — parallel risk-audit acceptance.

The Skeptic audits K accounts, each an independent LLM tool-loop. That loop is
the dominant cost of a live-Qwen run (up to K × _MAX_TOOL_TURNS sequential
calls), and on prod it overran the FC invocation timeout — the live debate spun
and died before rendering. WA-080 audits the K accounts concurrently.

What must hold:
  * determinism — the parallel path opens the SAME disputes in the SAME order as
    the sequential path (the audit runs concurrently; disputes are aggregated in
    ``top`` order). This is what lets the change be a pure speed-up, not a
    behaviour change.
  * concurrency actually engages — a brain that records its own max in-flight
    count proves the pool overlaps calls (>1) under max_workers>1 and never
    overlaps (==1) at the default.
  * graceful degrade — a brain that always errors yields no disputes and a clean
    OK run under parallelism, never a crash.
  * the default is sequential — max_workers=1 is byte-for-byte the pre-WA-080
    loop, which is why the scripted-MockLLM suite stays green.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

import pyarrow as pa
import pytest

from waspada.agents import AgentContext, MockLLM, Status
from waspada.agents.analytics import AnalyticsAgent
from waspada.agents.ingest import IngestAgent
from waspada.agents.llm import LLM, ChatResponse
from waspada.agents.protocol import AgentResult
from waspada.agents.risk_auditor import RiskAuditorAgent
from waspada.agents.risk_model import RiskModelAgent
from waspada.schema import RawLoans, schema_from_dataclass


# --------------------------------------------------------------------------- #
# A real scored_accounts fixture (ingest→analytics→risk_model), so the auditor
# sees a genuine table with every column it reads.
# --------------------------------------------------------------------------- #
def _raw_rows(n: int = 60, seed: int = 11) -> List[dict]:
    import numpy as np
    rng = np.random.default_rng(seed)
    issue_years = [2019, 2020, 2021, 2022, 2023]
    rows: List[dict] = []
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


def _raw_table(rows: List[dict]) -> pa.Table:
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
def scored_ctx() -> AgentContext:
    raw = _raw_table(_raw_rows())
    ctx = AgentContext(lane="collections", data_handles={})
    ingest = IngestAgent(MockLLM())
    ingest.register_tool("fetch", _stub_fetch(raw))
    ctx = ctx.with_result(ingest.run(ctx))
    ctx = ctx.with_result(AnalyticsAgent(MockLLM(), as_of=dt.date(2024, 12, 1)).run(ctx))
    ctx = ctx.with_result(RiskModelAgent(MockLLM()).run(ctx))
    assert ctx.data_handles["scored_accounts"].num_rows > 0
    return ctx


def _fresh_ctx(scored_ctx: AgentContext) -> AgentContext:
    """A context that resolves scored_accounts for a standalone auditor run."""
    ctx = AgentContext(lane="collections", data_handles=dict(scored_ctx.data_handles))
    return ctx.with_result(AgentResult(
        status=Status.OK, agent="risk_model", artifact_ref="scored_accounts"))


# --------------------------------------------------------------------------- #
# Thread-safe test brains.
# --------------------------------------------------------------------------- #
class _ConstBrain(LLM):
    """Returns a fixed auditor view, thread-safely; records max concurrency.

    One ``chat`` call per audited account (no tool_calls → the loop finishes on
    turn 0). The small sleep widens the overlap window so a real thread pool is
    observably concurrent. ``with_model`` returns self (tier is only a label).
    """

    name = "const"
    model_name = "const-flash"

    def __init__(self, view: str = "Low") -> None:
        self._view = view
        self._lock = threading.Lock()
        self._active = 0
        self.max_concurrent = 0
        self.n_calls = 0

    def _payload(self) -> str:
        return json.dumps({
            "auditor_view": self._view, "confidence": 0.8,
            "claim": f"auditor reads this as {self._view} risk",
            "evidence": ["synthetic-evidence"],
        })

    def complete(self, prompt: str, *, history: Optional[Sequence[str]] = None) -> str:
        return self._payload()

    def chat(self, prompt: str, *, tools: Optional[List[Dict[str, Any]]] = None,
             messages: Optional[List[Dict[str, Any]]] = None) -> ChatResponse:
        with self._lock:
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
            self.n_calls += 1
        try:
            time.sleep(0.02)
            return ChatResponse(content=self._payload(), tool_calls=[])
        finally:
            with self._lock:
                self._active -= 1

    def with_model(self, model: str) -> "LLM":
        return self


class _RaisingBrain(LLM):
    """Errors on every call (both surfaces) — forces graceful-degrade."""

    name = "boom"
    model_name = "boom"

    def complete(self, prompt: str, *, history: Optional[Sequence[str]] = None) -> str:
        raise RuntimeError("brain unreachable")

    def chat(self, prompt: str, *, tools=None, messages=None) -> ChatResponse:
        raise RuntimeError("brain unreachable")

    def with_model(self, model: str) -> "LLM":
        return self


def _dispute_ids(agent: RiskAuditorAgent, ctx: AgentContext) -> List[str]:
    agent.run(ctx)
    return [d.loan_id for d in (ctx.data_handles.get("risk_disputes") or [])]


# --------------------------------------------------------------------------- #
# Tests.
# --------------------------------------------------------------------------- #
def test_parallel_matches_sequential_exactly(scored_ctx):
    """Same disputes, same order — parallel is a pure speed-up."""
    seq = _dispute_ids(RiskAuditorAgent(_ConstBrain("Low"), k=8, max_workers=1),
                       _fresh_ctx(scored_ctx))
    par = _dispute_ids(RiskAuditorAgent(_ConstBrain("Low"), k=8, max_workers=8),
                       _fresh_ctx(scored_ctx))
    assert seq, "fixture must produce at least one dispute or the test is vacuous"
    assert seq == par  # identical loan_ids, identical order


def test_parallel_actually_overlaps_calls(scored_ctx):
    """max_workers>1 overlaps audits; the default never does."""
    par_brain = _ConstBrain("Low")
    RiskAuditorAgent(par_brain, k=8, max_workers=8).run(_fresh_ctx(scored_ctx))
    assert par_brain.n_calls >= 2, "need multiple audited accounts to observe overlap"
    assert par_brain.max_concurrent > 1, "parallel path did not overlap LLM calls"

    seq_brain = _ConstBrain("Low")
    RiskAuditorAgent(seq_brain, k=8, max_workers=1).run(_fresh_ctx(scored_ctx))
    assert seq_brain.max_concurrent == 1, "default path must stay sequential"


def test_parallel_degrades_gracefully(scored_ctx):
    """Every audit erroring → no disputes, clean OK run, no crash."""
    ctx = _fresh_ctx(scored_ctx)
    res = RiskAuditorAgent(_RaisingBrain(), k=8, max_workers=8).run(ctx)
    assert res.status == Status.OK
    assert (ctx.data_handles.get("risk_disputes") or []) == []


def test_default_workers_is_sequential():
    """The constructor default must be 1 (opt-in parallelism)."""
    assert RiskAuditorAgent(MockLLM()).max_workers == 1
    # Clamp guards against a nonsense value disabling the audit.
    assert RiskAuditorAgent(MockLLM(), max_workers=0).max_workers == 1
    assert RiskAuditorAgent(MockLLM(), max_workers=-4).max_workers == 1
