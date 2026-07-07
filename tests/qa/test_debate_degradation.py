"""WA-020 QA — parse-degradation + independent-routing gap-fill for the debate.

``tests/test_wa016_debate.py`` already covers (28 tests): the JSON parsers, the
defend_score / arbiter.rule unit paths (uphold/concede/unparsable/brain-dead),
and the four terminal resolutions end-to-end with a UNIFORM script (all
disputes resolve the same way). ``tests/test_risk_auditor.py`` covers Round-1
parse-failure → no dispute, both at the agent unit and the orchestrator e2e.

The gaps this file closes (no duplication):

  * **R3 unparsable, end-to-end** — an uphold rebuttal followed by an
    *unparsable* Arbiter ruling. The unit test ``test_arbiter_rule_unparsable``
    proves the agent escalates; nothing wired that through the orchestrator to
    the human gate with a closed ``escalated_approved`` / ``escalated_rejected``
    terminal state. This is the "R3 unparsable → escalate" acceptance row.
  * **R3 brain-unreachable, end-to-end** — same shape, Arbiter brain raises
    mid-run. Proves the orchestrator's dispute loop never crashes on a brain
    outage at Round 3.
  * **Mixed-resolution run** — every existing e2e test scripts one resolution
    kind for all disputes. None proves the orchestrator routes each dispute
    *independently* in a single run (upheld + overridden + escalated
    interleaved). A shared-state bleed between disputes would pass every
    uniform test and only fail here.

These are advisory integration tests: scripted MockLLM, real
ingest→analytics→risk_model data, deterministic assertions on
``_resolution_counts`` and per-dispute ``resolution`` / ``resolved_by``.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import List

import pyarrow as pa
import pytest

from waspada.agents import (
    AgentContext, ApprovalGate, MockLLM, Status,
)
from waspada.agents.analytics import AnalyticsAgent
from waspada.agents.arbiter import ARBITER_CONFIDENCE_THRESHOLD
from waspada.agents.base import Rejected
from waspada.agents.ingest import IngestAgent
from waspada.agents.orchestrator import Orchestrator
from waspada.agents.risk_model import RiskModelAgent
from waspada.schema import RawLoans, schema_from_dataclass


# --------------------------------------------------------------------------- #
# Shared synthetic data — mirrors test_wa016_debate so the scored table is real
# (genuine ScoredAccounts + FeatureFrame) and the debate runs against it.
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


def _orch(raw: pa.Table, brain, *, gate=None, enable_arbiter: bool = True) -> Orchestrator:
    g = gate if gate is not None else ApprovalGate(auto_approve=True)
    orch = Orchestrator(brain, gate=g, as_of=dt.date(2024, 12, 1),
                        top_n=10, audit_k=4, enable_arbiter=enable_arbiter)
    _orig = orch._build_agents
    def _build():
        agents = _orig()
        for a in agents:
            if isinstance(a, IngestAgent):
                a.register_tool("fetch", _stub_fetch(raw))
        return agents
    orch._build_agents = _build  # type: ignore[method-assign]
    return orch


# Shared brain-script fragments (true call order: 4 challenges, then per
# dispute the rebuttal + arbiter ruling interleaved). See test_wa016_debate.
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
_CONCEDE_REBUTTAL = json.dumps({
    "verdict": "concede", "confidence": 0.6,
    "claim": "auditor right", "evidence": [],
})
_LOW_CONF_UPHOLD = json.dumps({
    "ruling": "uphold", "confidence": ARBITER_CONFIDENCE_THRESHOLD - 0.1,
    "rationale": "meh", "evidence": [],
})


# --------------------------------------------------------------------------- #
# R3 unparsable — end-to-end (the gap: unit covers the agent, not the wire).
# --------------------------------------------------------------------------- #
class TestRound3UnparsableEndToEnd:
    """Uphold rebuttal + UNPARSABLE Arbiter ruling → escalate → human gate.

    Covers the "R3 unparsable → escalate" acceptance row at the orchestrator
    level (the unit test ``test_arbiter_rule_unparsable_escalates`` proves the
    agent degrades; this proves the orchestrator closes the dispute through the
    gate with a terminal ``escalated_*`` state and a 3-round transcript).
    """

    def test_r3_unparsable_escalates_approved(self):
        raw = _raw_table(_raw_rows())
        # 4 challenges, then per dispute: uphold rebuttal + unparsable arbiter.
        brain = MockLLM(script=[_CHALLENGE] * 4 + [_UPHOLD_REBUTTAL, "I can't decide this"] * 4)
        orch = _orch(raw, brain)  # auto-approve gate
        orch.run(AgentContext(lane="collections", data_handles={}))

        counts = orch._resolution_counts
        assert counts["escalated_approved"] == 4
        assert counts["upheld"] == 0 and counts["overridden"] == 0
        assert counts["escalated_rejected"] == 0
        for d in orch._final_ctx.data_handles["risk_disputes"]:
            assert len(d.rounds) == 3  # R1 + R2 uphold + R3 unparsable-escalate
            assert d.rounds[2].claim.startswith("ESCALATE:")
            assert d.resolution == "escalated_approved"
            assert d.resolved_by == "human"

    def test_r3_unparsable_escalates_rejected(self):
        raw = _raw_table(_raw_rows())
        brain = MockLLM(script=[_CHALLENGE] * 4 + [_UPHOLD_REBUTTAL, "nope"] * 4)
        rejecting = ApprovalGate(decide=lambda action, rationale: Rejected(
            action=action, rationale=rationale, reason="human veto"))
        orch = _orch(raw, brain, gate=rejecting)
        orch.run(AgentContext(lane="collections", data_handles={}))
        counts = orch._resolution_counts
        assert counts["escalated_rejected"] == 4
        for d in orch._final_ctx.data_handles["risk_disputes"]:
            assert d.resolution == "escalated_rejected"
            assert d.resolved_by == "human"


# --------------------------------------------------------------------------- #
# R3 brain-unreachable — end-to-end (brain raises mid-debate, no crash).
# --------------------------------------------------------------------------- #
class _ArbiterBoomBrain(MockLLM):
    """MockLLM that raises only when the Arbiter calls it.

    The Arbiter's prompt opens with ``"You are the Arbiter"``; the Skeptic and
    Actuary prompts open with their own roles. Raising on that signature
    deterministically kills only the Round-3 call, leaving Rounds 1-2 intact.
    """

    def complete(self, prompt, *, history=None):
        if "You are the Arbiter" in (prompt or ""):
            raise RuntimeError("arbiter brain 503")
        return super().complete(prompt, history=history)


class TestRound3BrainUnreachableEndToEnd:
    """An Arbiter brain outage mid-run must degrade to the gate, never crash."""

    def test_r3_brain_outage_escalates_approved(self):
        raw = _raw_table(_raw_rows())
        brain = _ArbiterBoomBrain(script=[_CHALLENGE] * 4 + [_UPHOLD_REBUTTAL] * 4)
        orch = _orch(raw, brain)  # auto-approve
        res = orch.run(AgentContext(lane="collections", data_handles={}))

        # The orchestrator run completed (did not raise) and routed to gate.
        assert res.status == Status.DISPUTED
        counts = orch._resolution_counts
        assert counts["escalated_approved"] == 4
        for d in orch._final_ctx.data_handles["risk_disputes"]:
            assert len(d.rounds) == 3
            assert d.rounds[2].claim.startswith("ESCALATE:")
            assert d.resolution == "escalated_approved"


# --------------------------------------------------------------------------- #
# Mixed-resolution run — independent per-dispute routing (the integration gap).
# --------------------------------------------------------------------------- #
class TestMixedResolutionRun:
    """One orchestrator run producing a MIX of terminal resolutions.

    Every existing e2e test scripts a single resolution kind for all disputes.
    This scripts three different kinds interleaved and asserts the orchestrator
    routes each dispute independently — catching any shared-state bleed between
    disputes that a uniform test would miss.
    """

    def test_mixed_upheld_overridden_escalated_in_one_run(self):
        raw = _raw_table(_raw_rows())
        # True call order: 4 challenges, then per dispute (rebuttal [+ arbiter]):
        #   dispute 0: uphold rebuttal + arbiter uphold  → upheld
        #   dispute 1: concede rebuttal                  → overridden (no R3)
        #   dispute 2: uphold rebuttal + low-conf arbiter → escalated_approved
        #   dispute 3: uphold rebuttal + arbiter uphold  → upheld
        brain = MockLLM(script=(
            [_CHALLENGE] * 4
            + [_UPHOLD_REBUTTAL, _ARBITER_UPHOLD]   # dispute 0 → upheld
            + [_CONCEDE_REBUTTAL]                    # dispute 1 → overridden
            + [_UPHOLD_REBUTTAL, _LOW_CONF_UPHOLD]  # dispute 2 → escalated
            + [_UPHOLD_REBUTTAL, _ARBITER_UPHOLD]   # dispute 3 → upheld
        ))
        orch = _orch(raw, brain)  # auto-approve gate
        orch.run(AgentContext(lane="collections", data_handles={}))
        counts = orch._resolution_counts
        assert counts["upheld"] == 2
        assert counts["overridden"] == 1
        assert counts["escalated_approved"] == 1
        assert counts["escalated_rejected"] == 0

        disputes = orch._final_ctx.data_handles["risk_disputes"]
        # Per-dispute terminal state matches its script (independent routing).
        by_id = {d.loan_id: d for d in disputes}
        resolved = sorted(
            (d.resolution, d.resolved_by, len(d.rounds)) for d in disputes
        )
        # Exactly one overridden-by-risk_model (2 rounds, concession skips R3),
        # one escalated-by-human (3 rounds), two upheld-by-arbiter (3 rounds).
        assert resolved.count(("upheld", "arbiter", 3)) == 2
        assert resolved.count(("overridden", "risk_model", 2)) == 1
        assert resolved.count(("escalated_approved", "human", 3)) == 1
        assert len(disputes) == 4
