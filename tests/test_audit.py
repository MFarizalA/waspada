"""Audit-log stream tests (WA-023).

Pin the WA-023 contract:
  * ``build_records`` flattens the orchestrator's step log into audit records
    with the spec fields (run_id/agent/action/model/tokens/latency/resolution).
  * ``LocalAuditSink`` writes JSON lines to a file (the fail-safe floor).
  * ``SLSAuditSink`` degrades to the local file when the SDK / creds / SLS are
    unavailable — it NEVER raises (the non-negotiable fail-safe).
  * ``ship_run_audit`` swallows every error and reports 0 rather than breaking
    the caller.
  * ``get_audit_sink`` / ``sls_configured`` pick the sink from env.
"""
from __future__ import annotations

import json

import pytest

from waspada.agents.protocol import (
    AgentContext,
    AgentResult,
    Dispute,
    DisputeRound,
    Handoff,
    Status,
    Step,
)
from waspada.audit.sls import (
    LocalAuditSink,
    SLSAuditSink,
    build_records,
    get_audit_sink,
    ship_run_audit,
    sls_configured,
)


# --------------------------------------------------------------------------- #
# A minimal fake orchestrator carrying the attributes build_records reads.
# --------------------------------------------------------------------------- #
class _FakeAgent:
    def __init__(self, steps):
        self.steps = steps


class _FakeOrch:
    def __init__(self):
        self.steps = [
            Step(agent="orchestrator", action="run_start", status=Status.OK),
            Step(agent="orchestrator", action="run_done", status=Status.DISPUTED),
        ]
        self._pipeline_agents = [
            _FakeAgent([Step(agent="data_engineer", action="quality_gate", status=Status.OK)]),
            _FakeAgent([Step(agent="risk_auditor", action="audit", status=Status.OK)]),
        ]
        self.handoffs = [
            Handoff(frm="data_engineer", to="data_analyst",
                    result=AgentResult(status=Status.OK, agent="data_engineer"),
                    rationale="cleared"),
        ]
        dispute = Dispute(
            loan_id="LN0001", opened_by="risk_auditor",
            model_band="Q5", auditor_view="Medium",
            rounds=[
                DisputeRound(round_no=1, speaker="risk_auditor", model="qwen3.6-flash",
                             claim="payment ratio high", confidence=0.72,
                             evidence=["payment_ratio=0.9"]),
                DisputeRound(round_no=2, speaker="risk_model", model="qwen3.7-plus",
                             claim="model stands", confidence=0.84),
            ],
            resolution="upheld", resolved_by="arbiter", rationale="no mismatch shown",
        )
        self._final_ctx = AgentContext(
            lane="collections", data_handles={"risk_disputes": [dispute]},
        )


# --------------------------------------------------------------------------- #
# build_records
# --------------------------------------------------------------------------- #
def test_build_records_covers_steps_handoffs_and_disputes():
    recs = build_records(_FakeOrch(), run_id="run-abc")
    actions = [r["action"] for r in recs]
    # orchestration steps + per-agent steps + handoff + 2 rounds + resolution
    assert "run_start" in actions
    assert "quality_gate" in actions
    assert "audit" in actions
    assert "handoff" in actions
    assert "dispute_round_1" in actions and "dispute_round_2" in actions
    assert "dispute_resolved" in actions


def test_build_records_every_record_has_spec_fields():
    recs = build_records(_FakeOrch(), run_id="run-abc")
    assert recs
    for r in recs:
        # Every record carries the spec schema (values may be None, keys present).
        for field in ("run_id", "agent", "action", "status", "model", "tokens", "latency", "resolution"):
            assert field in r
        assert r["run_id"] == "run-abc"
        # tokens/latency are honestly null (not measured), never fabricated.
        assert r["tokens"] is None
        assert r["latency"] is None


def test_build_records_dispute_round_carries_model_and_resolution():
    recs = build_records(_FakeOrch(), run_id="run-abc")
    r1 = next(r for r in recs if r["action"] == "dispute_round_1")
    assert r1["model"] == "qwen3.6-flash"
    assert r1["resolution"] == "upheld"
    assert r1["loan_id"] == "LN0001"


def test_build_records_tolerates_empty_orchestrator():
    class _Empty:
        pass
    assert build_records(_Empty(), run_id="r") == []


# --------------------------------------------------------------------------- #
# LocalAuditSink — the fail-safe floor.
# --------------------------------------------------------------------------- #
def test_local_sink_writes_json_lines(tmp_path):
    path = tmp_path / "audit.jsonl"
    sink = LocalAuditSink("run-1", path=str(path))
    sink.emit_many([{"run_id": "run-1", "action": "a"}, {"run_id": "run-1", "action": "b"}])
    sink.close()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["action"] == "a"


def test_local_sink_swallows_io_errors(tmp_path):
    # Point the sink at a directory path — open()-for-write on a directory
    # raises, and the sink must swallow it (audit never breaks the pipeline).
    sink = LocalAuditSink("run-1", path=str(tmp_path))
    sink.emit_many([{"x": 1}])  # must not raise
    sink.close()


# --------------------------------------------------------------------------- #
# SLSAuditSink — fail-safe: degrades to local when the SDK/creds are missing.
# --------------------------------------------------------------------------- #
def test_sls_sink_falls_back_to_local_when_sdk_missing(tmp_path):
    sink = SLSAuditSink("run-2")
    # aliyun-log-python-sdk is not installed in CI → emit_many must not raise,
    # and the records must land in the local fallback file.
    sink._fallback = LocalAuditSink("run-2", path=str(tmp_path / "fallback.jsonl"))
    sink.emit_many([{"run_id": "run-2", "action": "audit"}])  # must not raise
    fallback = tmp_path / "fallback.jsonl"
    assert fallback.exists()
    assert json.loads(fallback.read_text(encoding="utf-8").strip())["action"] == "audit"


# --------------------------------------------------------------------------- #
# ship_run_audit — end-to-end fail-safe.
# --------------------------------------------------------------------------- #
def test_ship_run_audit_returns_count(tmp_path):
    sink = LocalAuditSink("run-3", path=str(tmp_path / "a.jsonl"))
    n = ship_run_audit(_FakeOrch(), run_id="run-3", sink=sink)
    assert n > 0
    assert (tmp_path / "a.jsonl").exists()


def test_ship_run_audit_swallows_a_raising_sink():
    class _Boom:
        def emit_many(self, records):
            raise RuntimeError("sink exploded")
        def close(self):
            raise RuntimeError("close exploded too")

    # Must not raise; reports 0 shipped.
    assert ship_run_audit(_FakeOrch(), run_id="r", sink=_Boom()) == 0


# --------------------------------------------------------------------------- #
# Sink selection from env.
# --------------------------------------------------------------------------- #
def test_sls_not_configured_by_default():
    # conftest strips SLS_* / OSS_* env, so we default to local.
    assert sls_configured() is False
    assert isinstance(get_audit_sink("r"), LocalAuditSink)


def test_sls_configured_when_all_env_present(monkeypatch):
    for k in ("SLS_ENDPOINT", "SLS_PROJECT", "SLS_LOGSTORE",
              "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET"):
        monkeypatch.setenv(k, "x")
    assert sls_configured() is True
    assert isinstance(get_audit_sink("r"), SLSAuditSink)
