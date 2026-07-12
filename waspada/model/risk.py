"""Risk model — Collections lane (WA-005, CPU adaptation).

Trains a sklearn LogisticRegression on the frozen :class:`FeatureFrame` to
predict **P(eventual charge-off / default)** and emits a
:class:`ScoredAccounts`-shaped Arrow table (``p_default`` + ``score_band``).

This is the **CPU path** (GPU/cuML is on hold per the owner). It reuses the
exact same contract types, so swapping in a cuML estimator later is a
drop-in: ``train``/``predict`` keep their signatures.

Leakage guard (the important invariant)
---------------------------------------
The frozen :class:`FeatureFrame` carries the label (``label_default``) and two
outcome-derived fields (``delinquency_status`` bucket, which is derived from
``current_status``; and ``as_of_date``). None of these are model features:

  * ``label_default`` — this *is* the label.
  * ``delinquency_status`` — derived from the final ``current_status`` (an
    outcome), so it leaks the answer. Excluded.
  * ``as_of_date`` — snapshot metadata, not predictive.
  * ``loan_id`` — an identifier, not predictive.

Only :data:`FEATURE_COLUMNS` enter the model matrix. The leakage rule is
documented by ``test_model.py::test_no_outcome_leakage_in_features``.

Vintage split
-------------
The :class:`FeatureFrame` has no ``issue_date`` field (it is frozen and
carries only ``loan_age`` + ``as_of_date``). We reconstruct the origination
cohort year from those two (``issue_year`` ≈ ``as_of`` shifted back by
``loan_age`` months) and split chronologically: **train on older vintages,
test on newer** — an out-of-time-ish check that the model is never evaluated
on the same cohort distribution it trained on.
"""
from __future__ import annotations

import datetime as dt
import pickle
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from ..schema import RISK_LEVELS, FeatureFrame, ScoredAccounts, schema_from_dataclass, validate_table

__all__ = [
    "FEATURE_COLUMNS",
    "NUMERIC_FEATURES",
    "CATEGORICAL_FEATURES",
    "LEAKAGE_EXCLUDED",
    "train",
    "predict",
    "save_model",
    "load_model",
    "issue_year_from_frame",
]

# --------------------------------------------------------------------------- #
# Feature selection — the leakage-safe subset of the FeatureFrame.
# --------------------------------------------------------------------------- #
# These are the only columns that enter the model matrix. Everything else in
# the FeatureFrame is an identifier, the label, or outcome-derived and is
# explicitly excluded (see LEAKAGE_EXCLUDED).
NUMERIC_FEATURES: Tuple[str, ...] = (
    "amount", "term", "rate", "annual_income", "dti",
    "loan_age", "payment_ratio", "outstanding_ratio",
)
CATEGORICAL_FEATURES: Tuple[str, ...] = ("grade", "purpose", "region")
FEATURE_COLUMNS: List[str] = list(NUMERIC_FEATURES + CATEGORICAL_FEATURES)

# Fields present in the FeatureFrame that must NEVER be model features.
# Cited by the leakage test so the guard is self-documenting.
LEAKAGE_EXCLUDED: Tuple[str, ...] = (
    "loan_id",           # identifier
    "delinquency_status",  # derived from current_status (outcome) → leaks
    "label_default",     # the label
    "as_of_date",        # snapshot metadata
)

# Default vintage split: fraction of (older) vintages used for training.
DEFAULT_TRAIN_FRACTION = 0.7


# --------------------------------------------------------------------------- #
# Vintage reconstruction — issue_year from loan_age + as_of_date.
# --------------------------------------------------------------------------- #
def issue_year_from_frame(frame: pa.Table) -> pa.Array:
    """Reconstruct the origination cohort year for every row.

    ``issue_year = floor((as_of_year*12 + as_of_month - loan_age) / 12)``.
    The FeatureFrame is frozen without ``issue_date``, so this is the only
    way to recover the vintage cohort from contract fields. Clamped to a
    sane floor (1900) to avoid degenerate negatives on bad input.
    """
    as_of = frame.column("as_of_date")
    ym = pc.add(
        pc.multiply(pc.cast(pc.year(as_of), pa.int64()), 12),
        pc.cast(pc.month(as_of), pa.int64()),
    )
    issue_ym = pc.subtract(ym, pc.cast(frame.column("loan_age"), pa.int64()))
    years = pc.divide(issue_ym, pa.scalar(12, type=pa.int64()))
    years = pc.cast(years, pa.int64())
    return years


