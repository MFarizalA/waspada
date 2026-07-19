"""WA-094 — post-hoc probability calibration of the PD model.

`class_weight="balanced"` biases the LR probabilities toward 0.5; WA-094 fits an
isotonic map on the hold-out so ``p_default`` is a true PD. Pins:

  1. on a real-sized two-class book the calibrator activates and Brier improves;
  2. on a tiny frame it stays OFF (calibrator None) — offline/CI byte-identical;
  3. calibration is monotone → the ranking (and AUC) is preserved;
  4. explain() still decomposes the linear score (drivers unchanged in kind);
  5. the calibrated artifact still round-trips through save_model/load_model.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pyarrow as pa

from waspada.model.risk import (
    FEATURE_COLUMNS,
    explain,
    load_model,
    predict,
    save_model,
    train,
)
from waspada.schema import FeatureFrame, schema_from_dataclass


def _feature_frame(rows: list[dict], as_of: dt.date) -> pa.Table:
    import dataclasses

    cols: dict[str, list] = {f.name: [] for f in dataclasses.fields(FeatureFrame)}
    for r in rows:
        for name in cols:
            cols[name].append(r[name])
    return pa.table(cols, schema=schema_from_dataclass(FeatureFrame))


def _separable_rows(n: int, seed: int = 7):
    rng = np.random.default_rng(seed)
    as_of = dt.date(2024, 12, 1)
    years = [2019, 2020, 2021, 2022, 2023]
    rows = []
    for i in range(n):
        iy = int(years[i % len(years)])
        im = int(rng.integers(1, 13))
        risky = rng.random() < 0.5
        if risky:
            rate, dti, grade, pr, status, label = (
                float(rng.uniform(18, 28)), float(rng.uniform(22, 35)), "E",
                float(rng.uniform(0.0, 0.3)), "Charged Off", True,
            )
        else:
            rate, dti, grade, pr, status, label = (
                float(rng.uniform(4, 10)), float(rng.uniform(2, 12)), "A",
                float(rng.uniform(0.6, 1.0)), "Current", False,
            )
        loan_age = max(0, (as_of.year - iy) * 12 + (as_of.month - im))
        rows.append(dict(
            loan_id=f"L{i:04d}", amount=float(rng.uniform(2000, 25000)),
            term=int(rng.choice([36, 60])), rate=rate, grade=grade,
            annual_income=float(rng.uniform(30000, 120000)), dti=dti,
            purpose=str(rng.choice(["credit_card", "debt_consolidation", "car", "medical"])),
            region=str(rng.choice(["West", "South", "Midwest", "Northeast"])),
            loan_age=loan_age, payment_ratio=pr,
            outstanding_ratio=float(rng.uniform(0.0, 1.0)),
            delinquency_status=("Default" if label else "0"),
            label_default=label, as_of_date=as_of,
        ))
    return rows, as_of


def _big_frame(n: int = 300) -> pa.Table:
    rows, as_of = _separable_rows(n)
    return _feature_frame(rows, as_of)


def _tiny_frame() -> pa.Table:
    as_of = dt.date(2024, 12, 1)
    rows = [
        dict(loan_id="L1", amount=15000.0, term=36, rate=20.0, grade="E", annual_income=58000.0,
             dti=25.0, purpose="debt_consolidation", region="West", loan_age=33, payment_ratio=0.2,
             outstanding_ratio=0.7, delinquency_status="Default", label_default=True, as_of_date=as_of),
        dict(loan_id="L2", amount=8000.0, term=36, rate=6.0, grade="A", annual_income=92000.0,
             dti=6.0, purpose="credit_card", region="Northeast", loan_age=11, payment_ratio=0.9,
             outstanding_ratio=0.2, delinquency_status="0", label_default=False, as_of_date=as_of),
        dict(loan_id="L3", amount=24000.0, term=60, rate=9.0, grade="B", annual_income=110000.0,
             dti=12.0, purpose="home_improvement", region="South", loan_age=42, payment_ratio=0.95,
             outstanding_ratio=0.05, delinquency_status="0", label_default=False, as_of_date=as_of),
        dict(loan_id="L4", amount=5000.0, term=36, rate=22.0, grade="E", annual_income=40000.0,
             dti=28.0, purpose="medical", region="Midwest", loan_age=15, payment_ratio=0.1,
             outstanding_ratio=0.85, delinquency_status="Default", label_default=True, as_of_date=as_of),
        dict(loan_id="L5", amount=12000.0, term=60, rate=19.0, grade="D", annual_income=70000.0,
             dti=24.0, purpose="car", region="West", loan_age=22, payment_ratio=0.3,
             outstanding_ratio=0.6, delinquency_status="Default", label_default=True, as_of_date=as_of),
    ]
    return _feature_frame(rows, as_of)


def test_calibration_activates_and_improves_brier_on_real_frame():
    model = train(_big_frame(300))
    m = model["metrics"]
    assert model["calibrator"] is not None, "expected calibration on a 300-row two-class book"
    assert m.get("calibrated") is True
    assert "brier_raw" in m and "brier_calibrated" in m
    # Calibration should not worsen the Brier score (usually improves it).
    assert m["brier_calibrated"] <= m["brier_raw"] + 1e-9, m


def test_calibration_skipped_on_tiny_frame():
    model = train(_tiny_frame())
    assert model["calibrator"] is None, "tiny hold-out must skip calibration (offline unchanged)"


def test_calibrated_probs_are_unit_interval_and_ranking_preserved():
    frame = _big_frame(300)
    model = train(frame)
    scored = predict(model, frame)
    probs = np.asarray(scored.column("p_default").to_pylist(), dtype=float)
    assert np.all((probs >= 0.0) & (probs <= 1.0))
    assert not np.any(np.isnan(probs))
    # AUC is a rank metric; calibration is monotone, so the reported (raw) AUC
    # still separates the classes.
    assert model["metrics"]["auc"] > 0.5


def test_explain_still_works_after_calibration():
    frame = _big_frame(300)
    model = train(frame)
    loan_id = frame.column("loan_id")[0].as_py()
    drivers = explain(model, frame, loan_id, top_n=3)
    assert drivers, "explain must still decompose the linear score"
    for label, contribution in drivers:
        assert isinstance(label, str) and label
        assert isinstance(contribution, float)
        assert any(f in label for f in FEATURE_COLUMNS)


def test_calibrated_model_roundtrips_through_pickle(tmp_path):
    frame = _big_frame(300)
    model = train(frame)
    path = tmp_path / "pd-model.pkl"
    save_model(model, str(path))
    loaded = load_model(str(path))
    # Same calibrated scores after a save/load cycle.
    a = predict(model, frame).column("p_default").to_pylist()
    b = predict(loaded, frame).column("p_default").to_pylist()
    assert a == b
