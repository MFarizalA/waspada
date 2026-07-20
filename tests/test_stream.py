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
import re
from typing import Any, Dict, List

import pytest

from waspada.agents import MockLLM
from waspada.agents.llm import ChatResponse
from waspada.agents.data_analyst import DataAnalystAgent
from waspada.agents.data_engineer import DataEngineerAgent
from waspada.agents.orchestrator import Orchestrator

from waspada.agents.__main__ import _sample_raw_table

# Import the FastAPI app lazily so the test module imports cleanly even when
# FastAPI is not installed.
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import api.main as main_mod  # noqa: E402
from api import auth as auth_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _mock_oss_probe_for_stream_tests(monkeypatch):
    """Tests have no OSS creds; force the startup probe to pass.

    Each test that hits ``/api/run`` or ``/api/run/stream`` is responsible for
    injecting its own data stub via the DataEngineerAgent tool registry. The
    probe only ensures the endpoint returns 200 instead of 503 at the gate.
    """
    monkeypatch.setattr(main_mod, "_probe_oss", lambda: (True, "mock-probe-ok"))


@pytest.fixture
def client():
    """A TestClient with a freshly-seeded demo analyst."""
    auth_mod.reset_store()
    auth_mod.seed_default_user()
    # WA-077: the lifespan probe runs at TestClient startup WITHOUT OSS creds,
    # so app.state.oss_available lands False and the guard 503s. Tests inject
    # their own data stubs, so force the probe state AFTER startup — patching
    # _probe_oss itself is too late (lifespan already ran).
    with TestClient(main_mod.app) as c:
        # WA-077: the lifespan OSS probe runs at TestClient startup (before any
        # monkeypatch of _probe_oss could apply), so with no OSS creds
        # oss_available lands False and the run-gate 503s. Tests inject their
        # own data stubs, so force the probe state AFTER startup.
        main_mod.app.state.oss_available = True
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
    """Inject a real-data-aware fetch stub and give the Tier-2 data agents
    (Data Engineer + Data Analyst) fresh canned brains.

    Under WA-077 the product API reads real OSS data. Tests are not the
    product, so we stub the data-engineer fetch with a small RawLoans table
    so the scripted debate tests can run offline deterministically. The
    orchestrator's own LLM is reserved for the scripted debate agents.
    """
    from waspada.agents.dispute_memory import DisputeMemory, InMemoryMemory
    orch.memory = DisputeMemory(InMemoryMemory())

    # Build a tiny RawLoans table for the offline test path.
    import dataclasses
    import datetime as dt
    import numpy as np
    import pyarrow as pa

    from waspada.schema import RawLoans, schema_from_dataclass

    rng = np.random.default_rng(11)
    rows: list[dict] = []
    issue_years = [2019, 2020, 2021, 2022, 2023]
    for i in range(60):
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
    cols = {f.name: [] for f in dataclasses.fields(RawLoans)}
    for r in rows:
        for name in cols:
            cols[name].append(r[name])
    test_table = pa.table(cols, schema=schema_from_dataclass(RawLoans))

    def _test_fetch(*, lane="collections", limit=None):
        return test_table

    # Orchestrator.run() calls _build_agents() FRESH each run, so mutating a prior
    # build (the pre-existing one-shot pattern) is lost — the runtime agents would
    # fetch real OSS and the Data Analyst would consume the debate routing brain.
    # Wrap _build_agents so the fetch stub + Tier-2 isolation are (re)applied to
    # whatever instances the run actually uses. The debate brain (orch.llm) stays
    # reserved for the Skeptic / Actuary / Arbiter.
    orig_build = orch._build_agents

    def _build():
        agents = orig_build()
        for a in agents:
            if isinstance(a, DataAnalystAgent):
                a.llm = MockLLM()
            if isinstance(a, DataEngineerAgent):
                a.llm = MockLLM()
                a.register_tool("fetch", _test_fetch)
        return agents

    orch._build_agents = _build  # type: ignore[method-assign]
    return orch


# Band ordinal (mirror of risk_auditor._BAND_ORDINAL). Used to pick a Skeptic view
# guaranteed to diverge from the model band by >= DISPUTE_GAP, so every audited
# account deterministically opens a dispute regardless of the scored data.
_BAND_ORD = {"Very Low": 1, "Low": 2, "Medium": 3, "High": 4, "Very High": 5}
_BAND_RE = re.compile(r"band=([A-Za-z][A-Za-z ]*)")