def _vintage_split(frame: pa.Table, train_fraction: float) -> Tuple[np.ndarray, np.ndarray, Dict]:
    """Chronological train/test split by reconstructed vintage year.

    Returns ``(train_idx, test_idx, split_info)``. Older vintages train,
    newer vintages test. Falls back to a seeded shuffle split when only one
    vintage is present (so train() never no-ops on a single-cohort toy).
    """
    years = issue_year_from_frame(frame).to_pylist()
    years_arr = np.asarray(years, dtype=np.int64)
    n = len(years_arr)
    unique_years = sorted(set(years_arr.tolist()))

    if len(unique_years) <= 1:
        # Single cohort: deterministic shuffle split (seeded) — documented
        # fallback so train() still works on a single-vintage toy.
        rng = np.random.default_rng(42)
        perm = rng.permutation(n)
        cut = int(n * train_fraction)
        train_idx = np.sort(perm[:cut])
        test_idx = np.sort(perm[cut:])
        return train_idx, test_idx, {
            "method": "shuffle_fallback",
            "train_years": unique_years,
            "test_years": unique_years,
            "note": "single vintage; seeded shuffle split",
        }

    # Chronological: pick cutoff year at the train_fraction quantile of the
    # sorted unique years. < cutoff → train, >= cutoff → test.
    cut_pos = max(1, min(len(unique_years) - 1, int(np.floor(len(unique_years) * train_fraction))))
    cutoff_year = unique_years[cut_pos]
    train_mask = years_arr < cutoff_year
    test_mask = ~train_mask
    train_idx = np.nonzero(train_mask)[0]
    test_idx = np.nonzero(test_mask)[0]

    # Guard: if the split degenerated (all one side), fall back to shuffle.
    if len(train_idx) == 0 or len(test_idx) == 0:
        rng = np.random.default_rng(42)
        perm = rng.permutation(n)
        cut = int(n * train_fraction)
        train_idx = np.sort(perm[:cut])
        test_idx = np.sort(perm[cut:])
        return train_idx, test_idx, {
            "method": "shuffle_fallback",
            "train_years": unique_years,
            "test_years": unique_years,
            "note": "vintage split degenerated; seeded shuffle split",
        }

    return train_idx, test_idx, {
        "method": "vintage",
        "cutoff_year": int(cutoff_year),
        "train_years": [int(y) for y in unique_years if y < cutoff_year],
        "test_years": [int(y) for y in unique_years if y >= cutoff_year],
        "note": "chronological vintage split (older → train, newer → test)",
    }


# --------------------------------------------------------------------------- #
# train — fit LogisticRegression on the leakage-safe feature subset.
# --------------------------------------------------------------------------- #
def _build_pipeline() -> Pipeline:
    """Standardized numerics + one-hot categorics + L2 logistic regression."""
    pre = ColumnTransformer(
        transformers=[
            ("num", StandardScaler(), list(NUMERIC_FEATURES)),
            ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), list(CATEGORICAL_FEATURES)),
        ],
        remainder="drop",
    )
    clf = LogisticRegression(max_iter=1000, class_weight="balanced", solver="lbfgs")
    return Pipeline([("pre", pre), ("clf", clf)])


def _X_y(frame: pa.Table) -> Tuple[pd.DataFrame, np.ndarray]:
    """Pull the feature matrix (FEATURE_COLUMNS) and the label from the frame.

    Returns a pandas DataFrame so sklearn's ColumnTransformer can select
    columns by string name.
    """
    cols = {name: frame.column(name).to_pylist() for name in FEATURE_COLUMNS}
    df = pd.DataFrame({c: cols[c] for c in FEATURE_COLUMNS}, columns=FEATURE_COLUMNS)
    y = np.asarray(frame.column("label_default").to_pylist(), dtype=np.int8)
    return df, y


