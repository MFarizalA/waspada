"""Origination lane (WA-033..038) — contract, features, model, insight, e2e.

The second decision lane on the same risk engine + Agent Society. Pins:

  1. WA-034 — RawApplications round-trips through parquet; types importable.
  2. WA-035 — build_features → valid, non-null ApplicationFeatureFrame; the
     leakage guard (outcome columns never in the feature matrix).
  3. WA-036 — train/predict on the origination spec: valid ScoredApplications,
     hold-out AUC > chance, out-of-time application-cohort split.
  4. WA-037 — decide() applies the matrix (+ the adjudicated final_band);
     origination_health/alerts math.
  5. WA-033 — plan("origination") returns the step order (guard lifted) and the
     full pipeline runs offline end-to-end on a synthetic snapshot.
"""
from __future__ import annotations

import datetime as dt

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from waspada.agents.__main__ import _sample_raw_applications
from waspada.features.origination import build_features
from waspada.insight.origination import (
    ORIGINATION_ACTION_BY_BAND,
    decide,
    origination_alerts,
    origination_health,
)
from waspada.model.risk import ORIGINATION_SPEC, predict, train
from waspada.schema import (
    ApplicationFeatureFrame,
    RawApplications,
    ScoredApplications,
    schema_from_dataclass,
    validate_table,
)

AS_OF = dt.date(2024, 12, 1)


@pytest.fixture(scope="module")
def raw() -> pa.Table:
    return _sample_raw_applications(n=240, seed=13)


@pytest.fixture(scope="module")
def frame(raw) -> pa.Table:
    return build_features(raw, AS_OF)


# --------------------------------------------------------------------- WA-034
def test_contract_roundtrips_through_parquet(raw, tmp_path_factory):
    p = tmp_path_factory.mktemp("orig") / "apps.parquet"
    pq.write_table(raw, str(p))
    back = pq.read_table(str(p))
    validate_table(back, RawApplications, name="roundtrip")
    assert back.num_rows == raw.num_rows


def test_collections_contract_untouched():
    """Frozen-contract regression: the collections shapes didn't move."""
    from waspada.schema import FeatureFrame, RawLoans, ScoredAccounts

    assert [f.name for f in __import__("dataclasses").fields(RawLoans)][:3] == ["loan_id", "amount", "term"]
    assert "payment_ratio" in [f.name for f in __import__("dataclasses").fields(FeatureFrame)]
    assert [f.name for f in __import__("dataclasses").fields(ScoredAccounts)][0] == "loan_id"


# --------------------------------------------------------------------- WA-035
def test_features_valid_and_non_null(frame):
    validate_table(frame, ApplicationFeatureFrame, name="frame")
    import pyarrow.compute as pc
    for f in __import__("dataclasses").fields(ApplicationFeatureFrame):
        assert pc.sum(pc.is_null(frame.column(f.name))).as_py() == 0, f.name


def test_leakage_guard_outcomes_never_in_features(frame):
    """The acceptance centerpiece: outcome columns are not in the matrix."""
    for leaky in ("funded", "funded_default"):
        assert leaky not in frame.column_names, f"{leaky} leaked into the frame"
    for leaky in ORIGINATION_SPEC.leakage_excluded:
        assert leaky not in ORIGINATION_SPEC.feature_columns, f"{leaky} in feature columns"
    # label present for training but never a feature
    assert "label_default" in frame.column_names
    assert "label_default" not in ORIGINATION_SPEC.feature_columns


def test_loan_to_income_derivation(frame):
    amounts = frame.column("amount").to_pylist()
    incomes = frame.column("annual_income").to_pylist()
    ltis = frame.column("loan_to_income").to_pylist()
    for a, inc, l in list(zip(amounts, incomes, ltis))[:20]:
        assert l == pytest.approx(a / inc if inc else 0.0)


# --------------------------------------------------------------------- WA-036
def test_train_predict_origination(frame):
    model = train(frame, spec=ORIGINATION_SPEC)
    assert model["lane"] == "origination"
    assert model["id_column"] == "application_id"
    assert model["metrics"]["auc"] is not None and model["metrics"]["auc"] > 0.5
    # out-of-time split: train/test cohorts disjoint
    split = model["split"]
    if split["method"] == "application_cohort":
        assert not (set(split["train_years"]) & set(split["test_years"]))

    scored = predict(model, frame)
    validate_table(scored, ScoredApplications, name="scored")
    probs = scored.column("p_default").to_pylist()
    assert all(0.0 <= p <= 1.0 for p in probs)
    # origination carry-forwards present
    assert "application_year" in scored.column_names
    assert "amount" in scored.column_names


