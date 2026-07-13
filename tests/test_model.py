"""Risk-model tests (WA-005 acceptance).

CPU-only: builds a synthetic ``FeatureFrame``-shaped Arrow table and exercises
:mod:`waspada.model.risk` (train + predict). Covers the five acceptance checks:

  1. ``predict()`` returns one row per account, ``p_default ∈ [0,1]``, no NaNs.
  2. Vintage split implemented; train/test windows don't overlap.
  3. No outcome-derived field is a feature (documented leakage guard).
  4. AUC > 0.5 on a separable toy set.
  5. ``score_band`` are quintile bands; output validates against the contract.
"""
from __future__ import annotations

import datetime as dt
from typing import List

import numpy as np
import pyarrow as pa
import pytest

from waspada.model.risk import (
    FEATURE_COLUMNS,
    LEAKAGE_EXCLUDED,
    predict,
    train,
    issue_year_from_frame,
)
from waspada.schema import FeatureFrame, ScoredAccounts, schema_from_dataclass


# --------------------------------------------------------------------------- #
# Helpers — build a FeatureFrame-shaped Arrow table from row dicts.
# --------------------------------------------------------------------------- #
def _feature_frame(rows: list[dict], as_of: dt.date) -> pa.Table:
    """Assemble a FeatureFrame-contract table (all fields, non-null)."""
    cols: dict[str, list] = {f.name: [] for f in __import__("dataclasses").fields(FeatureFrame)}
    for r in rows:
        for name in cols:
            cols[name].append(r[name])
    return pa.table(cols, schema=schema_from_dataclass(FeatureFrame))


def _separable_rows(n: int = 200, seed: int = 7) -> tuple[list[dict], dt.date]:
    """A two-class FeatureFrame where features linearly separate the label.

    High-rate / high-DTI rows → default=True; low-rate / low-DTI → False.
    Multiple vintages so the chronological split has both sides populated.
    """
    rng = np.random.default_rng(seed)
    as_of = dt.date(2024, 12, 1)
    rows: list[dict] = []
    # 2019, 2020, 2021, 2022, 2023 — five vintages across the split.
    issue_years = [2019, 2020, 2021, 2022, 2023]
    for i in range(n):
        issue_year = int(issue_years[i % len(issue_years)])
        issue_month = int(rng.integers(1, 13))
        risky = rng.random() < 0.5
        if risky:
            rate = float(rng.uniform(18, 28))
            dti = float(rng.uniform(22, 35))
            grade = "E"
            payment_ratio = float(rng.uniform(0.0, 0.3))
            status = "Charged Off"
            label = True
        else:
            rate = float(rng.uniform(4, 10))
            dti = float(rng.uniform(2, 12))
            grade = "A"
            payment_ratio = float(rng.uniform(0.6, 1.0))
            status = "Current"
            label = False
        loan_age = (as_of.year - issue_year) * 12 + (as_of.month - issue_month)
        loan_age = max(0, loan_age)
        rows.append(dict(
            loan_id=f"L{i:04d}",
            amount=float(rng.uniform(2000, 25000)),
            term=int(rng.choice([36, 60])),
            rate=rate,
            grade=grade,
            annual_income=float(rng.uniform(30000, 120000)),
            dti=dti,
            purpose=str(rng.choice(["credit_card", "debt_consolidation", "car", "medical"])),
            region=str(rng.choice(["West", "South", "Midwest", "Northeast"])),
            loan_age=loan_age,
            payment_ratio=payment_ratio,
            outstanding_ratio=float(rng.uniform(0.0, 1.0)),
            delinquency_status=("Default" if label else "0"),
            label_default=label,
            as_of_date=dt.date(issue_year, issue_month, 1),
        ))
    # Overwrite as_of_date to the snapshot for all rows (so issue_year recon
    # is consistent), and recompute delinquency_status consistently.
    for r in rows:
        r["as_of_date"] = as_of
    return rows, as_of