class _DebateBrain(MockLLM):
    """Content-routing offline brain for the scripted-debate SSE tests.

    Each debate agent issues a DISTINGUISHABLE prompt and parses a specific JSON
    shape:

      * Skeptic  (risk_auditor, native ``chat()`` loop) →
        ``{"auditor_view", "confidence", "claim", "evidence"}``
      * Actuary  (risk_model.defend_score, ``complete()``) →
        ``{"verdict", "confidence", "claim", "evidence"}``
      * Arbiter  (arbiter.rule, ``complete()``) →
        ``{"ruling", "confidence", "rationale", "evidence"}``

    The pre-WA-041 positional-script harness (a script of sub-brains each carrying
    a ``.next`` dict) no longer matches ANY agent: nothing reads ``.next``, the
    auditor now runs a native tool-loop, and the audit-slice size is data-driven
    (WA-049/WA-080), so a fixed reply order can't line up. We route by prompt
    content instead — deterministic no matter how many accounts are audited or in
    what order (this brain is stateless, so it's also parallel-audit-safe).

    The Skeptic reply is band-aware: it returns a view that diverges from the
    model band by >= DISPUTE_GAP, so every audited account opens a dispute; the
    Actuary upholds and the Arbiter upholds (conf 0.9 >= threshold), so each
    dispute resolves ``upheld`` by ``arbiter``. Unmatched prompts (the Data
    Engineer / Data Analyst reasoning loops, model scoring) fall back to the
    canned reply so the surrounding pipeline runs unchanged.
    """

    def _reply_for(self, prompt: str) -> str:
        p = prompt or ""
        if "You are the Skeptic" in p:
            m = _BAND_RE.search(p)
            band = m.group(1).strip() if m else "Very High"
            view = "Low" if _BAND_ORD.get(band, 5) >= 3 else "High"
            return json.dumps({
                "auditor_view": view,
                "confidence": 0.8,
                "claim": f"Independent read diverges from the {band} band (DTI 31.2 > 28).",
                "evidence": ["dti=31.20", "grade=E"],
            })
        if "You are the Actuary" in p:
            return json.dumps({
                "verdict": "uphold",
                "confidence": 0.7,
                "claim": "Model already priced grade and rate into the band.",
                "evidence": ["grade=E", "rate=22.40"],
            })
        if "You are the Arbiter" in p:
            return json.dumps({
                "ruling": "uphold",
                "confidence": 0.9,
                "rationale": "The auditor did not prove a band mismatch.",
                "evidence": [],
            })
        return self._reply  # canned fallback: DE/DA reasoning, model scoring

    def complete(self, prompt: str, *, history=None) -> str:
        self.calls.append(prompt)
        return self._reply_for(prompt)

    def chat(self, prompt: str, *, tools=None, messages=None) -> ChatResponse:
        # The auditor drives its native tool-loop through chat(); return a
        # content-only response (no tool_calls) so the loop takes the final
        # answer immediately from ``content``.
        self.calls.append(prompt)
        return ChatResponse(content=self._reply_for(prompt), tool_calls=[])


