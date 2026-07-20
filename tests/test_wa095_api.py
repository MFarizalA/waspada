"""WA-095 phase-2b — /api/run accepts a per-run parameter matrix.

The human's submitted matrix governs THIS run and is stamped into the payload as
`policy_card` (provenance). An invalid matrix is a clean 400, not a 500.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

import api.main as main_mod  # noqa: E402
from api import auth as auth_mod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from waspada.agents.__main__ import _sample_raw_table  # noqa: E402
from waspada.agents.data_engineer import DataEngineerAgent  # noqa: E402


@pytest.fixture(autouse=True)
def _mock_oss_probe(monkeypatch):
    monkeypatch.setattr(main_mod, "_probe_oss", lambda: (True, "mock-probe-ok"))


@pytest.fixture
def client():
    auth_mod.reset_store()
    auth_mod.seed_default_user()
    with TestClient(main_mod.app) as c:
        main_mod.app.state.oss_available = True
        yield c


@pytest.fixture
def token(client):
    r = client.post("/api/auth/login", json={
        "email": auth_mod.DEFAULT_ANALYST_EMAIL, "password": auth_mod.DEFAULT_ANALYST_PASSWORD,
    })
    assert r.status_code == 200
    return r.json()["token"]


def _with_fetch_stub(monkeypatch):
    """Inject a small RawLoans fetch so the pipeline runs offline."""
    def _test_fetch(*, lane="collections", limit=None):
        return _sample_raw_table(n=80)

    orig_build = main_mod._build_orchestrator

    def _build(brain="mock", **kwargs):
        orch = orig_build(brain, **kwargs)  # passes policy= through
        orig = orch._build_agents

        def _build_agents():
            agents = orig()
            for a in agents:
                if isinstance(a, DataEngineerAgent):
                    a.register_tool("fetch", _test_fetch)
            return agents

        orch._build_agents = _build_agents  # type: ignore[method-assign]
        return orch

    monkeypatch.setattr(main_mod, "_build_orchestrator", _build)


def test_valid_matrix_stamps_policy_card(client, token, monkeypatch):
    _with_fetch_stub(monkeypatch)
    matrix = {
        "band_to_action": {"Very High": "call", "High": "call", "Medium": "watch",
                            "Low": "auto-cure", "Very Low": "auto-cure"},
        "audit_k": 6, "top_n": 15, "dispute_gap": 3, "arbiter_confidence": 0.7,
        "npl_threshold": 0.25, "vintage_threshold": 0.18,
    }
    r = client.post("/api/run?brain=mock",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"policy": matrix})
    assert r.status_code == 200, r.text
    payload = r.json()["payload"] if "payload" in r.json() else r.json()
    card = payload.get("policy_card")
    assert card is not None, "expected policy_card provenance in the payload"
    assert card["policy_id"].startswith("policy-")
    assert card["audit_k"] == 6 and card["top_n"] == 15 and card["dispute_gap"] == 3
    # top_n from the matrix drives the work-list cap.
    assert len(payload["work_list"]) <= 15


def test_invalid_matrix_returns_400(client, token):
    r = client.post("/api/run?brain=mock",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"policy": {"dispute_gap": 9}})  # out of [1,4]
    assert r.status_code == 400
    assert "matrix" in r.json()["error"].lower()


def test_no_body_runs_with_default_policy(client, token, monkeypatch):
    _with_fetch_stub(monkeypatch)
    r = client.post("/api/run?brain=mock", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