def _tiny_frame() -> pa.Table:
    """A small hand-built frame (5 rows, mixed classes across vintages).

    Designed so both classes appear on *each side* of the chronological
    vintage split (risky loans are NOT all in one cohort) — train() needs
    both classes in the training split.
    """
    as_of = dt.date(2024, 12, 1)
    rows = [
        dict(loan_id="L1", amount=15000.0, term=36, rate=20.0, grade="E",
             annual_income=58000.0, dti=25.0,
             purpose="debt_consolidation", region="West",
             loan_age=33, payment_ratio=0.2, outstanding_ratio=0.7,
             delinquency_status="Default", label_default=True, as_of_date=as_of),
        dict(loan_id="L2", amount=8000.0, term=36, rate=6.0, grade="A",
             annual_income=92000.0, dti=6.0,
             purpose="credit_card", region="Northeast",
             loan_age=11, payment_ratio=0.9, outstanding_ratio=0.2,
             delinquency_status="0", label_default=False, as_of_date=as_of),
        # L3: older vintage, non-default → train side has a False too.
        dict(loan_id="L3", amount=24000.0, term=60, rate=9.0, grade="B",
             annual_income=110000.0, dti=12.0,
             purpose="home_improvement", region="South",
             loan_age=42, payment_ratio=0.95, outstanding_ratio=0.05,
             delinquency_status="0", label_default=False, as_of_date=as_of),
        # L4: newer vintage, default → test side has a True too.
        dict(loan_id="L4", amount=5000.0, term=36, rate=22.0, grade="E",
             annual_income=40000.0, dti=28.0,
             purpose="medical", region="Midwest",
             loan_age=15, payment_ratio=0.1, outstanding_ratio=0.85,
             delinquency_status="Default", label_default=True, as_of_date=as_of),
        dict(loan_id="L5", amount=12000.0, term=60, rate=19.0, grade="D",
             annual_income=70000.0, dti=24.0,
             purpose="car", region="West",
             loan_age=22, payment_ratio=0.3, outstanding_ratio=0.6,
             delinquency_status="Default", label_default=True, as_of_date=as_of),
    ]
    return _feature_frame(rows, as_of)


@pytest.fixture
def tiny_frame() -> pa.Table:
    return _tiny_frame()


@pytest.fixture
def separable_frame() -> pa.Table:
    rows, _ = _separable_rows(n=240)
    return _feature_frame(rows, dt.date(2024, 12, 1))


# --------------------------------------------------------------------------- #
# 1. predict() — shape, p_default ∈ [0,1], no NaNs, contract-valid.
# --------------------------------------------------------------------------- #
def test_predict_returns_one_row_per_account(tiny_frame):
    model = train(tiny_frame)
    scored = predict(model, tiny_frame)
    assert scored.num_rows == tiny_frame.num_rows


def test_predict_probs_in_unit_interval_no_nans(tiny_frame):
    model = train(tiny_frame)
    scored = predict(model, tiny_frame)
    probs = scored.column("p_default").to_pylist()
    assert all(not np.isnan(p) for p in probs)
    assert all(0.0 <= p <= 1.0 for p in probs)


def test_predict_output_validates_against_scored_accounts(tiny_frame):
    """The scored table carries every ScoredAccounts field with the right type."""
    from waspada.schema import validate_table

    model = train(tiny_frame)
    scored = predict(model, tiny_frame)
    validate_table(scored, ScoredAccounts, name="scored")  # raises on drift


def test_predict_score_band_is_quintile_string(tiny_frame):
    model = train(tiny_frame)
    scored = predict(model, tiny_frame)
    bands = set(scored.column("score_band").to_pylist())
    valid = {"Very Low", "Low", "Medium", "High", "Very High"}
    assert bands.issubset(valid), f"unexpected bands: {bands - valid}"


def test_predict_recommended_action_left_empty_for_ranking(tiny_frame):
    """WA-005 leaves recommended_action empty; WA-006 fills it."""
    model = train(tiny_frame)
    scored = predict(model, tiny_frame)
    assert all(a == "" for a in scored.column("recommended_action").to_pylist())


# --------------------------------------------------------------------------- #
# 2. Vintage split — train/test windows don't overlap.
# --------------------------------------------------------------------------- #
def test_vintage_split_windows_dont_overlap(separable_frame):
    """Reconstructed issue_year cohorts: train set years all < test set years."""
    from waspada.model.risk import _vintage_split

    train_idx, test_idx, split = _vintage_split(separable_frame, train_fraction=0.6)
    assert split["method"] == "vintage"
    assert set(split["train_years"]).isdisjoint(set(split["test_years"]))
    assert max(split["train_years"]) < min(split["test_years"])
    # Indices partition the frame (no overlap, full coverage).
    full = sorted(train_idx.tolist() + test_idx.tolist())
    assert full == list(range(separable_frame.num_rows))


