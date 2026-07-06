"""Agent framework + ApprovalGate tests (WA-008 acceptance).

Covers the four acceptance checks from backlog/WA-008.md:

  1. Two stub agents hand off via ``AgentContext`` and produce a combined
     result; the run is logged (steps + handoff envelope).
  2. ``ApprovalGate`` blocks on rejection and proceeds on approval.
  3. Auto-approve is logged distinctly (``auto=True``) from a real decision.
  4. The LLM wrapper is mockable; the framework runs end-to-end with the mock
     brain and NO network (no SDK, no key, no socket).

The frozen data-contract names are cited where an artifact handle is used,
to prove the seam to :mod:`waspada.schema` is intact.
"""
from __future__ import annotations

import socket

import pytest

from waspada.agents import (
    Agent,
    AgentContext,
    AgentResult,
    ApprovalGate,
    Approved,
    Rejected,
    Status,
    Step,
    MockLLM,
    get_llm,
    handoff,
)
from waspada.agents.llm import GeminiLLM, LLM


# --------------------------------------------------------------------------- #
# Two stub agents — a producer and a consumer, to exercise the handoff.
# --------------------------------------------------------------------------- #
class ProducerAgent(Agent):
    name = "producer"
    role = "produce an artifact handle"

    def run(self, context: AgentContext) -> AgentResult:
        # Use the mock brain (offline). Exercises llm.complete end-to-end.
        thought = self.llm.complete("produce something")
        ref = context.data_handles.get("out", "mem://producer/out")
        self.step("produce", notes=f"llm said: {thought[:32]}")
        return AgentResult(status=Status.OK, artifact_ref=ref,
                           notes="produced artifact", agent=self.name)


class ConsumerAgent(Agent):
    name = "consumer"
    role = "consume the predecessor's artifact"

    def run(self, context: AgentContext) -> AgentResult:
        # Read the predecessor's artifact_ref from prior_results.
        if not context.prior_results:
            self.step("consume", status=Status.ERROR, notes="no predecessor")
            return AgentResult(status=Status.ERROR, agent=self.name,
                               notes="nothing to consume")
        prev = context.prior_results[-1]
        self.step("consume", notes=f"read {prev.artifact_ref}")
        return AgentResult(status=Status.OK,
                           artifact_ref=f"{prev.artifact_ref}#consumed",
                           notes="combined result", agent=self.name)


# --------------------------------------------------------------------------- #
# 1. Two-agent handoff via AgentContext; combined result + logged steps.
# --------------------------------------------------------------------------- #
def test_two_agent_handoff_produces_combined_result():
    producer = ProducerAgent()
    consumer = ConsumerAgent()

    ctx0 = AgentContext(lane="collections", data_handles={"out": "mem://run1/features"})
    r1 = producer.run(ctx0)
    assert r1.ok and r1.artifact_ref == "mem://run1/features"

    # Thread the producer's result into the consumer's context (non-mutating).
    ctx1 = ctx0.with_result(r1)
    r2 = consumer.run(ctx1)

    assert r2.ok
    assert r2.artifact_ref == "mem://run1/features#consumed"
    # Combined result is the consumer's output referencing the producer's.
    assert "consumed" in r2.artifact_ref
    # Steps logged on each agent.
    assert any(s.agent == "producer" for s in producer.steps)
    assert any(s.agent == "consumer" for s in consumer.steps)


def test_handoff_envelope_records_from_to():
    producer = ProducerAgent()
    consumer = ConsumerAgent()
    ctx = AgentContext(lane="collections")
    r = producer.run(ctx)
    env = handoff(producer, consumer, r, rationale="producer → consumer")
    assert env.frm == "producer"
    assert env.to == "consumer"
    assert env.result is r


def test_context_with_result_is_non_mutating():
    ctx = AgentContext(lane="collections")
    r = AgentResult(status=Status.OK, artifact_ref="x", agent="a")
    ctx2 = ctx.with_result(r)
    assert len(ctx.prior_results) == 0     # original untouched
    assert len(ctx2.prior_results) == 1


# --------------------------------------------------------------------------- #
# 2. ApprovalGate — approve proceeds, reject blocks.
# --------------------------------------------------------------------------- #
def test_approval_gate_approves_via_decide_callback():
    gate = ApprovalGate(decide=lambda action, rationale: Approved(action=action, rationale=rationale))
    decision = gate.request("apply_collections_strategy", "scored work-list ready")
    assert isinstance(decision, Approved)
    assert decision.auto is False
    # The decision was logged.
    assert any(s.action == "apply_collections_strategy" and s.status == Status.OK for s in gate.steps)


