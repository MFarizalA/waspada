"""WA-095 phase-2a — the parameter matrix threads through the run.

A policy is authoritative for the governance knobs: the orchestrator applies
audit_k / top_n / dispute_gap / arbiter_confidence from it, and the payload is
stamped with a policy_card (provenance). Pins the wiring (not a live debate):

  1. Orchestrator(policy=...) sets audit_k/top_n/dispute_gap/arbiter_confidence;
  2. the built RiskAuditorAgent carries the matrix's dispute_gap + K;
  3. the built ArbiterAgent carries the matrix's confidence threshold;
  4. absent a policy, the defaults are unchanged (regression anchor).
"""
from __future__ import annotations

from waspada.agents.arbiter import ARBITER_CONFIDENCE_THRESHOLD, ArbiterAgent
from waspada.agents.orchestrator import Orchestrator
from waspada.agents.risk_auditor import DISPUTE_GAP, RiskAuditorAgent
from waspada.policy import policy_from_dict


def _find(agents, cls):
    return next(a for a in agents if isinstance(a, cls))


def test_orchestrator_applies_policy_knobs():
    pol = policy_from_dict({
        "audit_k": 12, "top_n": 30, "dispute_gap": 3, "arbiter_confidence": 0.75,
    })
    orch = Orchestrator(policy=pol)
    assert orch.audit_k == 12
    assert orch.top_n == 30
    assert orch.dispute_gap == 3
    assert orch.arbiter_confidence == 0.75


def test_built_agents_carry_the_matrix():
    pol = policy_from_dict({"audit_k": 7, "dispute_gap": 4, "arbiter_confidence": 0.8})
    orch = Orchestrator(policy=pol)
    agents = orch._build_agents()

    auditor = _find(agents, RiskAuditorAgent)
    assert auditor.k == 7
    assert auditor.dispute_gap == 4

    # the arbiter is constructed in _build_agents and stashed on the orchestrator
    assert isinstance(orch._arbiter_agent, ArbiterAgent)
    assert orch._arbiter_agent.threshold == 0.8


def test_dispute_gap_changes_admissibility():
    tight = RiskAuditorAgent(dispute_gap=1)
    loose = RiskAuditorAgent(dispute_gap=3)
    # Very High (5) vs High (5 on the view ordinal) → gap 0 in the shared ordinal;
    # use a 2-apart case: model High(4) vs auditor Low(1) → gap 3.
    assert tight._should_dispute("High", "Low") is True     # 3 >= 1
    assert loose._should_dispute("High", "Low") is True     # 3 >= 3
    # model High(4) vs auditor Medium(3) → gap 1: disputes at gap 1, not at gap 3.
    assert tight._should_dispute("High", "Medium") is True   # 1 >= 1
    assert loose._should_dispute("High", "Medium") is False  # 1 < 3


def test_no_policy_keeps_defaults():
    orch = Orchestrator()
    assert orch.dispute_gap == int(DISPUTE_GAP)
    assert orch.arbiter_confidence == float(ARBITER_CONFIDENCE_THRESHOLD)
    assert orch.audit_k == 8 and orch.top_n == 50
    auditor = _find(orch._build_agents(), RiskAuditorAgent)
    assert auditor.dispute_gap == int(DISPUTE_GAP)