def test_vintage_split_falls_back_on_single_cohort():
    """One vintage → seeded shuffle fallback, still a valid partition."""
    from waspada.model.risk import _vintage_split

    # Same loan_age (→ same issue_year) for every row: single cohort.
    as_of = dt.date(2024, 12, 1)
    rows = [
        dict(loan_id=f"S{i}", amount=1.0, term=36, rate=10.0, grade="B",
             annual_income=50000.0, dti=10.0, purpose="car", region="West",
             loan_age=24, payment_ratio=0.5, outstanding_ratio=0.5,
             delinquency_status=("Default" if i % 2 else "0"),
             label_default=bool(i % 2), as_of_date=as_of)
        for i in range(10)
    ]
    single = _feature_frame(rows, as_of)
    train_idx, test_idx, split = _vintage_split(single, train_fraction=0.6)
    assert split["method"] == "shuffle_fallback"
    assert len(train_idx) > 0 and len(test_idx) > 0
    full = sorted(train_idx.tolist() + test_idx.tolist())
    assert full == list(range(single.num_rows))


def test_issue_year_reconstruction_round_trips():
    """issue_year_from_frame recovers the cohort from loan_age + as_of."""
    as_of = dt.date(2024, 12, 1)
    rows = [
        dict(loan_id="A", amount=1.0, term=36, rate=1.0, grade="A",
             annual_income=1.0, dti=1.0, purpose="x", region="y",
             loan_age=33, payment_ratio=0.0, outstanding_ratio=0.0,
             delinquency_status="0", label_default=False, as_of_date=as_of),
    ]
    # loan_age 33 months back from 2024-12 → 2022-03 → year 2022
    frame = _feature_frame(rows, as_of)
    years = issue_year_from_frame(frame).to_pylist()
    assert years == [2022]


# --------------------------------------------------------------------------- #
# 3. Leakage guard — no outcome-derived field is a feature (documented).
# --------------------------------------------------------------------------- #
def test_no_outcome_leakage_in_features():
    """The frozen FeatureFrame's outcome/identifier fields are NOT features.

    ``label_default`` is the label. ``delinquency_status`` is derived from
    ``current_status`` (the outcome), so it leaks the answer. ``loan_id`` is
    an identifier, ``as_of_date`` is metadata. None of these appear in
    FEATURE_COLUMNS; all are named in LEAKAGE_EXCLUDED.
    """
    forbidden = {"label_default", "delinquency_status", "current_status", "loan_id", "as_of_date"}
    used = set(FEATURE_COLUMNS)
    assert used.isdisjoint(forbidden), (
        f"leakage: {used & forbidden} are outcome/identifier fields used as features"
    )
    # And the exclusion list documents the rule explicitly.
    assert set(LEAKAGE_EXCLUDED) >= {"label_default", "delinquency_status", "loan_id", "as_of_date"}


# --------------------------------------------------------------------------- #
# 4. AUC > 0.5 on a separable toy set.
# --------------------------------------------------------------------------- #
def test_auc_above_chance_on_separable_toy(separable_frame):
    """Model beats random (AUC > 0.5) on the held-out newer vintages."""
    model = train(separable_frame, train_fraction=0.6)
    auc = model["metrics"].get("auc")
    assert auc is not None, "AUC should be computable (both classes in test)"
    assert auc > 0.5, f"model no better than chance: AUC={auc:.3f}"


def test_model_artifact_carries_split_and_feature_metadata(separable_frame):
    """The artifact dict records what was used (audit + reproducibility)."""
    model = train(separable_frame)
    assert model["feature_columns"] == FEATURE_COLUMNS
    assert "split" in model and "metrics" in model
    assert model["leakage_excluded"] == list(LEAKAGE_EXCLUDED)


# --------------------------------------------------------------------------- #
# 5. Persistence — save/load round-trips a trained model.
# --------------------------------------------------------------------------- #
def test_save_load_roundtrip(tmp_path, tiny_frame):
    from waspada.model.risk import load_model, save_model

    model = train(tiny_frame)
    p = save_model(model, tmp_path / "m.pkl")
    loaded = load_model(p)
    scored_a = predict(model, tiny_frame)
    scored_b = predict(loaded, tiny_frame)
    assert scored_a.column("p_default").to_pylist() == scored_b.column("p_default").to_pylist()
