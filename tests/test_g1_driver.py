"""G1 — the work-list "why this row" driver chip (backend half).

`rank(scored, model=, features=)` attaches an optional ``top_driver`` per row:
the model's single largest signed contribution behind that account's score
(via ``waspada.model.risk.explain``), formatted ``"<label> ↑|↓"``. This pins:

  1. with a fitted model + features, every ranked row carries a ``top_driver``
     that names a real feature and a direction arrow;
  2. WITHOUT the model (the standalone / pre-WA-050 path) the key is absent —
     i.e. the enrichment is purely additive and the old output is unchanged.
"""
from __future__ import annotations

import datetime as dt

import pyarrow as pa

from waspada.model.risk import FEATURE_COLUMNS, predict, train
from waspada.insight.ranking import rank
from waspada.schema import FeatureFrame, ScoredAccounts, schema_from_dataclass, validate_table


def _feature_frame(rows: list[dict], as_of: dt.date) -> pa.Table:
    import dataclasses

    cols: dict[str, list] = {f.name: [] for f in dataclasses.fields(FeatureFrame)}
    for r in rows:
        for name in cols:
            cols[name].append(r[name])
    return pa.table(cols, schema=schema_from_dataclass(FeatureFrame))


def _frame() -> pa.Table:
    """Small mixed-class frame across vintages — same shape as test_model._tiny_frame."""
    as_of = dt.date(2024, 12, 1)
    rows = [
        dict(loan_id="L1", amount=15000.0, term=36, rate=20.0, grade="E",
             annual_income=58000.0, dti=25.0, purpose="debt_consolidation", region="West",
             loan_age=33, payment_ratio=0.2, outstanding_ratio=0.7,
             delinquency_status="Default", label_default=True, as_of_date=as_of),
        dict(loan_id="L2", amount=8000.0, term=36, rate=6.0, grade="A",
             annual_income=92000.0, dti=6.0, purpose="credit_card", region="Northeast",
             loan_age=11, payment_ratio=0.9, outstanding_ratio=0.2,
             delinquency_status="0", label_default=False, as_of_date=as_of),
        dict(loan_id="L3", amount=24000.0, term=60, rate=9.0, grade="B",
             annual_income=110000.0, dti=12.0, purpose="home_improvement", region="South",
             loan_age=42, payment_ratio=0.95, outstanding_ratio=0.05,
             delinquency_status="0", label_default=False, as_of_date=as_of),
        dict(loan_id="L4", amount=5000.0, term=36, rate=22.0, grade="E",
             annual_income=40000.0, dti=28.0, purpose="medical", region="Midwest",
             loan_age=15, payment_ratio=0.1, outstanding_ratio=0.85,
             delinquency_status="Default", label_default=True, as_of_date=as_of),
        dict(loan_id="L5", amount=12000.0, term=60, rate=19.0, grade="D",
             annual_income=70000.0, dti=24.0, purpose="car", region="West",
             loan_age=22, payment_ratio=0.3, outstanding_ratio=0.6,
             delinquency_status="Default", label_default=True, as_of_date=as_of),
    ]
    return _feature_frame(rows, as_of)


def test_rank_attaches_top_driver_when_model_supplied():
    frame = _frame()
    model = train(frame)
    scored = predict(model, frame)
    validate_table(scored, ScoredAccounts, name="scored")  # sanity: rank input is valid

    ranked = rank(scored, top_n=10, model=model, features=frame)

    assert ranked, "expected a non-empty work-list"
    for rec in ranked:
        assert "top_driver" in rec, f"row {rec['loan_id']} has no top_driver"
        driver = rec["top_driver"]
        assert isinstance(driver, str) and driver
        # ends with a direction arrow…
        assert driver[-1] in ("↑", "↓"), driver
        # …and names one of the model's real features (num__/cat__ label, e.g. "dti=..").
        assert any(driver.startswith(f) or f in driver for f in FEATURE_COLUMNS), driver


def test_rank_omits_top_driver_without_model():
    """Additive contract: no model/features → no top_driver key (old output intact)."""
    frame = _frame()
    model = train(frame)
    scored = predict(model, frame)

    ranked = rank(scored, top_n=10)  # no model/features

    assert ranked
    assert all("top_driver" not in rec for rec in ranked)


def test_rank_top_driver_survives_a_bad_model():
    """A model that can't explain (empty dict) degrades to no key, never raises."""
    frame = _frame()
    model = train(frame)
    scored = predict(model, frame)

    ranked = rank(scored, top_n=10, model={}, features=frame)

    assert ranked
    assert all("top_driver" not in rec for rec in ranked)
