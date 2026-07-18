"""WA-051 acceptance — absolute band edges calibrate the dispute gate.

`score_band` was a per-batch quintile: ~20% of ANY batch was stamped "Very
High", regardless of absolute PD. The Skeptic returns an *absolute* view, so the
admissibility gate diffed two different scales — manufacturing disputes on a
healthy book and staying silent on a distressed one.

WA-051 freezes the reference book's [20,40,60,80] score percentiles as absolute
PD edges in the model artifact; future batches are banded against those. The
headline test: a uniformly-healthy batch scored against a mixed-reference model
opens **zero** disputes, where the old per-batch banding would have forced its
top 20% into "Very High" and burned LLM calls arguing about benign accounts.
"""
from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pyarrow as pa
import pytest

from waspada.agents import AgentContext, MockLLM
from waspada.agents.analytics import AnalyticsAgent
from waspada.agents.ingest import IngestAgent
from waspada.agents.protocol import AgentResult, Status
from waspada.agents.risk_auditor import RiskAuditorAgent
from waspada.model.risk import _risk_level_bands, predict, train
from waspada.schema import RISK_LEVELS

from tests.test_wa016_debate import _raw_rows, _raw_table, _stub_fetch


def _frame(rows):
    raw = _raw_table(rows)
    ctx = AgentContext(lane="collections", data_handles={})
    ing = IngestAgent(MockLLM())
    ing.register_tool("fetch", _stub_fetch(raw))
    ctx = ctx.with_result(ing.run(ctx))
    ctx = ctx.with_result(AnalyticsAgent(MockLLM(), as_of=dt.date(2024, 12, 1)).run(ctx))
    return ctx.data_handles[ctx.prior_results[-1].artifact_ref]


def _healthy_rows(n=40, seed=3):
    rng = np.random.default_rng(seed)
    return [
        dict(loan_id=f"H{i:05d}", amount=8000.0, term=36,
             rate=float(rng.uniform(4, 8)), grade="A", annual_income=90000.0,
             dti=float(rng.uniform(2, 8)), issue_date=dt.date(2022, 6, 1),
             purpose="car", region="West", outstanding_principal=150.0,
             total_paid=4200.0, current_status="Current")
        for i in range(n)
    ]


# --------------------------------------------------------------------------- #
# _risk_level_bands — the primitive
# --------------------------------------------------------------------------- #
def test_edges_none_reproduces_per_batch_quintile_byte_for_byte():
    rng = np.random.default_rng(0)
    probs = rng.random(200)
    relative = _risk_level_bands(probs, edges=None)
    qs = np.percentile(probs, [20, 40, 60, 80])
    expected = _risk_level_bands(probs, edges=qs.tolist())
    assert relative == expected  # edges == this batch's own quintiles → identical


def test_absolute_edges_band_by_fixed_pd_thresholds():
    edges = [0.10, 0.25, 0.50, 0.75]
    probs = np.array([0.02, 0.20, 0.40, 0.60, 0.90])
    assert _risk_level_bands(probs, edges=edges) == [
        "Very Low", "Low", "Medium", "High", "Very High"]


# --------------------------------------------------------------------------- #
# train persists edges; predict on the SAME frame stays byte-identical
# --------------------------------------------------------------------------- #
def test_train_persists_band_edges_and_same_frame_predict_is_unchanged():
    frame = _frame(_raw_rows())
    model = train(frame)
    assert model["band_edges"] is not None and len(model["band_edges"]) == 4

    new_bands = predict(model, frame).column("score_band").to_pylist()
    # The old behaviour: quintiles of the same clipped probs, no edges.
    from waspada.model.risk import _X_y
    X, _ = _X_y(frame)
    p = np.clip(np.nan_to_num(model["pipeline"].predict_proba(X)[:, 1], nan=0.0), 0.0, 1.0)
    old_bands = _risk_level_bands(p, edges=None)
    assert new_bands == old_bands  # byte-for-byte on the training frame


def test_edgeless_artifact_falls_back_to_relative_banding():
    frame = _frame(_raw_rows())
    model = train(frame)
    model["band_edges"] = None  # simulate a pre-WA-051 artifact
    bands = predict(model, frame).column("score_band").to_pylist()
    assert bands and set(bands) <= set(RISK_LEVELS)  # valid labels, relative path


# --------------------------------------------------------------------------- #
# THE HEADLINE — a healthy batch, scored against a mixed reference, is calm.
# --------------------------------------------------------------------------- #
def _audit(scored, frame, model):
    """Run the Skeptic (scripted to see every account as 'Low') over `scored`."""
    low_view = json.dumps({"auditor_view": "Low", "confidence": 0.8,
                           "claim": "benign", "evidence": ["payment_ratio=0.98"]})
    auditor = RiskAuditorAgent(MockLLM(script=[low_view] * 20), k=8)
    ctx = AgentContext(lane="collections", data_handles={
        "scored_accounts": scored, "feature_frame": frame, "risk_model": model,
    })
    ctx = ctx.with_result(AgentResult(status=Status.OK, agent="risk_model",
                                      artifact_ref="scored_accounts"))
    auditor.run(ctx)
    return ctx.data_handles.get("risk_disputes") or []


def test_healthy_batch_under_absolute_edges_opens_no_spurious_disputes():
    reference = _frame(_raw_rows())          # mixed risky/safe → sensible edges
    model = train(reference)
    healthy = _frame(_healthy_rows())
    scored = predict(model, healthy)         # absolute edges → all benign

    bands = set(scored.column("score_band").to_pylist())
    assert "Very High" not in bands, "a healthy batch must not be stamped Very High"
    assert _audit(scored, healthy, model) == [], "absolute edges must not manufacture disputes"


def test_relative_banding_would_have_disputed_the_same_healthy_batch():
    """The contrast that proves the fix bites: with per-batch quintiles the SAME
    benign book forces ~20% into Very High and the Skeptic's 'Low' view opens
    spurious disputes."""
    reference = _frame(_raw_rows())
    model = train(reference)
    model["band_edges"] = None               # revert to relative banding
    healthy = _frame(_healthy_rows())
    scored = predict(model, healthy)

    assert "Very High" in scored.column("score_band").to_pylist()
    assert len(_audit(scored, healthy, model)) > 0, "relative banding should misfire here"


# --------------------------------------------------------------------------- #
# The Skeptic's prompt states the absolute thresholds
# --------------------------------------------------------------------------- #
def test_auditor_prompt_states_absolute_thresholds_when_edges_present():
    frame = _frame(_raw_rows())
    model = train(frame)
    scored = predict(model, frame)
    auditor = RiskAuditorAgent(MockLLM(), k=4)
    auditor._run_model = model
    prompt = auditor._prompt(auditor._account_context(scored, frame, 0))
    assert "Absolute band thresholds" in prompt