def test_approval_gate_rejects_and_blocks():
    """A rejection returns Rejected and the caller treats it as BLOCKED."""
    def human_says_no(action, rationale):
        return Rejected(action=action, rationale=rationale, reason="not now")

    gate = ApprovalGate(decide=human_says_no)
    decision = gate.request("deploy_model", "v2 ready")
    assert isinstance(decision, Rejected)
    assert decision.reason == "not now"
    # The gate logged a BLOCKED step for this action.
    blocked = [s for s in gate.steps if s.action == "deploy_model"]
    assert blocked and blocked[0].status == Status.BLOCKED


def test_approval_gate_no_channel_fails_safe_blocked():
    """Interactive gate with no decide channel must block, not guess."""
    gate = ApprovalGate(auto_approve=False)  # no decide wired
    decision = gate.request("risky_action", "test")
    assert isinstance(decision, Rejected)
    assert decision.auto is True  # the fail-safe rejection is itself automatic


# --------------------------------------------------------------------------- #
# 3. Auto-approve logged distinctly from a real decision.
# --------------------------------------------------------------------------- #
def test_auto_approve_is_logged_distinctly():
    """WASPADA_AUTO_APPROVE=1 short-circuits to approve with auto=True."""
    gate = ApprovalGate(auto_approve=True)
    decision = gate.request("apply_strategy", "smoke run")
    assert isinstance(decision, Approved)
    assert decision.auto is True
    step = gate.steps[-1]
    assert step.auto is True and step.status == Status.OK


def test_auto_approve_env_override(monkeypatch):
    monkeypatch.setenv("WASPADA_AUTO_APPROVE", "1")
    gate = ApprovalGate()  # no explicit flag → reads env
    assert gate.auto_approve is True
    decision = gate.request("x", "y")
    assert isinstance(decision, Approved) and decision.auto is True


def test_manual_and_auto_log_side_by_side():
    """Same gate logs a manual decision and an auto decision distinctly."""
    # Start interactive, swap to auto mid-run.
    gate = ApprovalGate(decide=lambda a, r: Approved(action=a, rationale=r))
    gate.request("manual_action", "human approved")
    gate.auto_approve = True
    gate.request("auto_action", "smoke run")
    statuses = {(s.action, s.auto) for s in gate.steps}
    assert ("manual_action", False) in statuses
    assert ("auto_action", True) in statuses


# --------------------------------------------------------------------------- #
# 4. Mockable LLM — framework runs end-to-end offline, no network.
# --------------------------------------------------------------------------- #
def test_mock_llm_default_reply():
    llm = MockLLM()
    assert llm.complete("hello") == "mock-llm-ok"
    assert llm.complete("again") == "mock-llm-ok"
    assert len(llm.calls) == 2


def test_mock_llm_scripted_replies():
    llm = MockLLM(script=["first", "second"])
    assert llm.complete("a") == "first"
    assert llm.complete("b") == "second"
    assert llm.complete("c") == "second"  # exhausts to last


def test_get_llm_defaults_to_mock(monkeypatch):
    monkeypatch.delenv("WASPADA_LLM_PROVIDER", raising=False)
    assert isinstance(get_llm(), MockLLM)


def test_get_llm_invalid_provider_raises(monkeypatch):
    monkeypatch.setenv("WASPADA_LLM_PROVIDER", "bogus")
    with pytest.raises(ValueError):
        get_llm()


def test_gemini_llm_constructor_without_key_raises(monkeypatch):
    """Offline: no key → clear RuntimeError, never an opaque ImportError."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        GeminiLLM()


def test_framework_runs_offline_no_network(monkeypatch):
    """End-to-end: two agents + mock brain with the socket() family blocked.

    Proves the framework needs no network. We patch socket.socket to assert
    it's never opened during a full producer→consumer run on the mock brain.
    """
    monkeypatch.delenv("WASPADA_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    calls = {"n": 0}

    class _NoNetwork(Exception):
        pass

    def _blocked(*a, **k):
        calls["n"] += 1
        raise _NoNetwork("network is blocked in this test")

    monkeypatch.setattr(socket, "socket", _blocked)

    producer = ProducerAgent(llm=MockLLM(reply="offline-thought"))
    consumer = ConsumerAgent(llm=MockLLM())
    ctx = AgentContext(lane="collections", data_handles={"out": "mem://offline/out"})
    r1 = producer.run(ctx)
    r2 = consumer.run(ctx.with_result(r1))

    assert r1.ok and r2.ok
    assert calls["n"] == 0  # no socket opened


def test_agent_tools_registry_roundtrip():
    """An agent can register and call a tool through its registry."""
    p = ProducerAgent()
    p.register_tool("echo", lambda x: f"echo:{x}")
    assert p.tools["echo"]("hi") == "echo:hi"


def test_step_recorded_with_timestamp():
    p = ProducerAgent()
    p.run(AgentContext(lane="collections"))
    assert p.steps
    s: Step = p.steps[0]
    assert s.at.endswith("Z")  # ISO-8601 UTC marker
    assert s.agent == "producer"
