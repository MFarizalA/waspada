"""WA-093 — PD monitoring & drift (PSI + per-run record).

Pins the pure monitoring core:
  1. PSI is ~0 for identical distributions and grows with a real shift;
  2. PSI crosses the significant threshold on a strong covariate shift;
  3. build_monitor_record carries the run metrics + band distribution + observed
     default rate, and adds per-feature PSI + drift flags only when a reference
     is supplied;
  4. it never raises on missing pieces (offline-safe).
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pyarrow as pa

from waspada.model.monitoring import (
    PSI_SIGNIFICANT,
    build_monitor_record,
    categorical_psi,
    feature_psi,
    population_stability_index,
)
from waspada.model.risk import predict, train
from waspada.schema import FeatureFrame, schema_from_dataclass


def _feature_frame(rows: list[dict], as_of: dt.date) -> pa.Table:
    import dataclasses

    cols: dict[str, list] = {f.name: [] for f in dataclasses.fields(FeatureFrame)}
    for r in rows:
        for name in cols:
            cols[name].append(r[name])
    return pa.table(cols, schema=schema_from_dataclass(FeatureFrame))


def _frame(n: int, *, shift: float = 0.0, seed: int = 3) -> pa.Table:
    """A FeatureFrame; ``shift`` nudges rate/dti upward to induce covariate drift."""
    rng = np.random.default_rng(seed)
    as_of = dt.date(2024, 12, 1)
    years = [2019, 2020, 2021, 2022, 2023]
    rows = []
    for i in range(n):
        iy = int(years[i % len(years)])
        risky = rng.random() < 0.5
        base_rate = rng.uniform(18, 28) if risky else rng.uniform(4, 10)
        base_dti = rng.uniform(22, 35) if risky else rng.uniform(2, 12)
        label = bool(risky)
        rows.append(dict(
            loan_id=f"L{i:04d}", amount=float(rng.uniform(2000, 25000)),
            term=int(rng.choice([36, 60])), rate=float(base_rate + shift),
            grade=("E" if risky else "A"),
            annual_income=float(rng.uniform(30000, 120000)),
            dti=float(base_dti + shift),
            purpose=str(rng.choice(["credit_card", "debt_consolidation", "car", "medical"])),
            region=str(rng.choice(["West", "South", "Midwest", "Northeast"])),
            loan_age=int(rng.integers(6, 48)),
            payment_ratio=float(rng.uniform(0.0, 0.3) if risky else rng.uniform(0.6, 1.0)),
            outstanding_ratio=float(rng.uniform(0.0, 1.0)),
            delinquency_status=("Default" if label else "0"),
            label_default=label, as_of_date=as_of,
        ))
    return _feature_frame(rows, as_of)


# --- PSI ------------------------------------------------------------------- #
def test_psi_zero_for_identical_distributions():
    rng = np.random.default_rng(0)
    x = rng.normal(size=500)
    assert population_stability_index(x, x) < 1e-6


def test_psi_grows_with_shift():
    rng = np.random.default_rng(1)
    ref = rng.normal(0, 1, size=1000)
    small = rng.normal(0.3, 1, size=1000)
    large = rng.normal(2.0, 1, size=1000)
    psi_small = population_stability_index(ref, small)
    psi_large = population_stability_index(ref, large)
    assert psi_large > psi_small > 0.0


def test_psi_degenerate_inputs_return_zero():
    assert population_stability_index([], [1, 2, 3]) == 0.0
    assert population_stability_index([5, 5, 5], [5, 5, 5]) == 0.0


def test_categorical_psi_detects_mix_change():
    ref = ["A"] * 80 + ["B"] * 20
    same = ["A"] * 78 + ["B"] * 22
    flipped = ["A"] * 20 + ["B"] * 80
    assert categorical_psi(ref, flipped) > categorical_psi(ref, same)


# --- feature_psi + record -------------------------------------------------- #
def test_feature_psi_flags_a_strong_shift():
    ref = _frame(400, shift=0.0, seed=3)
    drifted = _frame(400, shift=12.0, seed=4)  # rate/dti pushed way up
    psi = feature_psi(ref, drifted)
    assert "rate" in psi and "dti" in psi
    assert psi["rate"] > PSI_SIGNIFICANT, psi


def test_build_monitor_record_core_fields():
    frame = _frame(300, seed=3)
    model = train(frame)
    scored = predict(model, frame)
    rec = build_monitor_record(model, scored, frame)

    assert rec["n_scored"] == frame.num_rows
    assert 0.0 <= rec["observed_default_rate"] <= 1.0
    assert isinstance(rec["band_distribution"], dict) and rec["band_distribution"]
    assert abs(sum(rec["band_distribution"].values()) - 1.0) < 0.02
    # No reference supplied → no PSI section.
    assert "psi" not in rec


def test_build_monitor_record_adds_psi_with_reference():
    ref = _frame(400, shift=0.0, seed=3)
    cur = _frame(400, shift=12.0, seed=4)
    model = train(ref)
    scored = predict(model, cur)
    rec = build_monitor_record(model, scored, cur, reference=ref)

    assert "psi" in rec and "rate" in rec["psi"]
    assert "drift_significant" in rec
    assert "rate" in rec["drift_significant"], rec["psi"]
    assert rec["max_psi"] >= rec["psi"]["rate"] - 1e-9


def test_build_monitor_record_never_raises_on_thin_model():
    frame = _frame(300, seed=3)
    model = train(frame)
    scored = predict(model, frame)
    # An empty model dict must degrade, not crash.
    rec = build_monitor_record({}, scored, frame)
    assert rec["n_scored"] == frame.num_rows
    assert rec["auc"] is None