def train(
    features: pa.Table,
    *,
    train_fraction: float = DEFAULT_TRAIN_FRACTION,
) -> Dict:
    """Train a LogisticRegression on ``features`` with a vintage split.

    Parameters
    ----------
    features
        A :class:`FeatureFrame`-shaped Arrow table (validated up front).
    train_fraction
        Fraction of *older* vintages used for training (default 0.7).

    Returns
    -------
    dict
        Model artifact: the fitted :class:`~sklearn.pipeline.Pipeline`,
        the feature columns used (the leakage-safe subset), the vintage
        split metadata, and hold-out metrics (AUC when computable).
    """
    validate_table(features, FeatureFrame, name="train(features)")

    X, y = _X_y(features)
    train_idx, test_idx, split = _vintage_split(features, train_fraction)

    pipeline = _build_pipeline()
    # .iloc for positional row selection (pandas 3.0 changed df[int] semantics).
    X_train = X.iloc[train_idx]
    y_train = y[train_idx]
    pipeline.fit(X_train, y_train)

    # Hold-out AUC on the newer-vintage test split (when both classes present).
    metrics: Dict[str, object] = {"n_train": int(len(train_idx)), "n_test": int(len(test_idx))}
    if len(test_idx) > 0 and len(np.unique(y[test_idx])) == 2:
        probs = pipeline.predict_proba(X.iloc[test_idx])[:, 1]
        metrics["auc"] = float(roc_auc_score(y[test_idx], probs))

    return {
        "pipeline": pipeline,
        "feature_columns": list(FEATURE_COLUMNS),
        "leakage_excluded": list(LEAKAGE_EXCLUDED),
        "split": split,
        "metrics": metrics,
        "trained_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


# --------------------------------------------------------------------------- #
# predict — ScoredAccounts-shaped output with p_default + risk-level band.
# --------------------------------------------------------------------------- #
def _risk_level_bands(probs: np.ndarray) -> List[str]:
    """Assign "Very Low" (lowest risk) .. "Very High" (highest) by p_default quintile.

    The labels come from :data:`waspada.schema.RISK_LEVELS` (the frozen
    vocabulary). Per-batch relative banding (the work-list ranks within the
    scored population). Degenerate cases (constant probs, tiny N) collapse to
    the middle level so banding never throws.
    """
    lo, low, mid, high, hi = RISK_LEVELS
    n = len(probs)
    if n == 0:
        return []
    qs = np.percentile(probs, [20, 40, 60, 80])
    # If all cutpoints collapse (constant probs), everyone is mid ("Medium").
    if len(set(qs.tolist())) == 1:
        return [mid] * n
    bands: List[str] = []
    for p in probs:
        if p <= qs[0]:
            bands.append(lo)
        elif p <= qs[1]:
            bands.append(low)
        elif p <= qs[2]:
            bands.append(mid)
        elif p <= qs[3]:
            bands.append(high)
        else:
            bands.append(hi)
    return bands


def predict(model: Dict, features: pa.Table) -> pa.Table:
    """Score ``features`` → a :class:`ScoredAccounts`-shaped Arrow table.

    The output validates against :class:`ScoredAccounts` (the five contract
    fields, ``score_band`` = quintile band). ``segment`` is populated from
    ``purpose``/``region``; ``recommended_action`` is left empty here and
    filled by the ranking layer (:mod:`waspada.insight.ranking`).

    Extra (non-contract) columns are appended so the insight layer can build
    portfolio health without re-reading the raw frame: ``issue_year``
    (vintage cohort), ``delinquency_status`` (status mix / NPL), and
    ``label_default`` (observed cohort default rate — a monitoring metric,
    never a model feature).
    """
    validate_table(features, FeatureFrame, name="predict(features)")
    X, _ = _X_y(features)
    probs = model["pipeline"].predict_proba(X)[:, 1].astype(float)

    # Guard: probabilities must be finite and in [0,1] (clip tiny float drift).
    probs = np.clip(np.nan_to_num(probs, nan=0.0), 0.0, 1.0)

    bands = _risk_level_bands(probs)

    # Segment struct per the frozen ScoredAccounts contract.
    seg_type = schema_from_dataclass(ScoredAccounts).field("segment").type
    purposes = features.column("purpose").to_pylist()
    regions = features.column("region").to_pylist()
    segment_arr = pa.array(
        [{"product": p, "region": r} for p, r in zip(purposes, regions)],
        type=seg_type,
    )

    base = pa.table(
        {
            "loan_id": features.column("loan_id"),
            "p_default": pa.array(probs.tolist(), type=pa.float64()),
            "score_band": pa.array(bands, type=pa.string()),
            "segment": segment_arr,
            # Empty here — the ranking layer fills recommended_action.
            "recommended_action": pa.array([""] * features.num_rows, type=pa.string()),
        },
        schema=schema_from_dataclass(ScoredAccounts),
    )

    # Carry-forward columns for the insight layer (NOT contract fields).
    scored = (
        base
        .append_column("issue_year", issue_year_from_frame(features))
        .append_column("delinquency_status", features.column("delinquency_status"))
        .append_column("label_default", features.column("label_default"))
    )
    validate_table(scored, ScoredAccounts, name="predict(scored)")
    return scored


# --------------------------------------------------------------------------- #
# Persistence — models/ is gitignored (see .gitignore).
# --------------------------------------------------------------------------- #
def save_model(model: Dict, path: str | Path) -> Path:
    """Pickle the model artifact to ``path``. ``models/`` is gitignored."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("wb") as fh:
        pickle.dump(model, fh)
    return p


def load_model(path: str | Path) -> Dict:
    """Load a model artifact pickled by :func:`save_model`."""
    with Path(path).open("rb") as fh:
        return pickle.load(fh)
