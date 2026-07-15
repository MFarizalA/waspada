"""SSE stream endpoint tests (WA-022 acceptance).

These tests pin the backend contract with
``dashboard/src/lib/useLiveDebateStream.ts``:

  * ``GET /api/run/stream`` requires auth (Bearer header OR ``?token=`` query).
  * Response ``Content-Type`` is ``text/event-stream``.
  * Events are ``data: <json>\n\n`` frames.
  * A debate produces ``round`` events, one ``resolution`` per dispute, and a
    terminal ``done``.
  * ``brain=mock`` with the default canned brain produces no disputes → stream
    is just ``done``.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from waspada.agents import MockLLM
from waspada.agents.data_analyst import DataAnalystAgent
from waspada.agents.data_engineer import DataEngineerAgent
from waspada.agents.orchestrator import Orchestrator

# Import the FastAPI app lazily so the test module imports cleanly even when
# FastAPI is not installed.
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import api.main as main_mod  # noqa: E402
from api import auth as auth_mod  # noqa: E402


@pytest.fixture
def client():
    """A TestClient with a freshly-seeded demo analyst."""
    auth_mod.reset_store()
    auth_mod.seed_default_user()
    with TestClient(main_mod.app) as c:
        yield c


@pytest.fixture
def token(client) -> str:
    """JWT for the seeded demo analyst."""
    r = client.post(
        "/api/auth/login",
        json={"email": auth_mod.DEFAULT_ANALYST_EMAIL,
              "password": auth_mod.DEFAULT_ANALYST_PASSWORD},
    )
    assert r.status_code == 200
    return r.json()["token"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _parse_sse(body: str) -> List[Dict[str, Any]]:
    """Parse the raw SSE body into a list of JSON events."""
    events: List[Dict[str, Any]] = []
    for line in body.split("\n\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def _isolated_de_brain(orch: Orchestrator) -> Orchestrator:
    """Give the Tier-2 data agents (Data Engineer + Data Analyst) fresh canned
    brains so the scripted orchestrator brain is reserved for the debate agents.

    Both data agents run function-calling loops on ``self.llm``; without this,
    they would consume the scripted debate script before the Risk Auditor speaks.
    Mirrors the isolation WA-030 applied to the other debate tests.
    """
    orig = orch._build_agents
    def _build():
        agents = orig()
        for a in agents:
            if isinstance(a, (DataEngineerAgent, DataAnalystAgent)):
                a.llm = MockLLM()
        return agents
    orch._build_agents = _build  # type: ignore[method-assign]
    return orch


def _debate_brain_script(n_disputes: int = 4) -> MockLLM:
    """Return a scripted MockLLM that opens and resolves ``n_disputes``.

    True call order (see test_wa016_debate): n challenges, then per dispute a
    rebuttal + arbiter ruling interleaved.
    """
    challenge = json.dumps({
        "auditor_view": "Low",
        "confidence": 0.72,
        "claim": "payment ratio is high relative to the band",
        "evidence": ["payment_ratio=0.95"],
    })
    rebuttal = json.dumps({
        "verdict": "uphold",
        "confidence": 0.84,
        "claim": "model stands; dti and rate support Very High",
        "evidence": ["dti=35"],
    })
    ruling = json.dumps({
        "ruling": "uphold",
        "confidence": 0.9,
        "rationale": "auditor did not prove mismatch",
        "evidence": [],
    })
    return MockLLM(script=[challenge] * n_disputes + [rebuttal, ruling] * n_disputes)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def test_stream_without_token_is_401(client):
    r = client.get("/api/run/stream")
    assert r.status_code == 401


def test_stream_with_garbage_token_is_401(client):
    r = client.get("/api/run/stream?token=not-a-jwt")
    assert r.status_code == 401


def test_stream_with_valid_token_is_200_and_event_stream(client, token):
    r = client.get(f"/api/run/stream?token={token}")
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")


def test_stream_accepts_bearer_header(client, token):
    r = client.get("/api/run/stream", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")


# --------------------------------------------------------------------------- #
# Mock no-dispute path
# --------------------------------------------------------------------------- #
def test_stream_mock_no_dispute_ends_with_done(client, token):
    r = client.get(f"/api/run/stream?token={token}&brain=mock")
    assert r.status_code == 200
    events = _parse_sse(r.text)
    assert events
    assert events[-1] == {"type": "done"}
    # No disputes opened with the default canned mock brain.
    assert all(e.get("type") in ("round", "resolution", "done") for e in events)


# --------------------------------------------------------------------------- #
# Scripted debate path
# --------------------------------------------------------------------------- #
def test_stream_scripted_debate_emits_rounds_resolution_done(client, token, monkeypatch):
    """A scripted brain opens one dispute and resolves it → round/resolution/done."""
    n = 1
    scripted = _debate_brain_script(n_disputes=n)
    orig_build = main_mod._build_demo_orchestrator

    def _build_scripted(brain: str = "mock", **kwargs):
        # Force the orchestrator onto the scripted brain regardless of the
        # query param; isolate the Data Engineer so it doesn't eat the script.
        # Cap audit_k so the scripted brain is consumed predictably.
        orch = orig_build("mock", **kwargs)
        orch.llm = scripted
        orch.audit_k = n
        return _isolated_de_brain(orch)

    monkeypatch.setattr(main_mod, "_build_demo_orchestrator", _build_scripted)

    r = client.get(f"/api/run/stream?token={token}")
    assert r.status_code == 200
    events = _parse_sse(r.text)

    types = [e.get("type") for e in events]
    assert types.count("round") >= 1
    assert types.count("resolution") == 1
    assert types[-1] == "done"

    # Verify the resolution shape matches the frontend contract.
    resolution = next(e for e in events if e.get("type") == "resolution")
    assert resolution["loan_id"]
    assert resolution["resolution"] == "upheld"
    assert resolution["resolved_by"] == "arbiter"
    assert isinstance(resolution["rationale"], str)


def test_stream_scripted_multiple_disputes_one_resolution_each(client, token, monkeypatch):
    """Several disputes → each gets exactly one resolution, then done."""
    n = 4
    scripted = _debate_brain_script(n_disputes=n)
    orig_build = main_mod._build_demo_orchestrator

    def _build_scripted(brain: str = "mock", **kwargs):
        orch = orig_build("mock", **kwargs)
        orch.llm = scripted
        orch.audit_k = n
        return _isolated_de_brain(orch)

    monkeypatch.setattr(main_mod, "_build_demo_orchestrator", _build_scripted)

    r = client.get(f"/api/run/stream?token={token}")
    assert r.status_code == 200
    events = _parse_sse(r.text)

    types = [e.get("type") for e in events]
    # ONE resolution per dispute — the invariant. (Pre WA-049 this asserted
    # exactly ``n``, which held only because audit_k=n and every audited account
    # was top-K/"Very High" and so always disputed. The slice is now stratified,
    # so some audited accounts legitimately agree with the model and open no
    # dispute. What must never break is the 1:1 pairing.)
    resolved_ids = [e["loan_id"] for e in events if e.get("type") == "resolution"]
    disputed_ids = {e["loan_id"] for e in events if e.get("type") == "round"}
    assert 1 <= len(resolved_ids) <= n
    assert len(resolved_ids) == len(set(resolved_ids)), "a dispute resolved twice"
    assert set(resolved_ids) == disputed_ids, "every dispute resolves exactly once"
    assert types[-1] == "done"

    # Every resolution has a real loan_id and a valid terminal state.
    valid_resolutions = {"upheld", "overridden", "escalated_approved", "escalated_rejected"}
    for e in events:
        if e.get("type") == "resolution":
            assert e["loan_id"]
            assert e["resolution"] in valid_resolutions
            assert e["resolved_by"] in {"risk_model", "arbiter", "human"}


# --------------------------------------------------------------------------- #
# /api/run non-stream still works
# --------------------------------------------------------------------------- #
def test_non_stream_run_still_works(client, token):
    # /api/run stays header-auth only (the stream route gets the query-param
    # fallback for EventSource). Pass the Bearer token as a header.
    r = client.post("/api/run?brain=mock", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert "payload" in body
    assert "report" in body
    assert "steps" in body


# --------------------------------------------------------------------------- #
# Brain unavailable (e.g. brain=qwen with no DASHSCOPE_API_KEY) → clean 503,
# not a bare 500. Regression for the "Run live (Qwen)" 500.
# --------------------------------------------------------------------------- #
def _raise_brain(_brain):
    raise RuntimeError("DashScope unreachable: invalid API key")


def test_run_qwen_unavailable_returns_503(client, token, monkeypatch):
    monkeypatch.setattr(main_mod, "get_llm", _raise_brain)
    r = client.post("/api/run?brain=qwen", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 503
    body = r.json()
    assert "unavailable" in body["error"].lower()
    assert body["brain"] == "qwen"


def test_stream_qwen_unavailable_returns_503(client, token, monkeypatch):
    monkeypatch.setattr(main_mod, "get_llm", _raise_brain)
    r = client.get(f"/api/run/stream?token={token}&brain=qwen")
    assert r.status_code == 503
    assert "unavailable" in r.json()["error"].lower()


def test_mock_run_unaffected_by_the_guard(client, token):
    # mock never calls get_llm's qwen path → still 200.
    r = client.post("/api/run?brain=mock", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
