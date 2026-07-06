"""QA — Model sanity: AUC/calibration on the vintage test split.

The model (waspada/model/risk.py) is now implemented (commit 1ee2f78), so these
tests run for real rather than as pending scaffolding. They check that the
trained model is better than chance on an out-of-time split and that the
predicted probabilities are sane.

Findings -> tests/qa/REPORT.md "Model sanity" section.
"""
from __future__ import annotations

import datetime as dt
import dataclasses

import numpy as np
import pyarrow as pa
import pytest

from waspada.schema import RawLoans, schema_from_dataclass
from waspada.features.collections import build_features

from .conftest import synthetic_raw_rows


def _frame_from_rows(rows, as_of):
    cols = {f.name: [] for f in dataclasses.fields(RawLoans)}
    for r in rows:
        for name in cols:
            cols[name].append(r[name])
    raw = pa.table(cols, schema=schema_from_dataclass(RawLoans))
    return build_features(raw, as_of=as_of)


@pytest.fixture
def large_frame():
    """A bigger synthetic frame with multiple vintages and a learnable signal:
    high dti + high rate -> more likely default. Enough rows for a real split.
    """
    import random
    rng = random.Random(7)
    statuses_pool = ["Charged Off", "Default", "Current", "Fully Paid",
                     "Late (16-30 days)", "Late (31-120 days)"]
    rows = []
    for i in range(400):
        high_risk = rng.random() < 0.5
        dti = rng.uniform(20, 40) if high_risk else rng.uniform(2, 15)
        rate = rng.uniform(18, 28) if high_risk else rng.uniform(5, 12)
        grade = rng.choice(["D", "E"]) if high_risk else rng.choice(["A", "B"])
        # Signal: high-risk rows default more often (but not deterministically).
        if high_risk:
            status = rng.choices(statuses_pool, weights=[5, 4, 3, 2, 2, 2])[0]
        else:
            status = rng.choices(statuses_pool, weights=[1, 1, 8, 6, 1, 1])[0]
        year = rng.choice([2021, 2022, 2023])
        amount = rng.uniform(3000, 25000)
        rows.append(dict(
            loan_id=f"QL-{i:04d}", amount=amount, term=rng.choice([36, 60]),
            rate=rate, grade=grade, annual_income=rng.uniform(30000, 120000),
            dti=dti, issue_date=dt.date(year, rng.randint(1, 12), rng.randint(1, 28)),
            purpose=rng.choice(["debt_consolidation", "credit_card", "medical", "car"]),
            region=rng.choice(["DKI Jakarta", "Bali", "Jawa Barat", "Banten"]),
            outstanding_principal=rng.uniform(0, amount * 0.8),
            total_paid=rng.uniform(0, amount * 0.9),
            current_status=status,
        ))
    return _frame_from_rows(rows, dt.date(2024, 12, 1))


class TestVintageSplitAndMetrics:
    def test_train_carries_split_metadata(self, large_frame):
        from waspada.model.risk import train
        model = train(large_frame)
        split = model["split"]
        assert split["method"] in {"vintage", "shuffle_fallback"}
        assert model["metrics"]["n_train"] > 0
        assert model["metrics"]["n_test"] > 0

    def test_auc_above_chance_on_separable_data(self, large_frame):
        """F-MS-01: on data with a real signal, the model's out-of-time AUC
        must beat chance (0.5). If it doesn't, either the split leaked or the
        model can't learn — either way flag it."""
        from waspada.model.risk import train
        model = train(large_frame)
        auc = model["metrics"].get("auc")
        if auc is None:
            pytest.skip("AUC not computable (single class in test split)")
        assert auc > 0.5, f"model no better than chance: AUC={auc:.3f}"

    def test_model_artifact_lists_leakage_excluded(self, large_frame):
        from waspada.model.risk import train, FEATURE_COLUMNS
        model = train(large_frame)
        assert set(model["leakage_excluded"]) >= {"delinquency_status", "label_default"}
        assert set(model["feature_columns"]) == set(FEATURE_COLUMNS)


class TestPredictSanity:
    def test_predict_probs_in_unit_interval(self, large_frame):
        from waspada.model.risk import train, predict
        model = train(large_frame)
        scored = predict(model, large_frame)
        probs = scored.column("p_default").to_pylist()
        assert all(0.0 <= p <= 1.0 for p in probs), "p_default out of [0,1]"
        assert all(np.isfinite(p) for p in probs), "non-finite p_default"

    def test_predict_output_validates_against_scored_accounts(self, large_frame):
        from waspada.model.risk import train, predict
        from waspada.schema import ScoredAccounts, validate_table
        model = train(large_frame)
        scored = predict(model, large_frame)
        validate_table(scored, ScoredAccounts, name="predict(scored)")

    def test_high_dti_high_rate_scores_higher_on_average(self, large_frame):
        """F-MS-02 (sanity): the engineered signal (dti/rate) should show up in
        the ranking — the high-risk bucket should score higher on average.
        Documents that the model is using the signal, not ignoring it."""
        from waspada.model.risk import train, predict
        model = train(large_frame)
        scored = predict(model, large_frame)
        probs = scored.column("p_default").to_pylist()
        dti = large_frame.column("dti").to_pylist()
        hi = [p for d, p in zip(dti, probs) if d >= 20]
        lo = [p for d, p in zip(dti, probs) if d < 20]
        if hi and lo:
            assert np.mean(hi) >= np.mean(lo), (
                f"high-dti mean p={np.mean(hi):.3f} not >= low-dti mean "
                f"p={np.mean(lo):.3f} — model may be ignoring the risk signal"
            )