def test_explain_uses_origination_features(frame):
    from waspada.model.risk import explain

    model = train(frame, spec=ORIGINATION_SPEC)
    app_id = frame.column("application_id")[0].as_py()
    drivers = explain(model, frame, app_id, top_n=3)
    assert drivers, "explain must decompose the origination score"
    joined = " ".join(label for label, _ in drivers)
    # drivers cite origination features, never collections-only ones
    assert "payment_ratio" not in joined and "loan_age" not in joined


# --------------------------------------------------------------------- WA-037
def test_decide_applies_matrix(frame):
    model = train(frame, spec=ORIGINATION_SPEC)
    scored = predict(model, frame)
    wl = decide(scored, top_n=50)
    assert wl and all(r["recommended_action"] in ("approve", "refer", "reject") for r in wl)
    for r in wl:
        assert r["recommended_action"] == ORIGINATION_ACTION_BY_BAND.get(r["score_band"], "refer")


def test_decide_honours_adjudicated_final_band(frame):
    model = train(frame, spec=ORIGINATION_SPEC)
    scored = predict(model, frame)
    # society overrides row 0's band to Very Low → decision must become approve
    n = scored.num_rows
    fb = [None] * n
    ids = scored.column("application_id").to_pylist()
    fb0_id = ids[0]
    fb[0] = "Very Low"
    scored2 = scored.append_column("final_band", pa.array(
        [b if b is not None else "" for b in fb], type=pa.string()))
    # empty string = no override; decide treats falsy as "use model band"
    wl = decide(scored2, top_n=n)
    row = next(r for r in wl if r["application_id"] == fb0_id)
    assert row["recommended_action"] == "approve"
    assert row["final_band"] == "Very Low"


def test_health_and_alerts(frame):
    model = train(frame, spec=ORIGINATION_SPEC)
    scored = predict(model, frame)
    health = origination_health(scored)
    assert 0.0 <= health["approval_rate"] <= 1.0
    assert 0.0 <= health["projected_default_rate"] <= 1.0
    assert abs(sum(health["band_mix"].values()) - 1.0) < 0.02
    assert health["approved_volume"] >= 0.0

    # forced-breach alert fires
    hot = dict(health)
    hot["projected_default_rate"] = 0.5
    alerts = origination_alerts(hot)
    assert any(a["metric"] == "projected_default_rate" for a in alerts)


def test_decide_rejects_bad_matrix(frame):
    model = train(frame, spec=ORIGINATION_SPEC)
    scored = predict(model, frame)
    with pytest.raises(ValueError):
        decide(scored, action_by_band={"Very High": "teleport"})


# --------------------------------------------------------------------- WA-033
def test_plan_origination_no_longer_raises():
    from waspada.agents.orchestrator import Orchestrator

    orch = Orchestrator()
    steps = orch.plan("origination")
    assert steps == ["data_engineer", "data_analyst", "risk_model", "risk_auditor", "insight"]


def test_origination_pipeline_end_to_end_offline(raw):
    """The full society runs the origination lane offline (synthetic snapshot)."""
    from waspada.agents.base import ApprovalGate
    from waspada.agents.data_engineer import DataEngineerAgent
    from waspada.agents.orchestrator import Orchestrator
    from waspada.agents.protocol import AgentContext

    orch = Orchestrator(gate=ApprovalGate(auto_approve=True), as_of=AS_OF, top_n=20)
    orig_build = orch._build_agents

    def _build():
        agents = orig_build()
        for a in agents:
            if isinstance(a, DataEngineerAgent):
                a.register_tool("fetch", lambda *, lane="origination", limit=None: raw)
        return agents

    orch._build_agents = _build  # type: ignore[method-assign]
    orch.plan("origination")
    ctx = AgentContext(lane="origination", data_handles={}, meta={"cli": True})
    result = orch.run(ctx)

    assert result.ok, result.notes
    payload = getattr(orch, "_final_ctx", ctx).data_handles[result.artifact_ref]
    assert payload["lane"] == "origination"
    assert payload["work_list"], "expected decided applications"
    assert "approval_rate" in payload["portfolio_health"]
    for row in payload["work_list"]:
        assert row["recommended_action"] in ("approve", "refer", "reject")
    # the analyst report reads lane-aware
    text = orch.report(payload)
    assert "Origination" in text and "Approval rate" in text