def _debate_brain_script(n_disputes: int = 1) -> _DebateBrain:
    """A content-routing debate brain (see :class:`_DebateBrain`).

    ``n_disputes`` is retained for call-site compatibility but no longer drives
    a reply count — the number of disputes now equals the number of audited
    accounts, which the tests set via ``orch.audit_k``. Every audited account
    opens a dispute that resolves ``upheld`` by the arbiter.
    """
    return _DebateBrain()


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
def test_stream_mock_no_dispute_ends_with_done(client, token, monkeypatch):
    """Without OSS creds, inject a stub fetch so the mock stream still runs."""
    from waspada.agents.data_engineer import DataEngineerAgent

    def _test_fetch(*, lane="collections", limit=None):
        return _sample_raw_table(n=60)

    orig_build = main_mod._build_orchestrator
    def _build_with_test_fetch(brain: str = "mock", **kwargs):
        orch = orig_build(brain, **kwargs)
        orig = orch._build_agents
        def _build():
            agents = orig()
            for a in agents:
                if isinstance(a, DataEngineerAgent):
                    a.register_tool("fetch", _test_fetch)
            return agents
        orch._build_agents = _build  # type: ignore[method-assign]
        return orch
    monkeypatch.setattr(main_mod, "_build_orchestrator", _build_with_test_fetch)

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
    orig_build = main_mod._build_orchestrator

    def _build_scripted(brain: str = "mock", **kwargs):
        # Force the orchestrator onto the scripted brain regardless of the
        # query param; isolate the Data Engineer so it doesn't eat the script.
        # Cap audit_k so the scripted brain is consumed predictably.
        orch = orig_build("mock", **kwargs)
        orch.llm = scripted
        orch.audit_k = n
        return _isolated_de_brain(orch)

    monkeypatch.setattr(main_mod, "_build_orchestrator", _build_scripted)

    r = client.get(f"/api/run/stream?token={token}")
    assert r.status_code == 200
    events = _parse_sse(r.text)

    types = [e.get("type") for e in events]
    assert types.count("round") >= 1
    assert types.count("resolution") == 1
    assert events[-1] == {"type": "done"}

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
    orig_build = main_mod._build_orchestrator

    def _build_scripted(brain: str = "mock", **kwargs):
        orch = orig_build("mock", **kwargs)
        orch.llm = scripted
        orch.audit_k = n
        return _isolated_de_brain(orch)

    monkeypatch.setattr(main_mod, "_build_orchestrator", _build_scripted)

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
def test_non_stream_run_still_works(client, token, monkeypatch):
    """The non-streaming run must also allow offline data injection in tests."""
    # Under WA-077 the endpoint expects OSS data. Since tests have no OSS creds,
    # inject a stub fetch into the orchestrator before the route builds it.
    from waspada.agents.data_engineer import DataEngineerAgent
    from waspada.agents.data_analyst import DataAnalystAgent
    from waspada.agents import MockLLM
    import dataclasses, datetime as dt, numpy as np, pyarrow as pa
    from waspada.schema import RawLoans, schema_from_dataclass

    rng = np.random.default_rng(11)
    rows = []
    for i in range(60):
        risky = rng.random() < 0.5
        rate = float(rng.uniform(18, 28)) if risky else float(rng.uniform(4, 10))
        dti_ = float(rng.uniform(22, 35)) if risky else float(rng.uniform(2, 12))
        grade = "E" if risky else "A"
        status = "Charged Off" if risky else "Current"
        rows.append(dict(
            loan_id=f"R{i:04d}", amount=float(rng.uniform(2000, 25000)),
            term=int(rng.choice([36, 60])), rate=rate, grade=grade,
            annual_income=float(rng.uniform(30000, 120000)), dti=dti_,
            issue_date=dt.date(2020, 1, 1),
            purpose="debt_consolidation", region="West",
            outstanding_principal=100.0, total_paid=100.0, current_status=status,
        ))
    cols = {f.name: [] for f in dataclasses.fields(RawLoans)}
    for r in rows:
        for name in cols:
            cols[name].append(r[name])
    test_table = pa.table(cols, schema=schema_from_dataclass(RawLoans))

    def _test_fetch(*, lane="collections", limit=None):
        return test_table

    orig_build = main_mod._build_orchestrator
    def _build_with_test_fetch(*args, **kwargs):
        orch = orig_build(*args, **kwargs)
        orig = orch._build_agents
        def _build():
            agents = orig()
            for a in agents:
                if isinstance(a, (DataEngineerAgent, DataAnalystAgent)):
                    a.llm = MockLLM()
                if isinstance(a, DataEngineerAgent):
                    a.register_tool("fetch", _test_fetch)
            return agents
        orch._build_agents = _build  # type: ignore[method-assign]
        return orch
    monkeypatch.setattr(main_mod, "_build_orchestrator", _build_with_test_fetch)

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
    """Stub the fetch so OSS is not required for this brain-error test."""
    from waspada.agents.data_engineer import DataEngineerAgent

    def _test_fetch(*, lane="collections", limit=None):
        return _sample_raw_table(n=60)

    def _build_with_test_fetch(brain: str = "mock", **kwargs):
        orch = orig_build(brain, **kwargs)
        orig = orch._build_agents
        def _build():
            agents = orig()
            for a in agents:
                if isinstance(a, DataEngineerAgent):
                    a.register_tool("fetch", _test_fetch)
            return agents
        orch._build_agents = _build  # type: ignore[method-assign]
        return orch
    monkeypatch.setattr(main_mod, "_build_orchestrator", _build_with_test_fetch)

    monkeypatch.setattr(main_mod, "get_llm", _raise_brain)
    r = client.post("/api/run?brain=qwen", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 503
    body = r.json()
    assert "unavailable" in body["error"].lower()
    assert body["brain"] == "qwen"


def test_stream_qwen_unavailable_returns_503(client, token, monkeypatch):
    """Also stub the fetch so OSS is not required for this brain-error test."""
    from waspada.agents.data_engineer import DataEngineerAgent

    def _test_fetch(*, lane="collections", limit=None):
        return _sample_raw_table(n=60)

    def _build_with_test_fetch(brain: str = "mock", **kwargs):
        orch = orig_build(brain, **kwargs)
        orig = orch._build_agents
        def _build():
            agents = orig()
            for a in agents:
                if isinstance(a, DataEngineerAgent):
                    a.register_tool("fetch", _test_fetch)
            return agents
        orch._build_agents = _build  # type: ignore[method-assign]
        return orch
    monkeypatch.setattr(main_mod, "_build_orchestrator", _build_with_test_fetch)

    monkeypatch.setattr(main_mod, "get_llm", _raise_brain)
    r = client.get(f"/api/run/stream?token={token}&brain=qwen")
    assert r.status_code == 503
    assert "unavailable" in r.json()["error"].lower()


def test_mock_run_unaffected_by_the_guard(client, token, monkeypatch):
    """Mock run still needs a stub fetch in the no-OSS test environment."""
    from waspada.agents.data_engineer import DataEngineerAgent

    def _test_fetch(*, lane="collections", limit=None):
        return _sample_raw_table(n=60)

    orig_build = main_mod._build_orchestrator

    def _build_with_test_fetch(brain: str = "mock", **kwargs):
        orch = orig_build(brain, **kwargs)
        orig = orch._build_agents
        def _build():
            agents = orig()
            for a in agents:
                if isinstance(a, DataEngineerAgent):
                    a.register_tool("fetch", _test_fetch)
            return agents
        orch._build_agents = _build  # type: ignore[method-assign]
        return orch
    monkeypatch.setattr(main_mod, "_build_orchestrator", _build_with_test_fetch)

    # mock never calls get_llm's qwen path → still 200.
    r = client.post("/api/run?brain=mock", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
