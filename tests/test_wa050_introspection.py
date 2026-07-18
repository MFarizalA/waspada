"""WA-050 acceptance — the Actuary can introspect its own model.

Before this, the debate's defense saw only raw feature *values*, never *why the
model* produced the band — so its rebuttal was a plausible rationalization, not
an explanation ("the counsel had never met its client"). WA-050 adds
:func:`waspada.model.risk.explain`, an exact decomposition of the score into
signed logit contributions, and threads the top drivers into both the Actuary's
rebuttal prompt and the Skeptic's challenge prompt.

The load-bearing assertion is the **round-trip**: because the model is linear,
``intercept + Σ(all contributions) == logit(p_default)``. If that holds, the
drivers are the model's actual reasoning, not an approximation.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pyarrow as pa
import pytest

from waspada.agents import AgentContext, MockLLM
from waspada.agents.analytics import AnalyticsAgent
from waspada.agents.ingest import IngestAgent
from waspada.agents.risk_auditor import RiskAuditorAgent
from waspada.agents.risk_model import RiskModelAgent
from waspada.model.risk import explain, format_drivers, predict, train

from tests.test_wa016_debate import _raw_rows, _raw_table, _stub_fetch


@pytest.fixture
def scored_frame_model():
    """Run ingest→analytics→risk_model; return (scored, frame, model, agent)."""
    raw = _raw_table(_raw_rows())
    ctx = AgentContext(lane="collections", data_handles={})
    ing = IngestAgent(MockLLM())
    ing.register_tool("fetch", _stub_fetch(raw))
    ctx = ctx.with_result(ing.run(ctx))
    ctx = ctx.with_result(AnalyticsAgent(MockLLM(), as_of=dt.date(2024, 12, 1)).run(ctx))
    frame = ctx.data_handles[ctx.prior_results[-1].artifact_ref]
    agent = RiskModelAgent(MockLLM())
    ctx = ctx.with_result(agent.run(ctx))
    scored = ctx.data_handles["scored_accounts"]
    return scored, frame, agent, ctx


# --------------------------------------------------------------------------- #
# explain() — the primitive
# --------------------------------------------------------------------------- #
def test_explain_contributions_decompose_the_logit_exactly(scored_frame_model):
    """intercept + sum(all contributions) == logit(p_default), for every account.

    This is what makes the drivers the model's *actual* reasoning: the number the
    Actuary defends is fully reconstructed from them, not approximated.
    """
    scored, frame, agent, _ = scored_frame_model
    model = agent._model
    clf = model["pipeline"].named_steps["clf"]
    intercept = float(clf.intercept_[0])

    ids = scored.column("loan_id").to_pylist()
    ps = scored.column("p_default").to_pylist()
    for lid, p in list(zip(ids, ps))[:15]:
        drivers = explain(model, frame, lid, top_n=999)  # all terms
        logit = intercept + sum(c for _, c in drivers)
        p_reconstructed = 1.0 / (1.0 + np.exp(-logit))
        assert abs(float(p) - p_reconstructed) < 1e-6, f"{lid}: {p} vs {p_reconstructed}"


def test_explain_ranks_by_absolute_contribution_and_caps_at_top_n(scored_frame_model):
    scored, frame, agent, _ = scored_frame_model
    lid = scored.column("loan_id")[0].as_py()
    drivers = explain(agent._model, frame, lid, top_n=3)
    assert len(drivers) <= 3
    mags = [abs(c) for _, c in drivers]
    assert mags == sorted(mags, reverse=True), "drivers must be ranked by |contribution|"
    # Labels carry the account's own values / active categories.
    assert all("=" in label for label, _ in drivers)


def test_explain_degrades_to_empty_on_missing_loan_or_bad_model(scored_frame_model):
    _, frame, agent, _ = scored_frame_model
    assert explain(agent._model, frame, "NOT-A-LOAN") == []
    assert explain({}, frame, "anything") == []            # no pipeline
    assert explain({"pipeline": None}, frame, "anything") == []


def test_format_drivers_is_a_signed_compact_line():
    line = format_drivers([("rate=24.00", 0.81), ("payment_ratio=0.95", -0.32)])
    assert line == "rate=24.00 (+0.81), payment_ratio=0.95 (-0.32)"
    assert format_drivers([]) == ""


# --------------------------------------------------------------------------- #
# The Actuary's defense now sees the drivers
# --------------------------------------------------------------------------- #
def test_rebuttal_prompt_cites_the_models_own_drivers(scored_frame_model):
    """The defense prompt must contain the model's signed contributions — this is
    the whole point: the Actuary defends its actual reasoning, not raw values."""
    scored, frame, agent, _ = scored_frame_model
    # A scripted brain so defend_score runs deterministically offline.
    import json
    from waspada.agents.protocol import Dispute, DisputeRound

    lid = scored.column("loan_id")[0].as_py()
    band = scored.column("score_band")[0].as_py()
    dispute = Dispute(
        loan_id=lid, opened_by="risk_auditor", model_band=band, auditor_view="Low",
        rounds=[DisputeRound(round_no=1, speaker="risk_auditor", claim="looks fine")],
    )

    captured = {}
    real_prompt = agent._rebuttal_prompt

    def _spy(*a, **k):
        p = real_prompt(*a, **k)
        captured["prompt"] = p
        return p

    agent._rebuttal_prompt = _spy  # type: ignore[method-assign]
    agent.llm = MockLLM(script=[json.dumps(
        {"verdict": "uphold", "confidence": 0.8, "claim": "band stands", "evidence": []})])
    agent.defend_score(dispute, scored, frame)

    prompt = captured["prompt"]
    assert "drivers" in prompt.lower()
    # At least one real driver term (feature=value (+/-contribution)) is present.
    expected = format_drivers(explain(agent._model, frame, lid, top_n=5))
    assert expected.split(" (")[0] in prompt   # first feature=value token appears


# --------------------------------------------------------------------------- #
# The Skeptic's challenge now sees the drivers (grounded, not vibes)
# --------------------------------------------------------------------------- #
def test_auditor_challenge_prompt_cites_drivers_when_model_handle_present(scored_frame_model):
    scored, frame, agent, ctx = scored_frame_model
    auditor = RiskAuditorAgent(MockLLM(), k=4)
    # Simulate the run wiring: the model handle is published by risk_model.
    auditor._run_model = ctx.data_handles["risk_model"]
    prompt = auditor._prompt(auditor._account_context(scored, frame, 0))
    assert "drivers" in prompt.lower()


def test_auditor_challenge_prompt_omits_drivers_without_a_model_handle(scored_frame_model):
    """Back-compat: a standalone auditor (no risk_model handle) just gets a
    thinner prompt — no crash, no drivers line."""
    scored, frame, _, _ = scored_frame_model
    auditor = RiskAuditorAgent(MockLLM(), k=4)
    assert auditor._run_model is None
    ctx = auditor._account_context(scored, frame, 0)
    assert ctx["drivers"] == ""
    assert "drivers" not in auditor._prompt(ctx).lower()
