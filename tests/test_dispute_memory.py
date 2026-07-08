"""WA-026 acceptance — cross-run dispute memory.

Covers the three mandated paths with a scripted MockLLM (no network):

  * **miss** — a fresh book on a cold memory: every dispute debates fully, none
    short-circuit; the memory is populated by the run.
  * **hit / short-circuit** — a prior HUMAN ruling on the same loan_id reuses
    the prior resolution and skips the debate (0 LLM calls for that dispute).
  * **precedent (non-human)** — a prior arbiter/model ruling is injected as
    context but the debate still runs in full (the memory INFORMS, never
    silences).

Plus the headline efficiency axis: run the same book twice → run 2 spends
measurably fewer LLM calls (the second headline number for the benchmark),
because the human-settled disputes short-circuit.

The data path is a real ingest→analytics→risk_model run (reusing the WA-016
fixture shape) so the debate runs against genuine ScoredAccounts +
FeatureFrame tables.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import List

import pyarrow as pa
import pytest

from waspada.agents import (
    AgentContext, ApprovalGate, Dispute, DisputeMemory, DisputeRound,
    InMemoryMemory, LocalFileMemory, MockLLM, Status,
)
from waspada.agents.analytics import AnalyticsAgent
from waspada.agents.base import Approved, Rejected
from waspada.agents.ingest import IngestAgent
from waspada.agents.data_engineer import DataEngineerAgent
from waspada.agents.orchestrator import Orchestrator
from waspada.agents.risk_model import RiskModelAgent
from waspada.schema import RawLoans, schema_from_dataclass


# --------------------------------------------------------------------------- #
# Shared synthetic data (mirrors test_wa016_debate so the scored table is real).
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


def _open_dispute(loan_id: str = "L1") -> Dispute:
    """A Round-1-only open dispute (the state WA-026 receives from WA-014)."""
    return Dispute(
        loan_id=loan_id, opened_by="risk_auditor",
        model_band="Q5", auditor_view="Low",
        rounds=[DisputeRound(
            round_no=1, speaker="risk_auditor", model="qwen3.6-flash",
            claim="near-settled balance contradicts the band",
            confidence=0.8, evidence=["payment_ratio=0.95"],
        )],
    )


# The shared brain script fragments (parity with test_wa016_debate).
_CHALLENGE = json.dumps({
    "auditor_view": "Low", "confidence": 0.8,
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
_LOW_CONF_UPHOLD = json.dumps({
    "ruling": "uphold", "confidence": 0.5,  # below 0.6 threshold → escalate
    "rationale": "meh", "evidence": [],
})


def _orch_with_brain(raw: pa.Table, brain: MockLLM, *, gate=None,
                     enable_arbiter: bool = True,
                     memory: DisputeMemory = None) -> Orchestrator:
    """Build an orchestrator with a stubbed fetch + a given dispute memory."""
    g = gate if gate is not None else ApprovalGate(auto_approve=True)
    orch = Orchestrator(
        brain, gate=g, as_of=dt.date(2024, 12, 1),
        top_n=10, audit_k=4, enable_arbiter=enable_arbiter,
        memory=memory,
    )
    _orig = orch._build_agents
    def _build():
        agents = _orig()
        for a in agents:
            if isinstance(a, DataEngineerAgent):
                a.register_tool("fetch", _stub_fetch(raw))
                a.llm = MockLLM()  # fresh brain — DE loop must not eat the debate script
        return agents
    orch._build_agents = _build  # type: ignore[method-assign]
    return orch


def _debate_brain(n_disputes: int = 4, *, escalate: bool = False) -> MockLLM:
    """A shared brain that drives a full debate for ``n_disputes`` accounts.

    True call order: n challenges, then per dispute the rebuttal + arbiter
    ruling interleaved. ``escalate=True`` makes the arbiter low-confidence so
    every dispute lands on the human gate (→ escalated_approved).
    """
    arbiter_reply = _LOW_CONF_UPHOLD if escalate else _ARBITER_UPHOLD
    return MockLLM(script=[_CHALLENGE] * n_disputes +
                   [_UPHOLD_REBUTTAL, arbiter_reply] * n_disputes)


# =========================================================================== #
# Unit: DisputeMemory facade (no orchestrator — pure memory semantics)
# =========================================================================== #
class TestDisputeMemoryFacade:
    def test_cold_memory_misses_everything(self):
        """A fresh memory has no precedent: every lookup is a miss."""
        mem = DisputeMemory(InMemoryMemory())
        assert mem.size == 0
        assert mem.lookup("any") is None
        assert mem.short_circuit(_open_dispute("X1")) is None
        assert mem.precedent(_open_dispute("X1")) is None
        assert mem.misses == 1
        assert mem.short_circuited == 0

    def test_human_ruling_short_circuits(self):
        """A prior HUMAN ruling is reused; short_circuit returns the resolution."""
        mem = DisputeMemory(InMemoryMemory(seed={
            "L1": {"resolution": "escalated_rejected", "resolved_by": "human",
                   "rationale": "analyst rejected", "model_band": "Q5",
                   "auditor_view": "Low"},
        }))
        recalled = mem.short_circuit(_open_dispute("L1"))
        assert recalled is not None
        assert recalled["resolution"] == "escalated_rejected"
        assert recalled["resolved_by"] == "human"
        assert recalled["from_memory"] is True
        assert mem.short_circuited == 1

    def test_arbiter_precedent_does_not_short_circuit(self):
        """A prior ARBITER ruling informs but does NOT silence: short_circuit
        returns None (debate runs), precedent returns the context."""
        mem = DisputeMemory(InMemoryMemory(seed={
            "L1": {"resolution": "upheld", "resolved_by": "arbiter",
                   "rationale": "model stronger", "model_band": "Q5",
                   "auditor_view": "Low"},
        }))
        assert mem.short_circuit(_open_dispute("L1")) is None
        assert mem.short_circuited == 0
        assert mem.precedent_hits == 1
        prior = mem.precedent(_open_dispute("L1"))
        assert prior is not None
        assert prior["resolved_by"] == "arbiter"

    def test_record_resolved_then_recall(self):
        """Record a freshly-resolved dispute → persist → a second facade
        (simulating the next run) recalls it. The facade caches locally until
        persist() flushes to the backend, so a second facade only sees records
        that were actually persisted."""
        mem = DisputeMemory(InMemoryMemory())
        d = _open_dispute("L9")
        d.resolution = "overridden"; d.resolved_by = "risk_model"
        d.rationale = "conceded under evidence"
        mem.record_resolved(d)
        assert mem.size == 1
        mem.persist()  # flush to the backend so the next run can read it
        # A second facade over the SAME backend (simulating the next run) recalls.
        mem2 = DisputeMemory(mem.backend)
        recalled = mem2.short_circuit(_open_dispute("L9"))
        # risk_model precedent → informs, does not short-circuit.
        assert recalled is None
        prior = mem2.precedent(_open_dispute("L9"))
        assert prior["resolved_by"] == "risk_model"

    def test_record_many_skips_unresolved(self):
        """Unresolved disputes (empty resolution) are never recorded."""
        mem = DisputeMemory(InMemoryMemory())
        open_d = _open_dispute("L1")   # no resolution set
        closed_d = _open_dispute("L2"); closed_d.resolution = "upheld"
        closed_d.resolved_by = "arbiter"
        n = mem.record_many([open_d, closed_d])
        assert n == 1
        assert mem.lookup("L1") is None
        assert mem.lookup("L2") is not None

    def test_reset_counters_zeroes_bookkeeping(self):
        mem = DisputeMemory(InMemoryMemory(seed={
            "L1": {"resolution": "upheld", "resolved_by": "human"},
        }))
        mem.short_circuit(_open_dispute("L1"))
        assert mem.short_circuited == 1
        mem.reset_counters()
        assert mem.short_circuited == 0

    def test_empty_loan_id_is_noop(self):
        mem = DisputeMemory(InMemoryMemory(seed={"": {"resolution": "x"}}))
        assert mem.lookup("") is None
        assert mem.short_circuit(_open_dispute("")) is None


# =========================================================================== #
# Unit: backends — InMemory + LocalFile (load/save/atomic/corrupt-tolerant)
# =========================================================================== #
class TestLocalFileBackend:
    def test_missing_file_reads_empty(self, tmp_path: Path):
        mem = LocalFileMemory(str(tmp_path / "absent.json"))
        assert mem.load_memory() == {}

    def test_roundtrip_persists_and_loads(self, tmp_path: Path):
        path = tmp_path / "mem.json"
        be = LocalFileMemory(str(path))
        be.save_memory({"L1": {"resolution": "upheld", "resolved_by": "arbiter"}})
        assert path.exists()
        loaded = LocalFileMemory(str(path)).load_memory()
        assert loaded["L1"]["resolution"] == "upheld"

    def test_versioned_wrapper_is_tolerated(self, tmp_path: Path):
        """The loader accepts both a bare mapping and a versioned wrapper."""
        path = tmp_path / "mem.json"
        path.write_text(json.dumps({
            "version": "1", "disputes": {"L2": {"resolution": "overridden",
                                                "resolved_by": "risk_model"}},
        }), encoding="utf-8")
        loaded = LocalFileMemory(str(path)).load_memory()
        assert loaded["L2"]["resolved_by"] == "risk_model"

    def test_corrupt_file_degrades_to_empty(self, tmp_path: Path):
        """A corrupt file never crashes a run — degrade to cold start."""
        path = tmp_path / "mem.json"
        path.write_text("{not valid json", encoding="utf-8")
        assert LocalFileMemory(str(path)).load_memory() == {}

    def test_empty_file_reads_empty(self, tmp_path: Path):
        path = tmp_path / "mem.json"
        path.write_text("", encoding="utf-8")
        assert LocalFileMemory(str(path)).load_memory() == {}

    def test_atomic_write_no_tmp_left_behind(self, tmp_path: Path):
        """save_memory writes via a temp rename — no .tmp fragment survives."""
        path = tmp_path / "mem.json"
        LocalFileMemory(str(path)).save_memory({"L1": {"resolution": "upheld"}})
        assert path.exists()
        assert not path.with_suffix(path.suffix + ".tmp").exists()

    def test_save_creates_parent_dirs(self, tmp_path: Path):
        """A nested path creates the parent dir lazily."""
        path = tmp_path / "nested" / "deep" / "mem.json"
        LocalFileMemory(str(path)).save_memory({"L1": {"resolution": "upheld"}})
        assert path.exists()


# =========================================================================== #
# Integration: orchestrator end-to-end memory paths
# =========================================================================== #
class TestOrchestratorMemoryPaths:
    def test_miss_first_run_populates_memory(self):
        """Cold memory → all 4 disputes debate fully; the memory is populated
        with 4 resolved records. This is the run-1 baseline."""
        raw = _raw_table(_raw_rows())
        mem = DisputeMemory(InMemoryMemory())
        brain = _debate_brain(n_disputes=4)
        orch = _orch_with_brain(raw, brain, memory=mem)
        orch.run(AgentContext(lane="collections", data_handles={}))

        assert mem.misses == 4
        assert mem.short_circuited == 0
        # All 4 resolved + recorded.
        assert mem.size == 4
        counts = orch._resolution_counts
        assert counts["upheld"] == 4

    def test_human_ruling_short_circuits_debate(self):
        """A prior human ruling on a disputed loan → that dispute skips the
        debate entirely (0 LLM calls for it) and reuses the prior resolution."""
        raw = _raw_table(_raw_rows())
        # Seed the memory with a human ruling on the first disputed loan_id.
        # The auditor opens disputes on the top-K riskiest accounts (Q5); the
        # exact loan_ids depend on the synthetic data, so we grab them by
        # running once cold and reading the disputes back.
        mem = DisputeMemory(InMemoryMemory())
        brain = _debate_brain(n_disputes=4)
        orch = _orch_with_brain(raw, brain, memory=mem)
        orch.run(AgentContext(lane="collections", data_handles={}))
        first_loan = orch._final_ctx.data_handles["risk_disputes"][0].loan_id
        human_resolution = orch._final_ctx.data_handles["risk_disputes"][0].resolution

        # Now rewrite the memory: that one loan was settled by a HUMAN (force
        # resolved_by=human so it short-circuits next run).
        seed = {first_loan: {
            "resolution": "escalated_approved", "resolved_by": "human",
            "rationale": "prior human sign-off", "model_band": "Q5",
            "auditor_view": "Low",
        }}
        mem2 = DisputeMemory(InMemoryMemory(seed=seed))
        brain2 = _debate_brain(n_disputes=4)
        orch2 = _orch_with_brain(raw, brain2, memory=mem2)
        orch2.run(AgentContext(lane="collections", data_handles={}))

        # One dispute short-circuited; the other three debated.
        assert mem2.short_circuited == 1
        # The short-circuited dispute carries the recalled resolution.
        d0 = next(d for d in orch2._final_ctx.data_handles["risk_disputes"]
                  if d.loan_id == first_loan)
        assert d0.resolution == "escalated_approved"
        assert d0.resolved_by == "human"
        assert any("RECALLED" in r.claim for r in d0.rounds)

    def test_arbiter_precedent_does_not_skip_debate(self):
        """A prior ARBITER ruling is injected as context but the debate STILL
        runs — the memory INFORMS, never silences (the demo keeps showing
        disputes)."""
        raw = _raw_table(_raw_rows())
        # First run to discover a disputed loan_id + its arbiter resolution.
        mem = DisputeMemory(InMemoryMemory())
        orch = _orch_with_brain(raw, _debate_brain(4), memory=mem)
        orch.run(AgentContext(lane="collections", data_handles={}))
        first_loan = orch._final_ctx.data_handles["risk_disputes"][0].loan_id

        # Seed the memory with a prior ARBITER ruling on that loan.
        seed = {first_loan: {
            "resolution": "upheld", "resolved_by": "arbiter",
            "rationale": "prior arbiter uphold", "model_band": "Q5",
            "auditor_view": "Low",
        }}
        mem2 = DisputeMemory(InMemoryMemory(seed=seed))
        brain2 = _debate_brain(n_disputes=4)
        orch2 = _orch_with_brain(raw, brain2, memory=mem2)
        orch2.run(AgentContext(lane="collections", data_handles={}))

        # NO short-circuit (arbiter precedent only informs).
        assert mem2.short_circuited == 0
        # The precedent was injected onto the dispute (a PRECEDENT round).
        d0 = next(d for d in orch2._final_ctx.data_handles["risk_disputes"]
                  if d.loan_id == first_loan)
        assert any("PRECEDENT" in r.claim for r in d0.rounds)
        # The debate still ran in full (3 rounds: precedent-note + R1 + R2/R3
        # depending on counting — but at least the substantive debate rounds).
        assert len(d0.rounds) >= 3

    def test_persist_called_after_run(self):
        """run() flushes the memory at the end; the audit log records it."""
        raw = _raw_table(_raw_rows())
        mem = DisputeMemory(InMemoryMemory())
        orch = _orch_with_brain(raw, _debate_brain(4), memory=mem)
        orch.run(AgentContext(lane="collections", data_handles={}))
        # The persist is recorded as an audit step.
        assert any(s.action == "memory_persisted" for s in orch.steps)


# =========================================================================== #
# Headline efficiency axis: run the same book twice → run 2 spends fewer calls
# =========================================================================== #
class TestCrossRunEfficiency:
    def test_second_run_fewer_llm_calls_on_escalated_book(self):
        """Run the same book twice. Run 1 escalates every dispute to the human
        gate (resolved_by=human). Run 2 should short-circuit those and spend
        measurably fewer LLM calls — the WA-026 headline efficiency number."""
        raw = _raw_table(_raw_rows())
        backend = InMemoryMemory()

        # --- Run 1: cold memory, every dispute escalates to the human gate. ---
        mem1 = DisputeMemory(backend)
        brain1 = _debate_brain(n_disputes=4, escalate=True)
        orch1 = _orch_with_brain(raw, brain1, memory=mem1)
        orch1.run(AgentContext(lane="collections", data_handles={}))
        calls1 = len(brain1.calls)
        counts1 = orch1._resolution_counts
        assert counts1["escalated_approved"] == 4
        # Run 1 recorded 4 human rulings.
        assert mem1.size == 4
        assert all(v["resolved_by"] == "human" for v in mem1.load().values())

        # --- Run 2: SAME book, SAME backend (memory now warm). ---
        mem2 = DisputeMemory(backend)
        brain2 = _debate_brain(n_disputes=4, escalate=True)
        orch2 = _orch_with_brain(raw, brain2, memory=mem2)
        orch2.run(AgentContext(lane="collections", data_handles={}))
        calls2 = len(brain2.calls)

        # All 4 short-circuited (the human rulings are recalled).
        assert mem2.short_circuited == 4
        # The headline number: run 2 spends strictly fewer LLM calls than run 1.
        assert calls2 < calls1
        # The efficiency delta is large: each short-circuited dispute saved
        # 2 debate calls (rebuttal + arbiter ruling). With 4 disputes that's
        # ~8 calls saved.
        assert (calls1 - calls2) >= 8

    def test_second_run_decision_consistency(self):
        """Run 2's short-circuited resolutions MATCH run 1's human rulings —
        decision consistency (the honest framing), not a new verdict."""
        raw = _raw_table(_raw_rows())
        backend = InMemoryMemory()

        mem1 = DisputeMemory(backend)
        orch1 = _orch_with_brain(raw, _debate_brain(4, escalate=True), memory=mem1)
        orch1.run(AgentContext(lane="collections", data_handles={}))
        run1 = {d.loan_id: (d.resolution, d.resolved_by)
                for d in orch1._final_ctx.data_handles["risk_disputes"]}

        mem2 = DisputeMemory(backend)
        orch2 = _orch_with_brain(raw, _debate_brain(4, escalate=True), memory=mem2)
        orch2.run(AgentContext(lane="collections", data_handles={}))
        run2 = {d.loan_id: (d.resolution, d.resolved_by)
                for d in orch2._final_ctx.data_handles["risk_disputes"]}

        # Every account resolved identically across runs (consistency).
        assert run1 == run2

    def test_localfile_backend_survives_across_instances(self, tmp_path: Path):
        """The LocalFile backend persists across two orchestrator instances
        (the demo's actual cross-process path). Run 1 writes; a fresh
        orchestrator over the same file path recalls on run 2."""
        raw = _raw_table(_raw_rows())
        path = str(tmp_path / "dispute_memory.json")

        mem1 = DisputeMemory(LocalFileMemory(path))
        brain1 = _debate_brain(4, escalate=True)
        orch1 = _orch_with_brain(raw, brain1, memory=mem1)
        orch1.run(AgentContext(lane="collections", data_handles={}))
        assert Path(path).exists()

        # A totally fresh orchestrator + facade over the same file.
        mem2 = DisputeMemory(LocalFileMemory(path))
        brain2 = _debate_brain(n_disputes=4, escalate=True)
        orch2 = _orch_with_brain(raw, brain2, memory=mem2)
        orch2.run(AgentContext(lane="collections", data_handles={}))

        assert mem2.short_circuited == 4
        # Run 2 spent strictly fewer LLM calls than run 1 (the headline number).
        assert len(brain2.calls) < len(brain1.calls)
