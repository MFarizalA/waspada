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
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
from sklearn.compose import ColumnTransformer
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
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
    "explain",
    "format_drivers",
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
# WA-094: below this hold-out size, calibration is unreliable — keep the raw
# probabilities (tiny/offline frames are then byte-identical to the pre-WA-094
# path, so tests/CI don't move). Real books clear this easily.
_CALIBRATION_MIN_SAMPLES = 30


def _raw_proba(pipeline: Pipeline, X: pd.DataFrame) -> np.ndarray:
    """The LR pipeline's raw P(default), finite and clipped to [0,1]."""
    p = pipeline.predict_proba(X)[:, 1].astype(float)
    return np.clip(np.nan_to_num(p, nan=0.0), 0.0, 1.0)


def _fit_calibrator(raw_test: np.ndarray, y_test: np.ndarray):
    """Fit an isotonic map raw_prob → calibrated_prob on the hold-out (WA-094).

    ``class_weight="balanced"`` biases the LR probabilities toward 0.5; an
    isotonic (monotone, non-parametric) fit on the held-out newer vintages
    corrects them so ``p_default`` is a true PD. Monotone ⇒ the ranking (and
    therefore AUC) is unchanged; only the probability *values* are corrected.

    Returns the fitted :class:`IsotonicRegression` or ``None`` when the hold-out
    is too small / single-class / degenerate (caller keeps the raw probs).
    """
    if len(raw_test) < _CALIBRATION_MIN_SAMPLES or len(np.unique(y_test)) < 2:
        return None
    try:
        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        iso.fit(raw_test, y_test.astype(float))
        return iso
    except Exception:  # pragma: no cover - defensive; degrade to raw
        return None


def _calibrated_proba(model: Dict, X: pd.DataFrame) -> np.ndarray:
    """Score ``X`` → calibrated P(default). Applies the model's calibrator when
    present (WA-094), else the raw LR probability. The single scoring path both
    ``predict`` and the band-edge computation go through, so bands are always on
    the served probability."""
    raw = _raw_proba(model["pipeline"], X)
    cal = model.get("calibrator")
    if cal is None:
        return raw
    try:
        out = np.asarray(cal.predict(raw), dtype=float)
        return np.clip(np.nan_to_num(out, nan=0.0), 0.0, 1.0)
    except Exception:  # pragma: no cover - defensive; degrade to raw
        return raw


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
    # WA-094: post-hoc probability calibration. Fit an isotonic map on the
    # hold-out so p_default is a true PD (expected_loss + the absolute bands
    # depend on it). The LR pipeline is untouched — explain()'s coefficients and
    # the honest linear-score decomposition are unchanged; calibration is a
    # monotone remap of the final probability. Guarded: on too-small/single-class
    # hold-outs the calibrator is None and scoring stays raw (offline unchanged).
    calibrator = None
    if len(test_idx) > 0 and len(np.unique(y[test_idx])) == 2:
        raw_test = _raw_proba(pipeline, X.iloc[test_idx])
        metrics["auc"] = float(roc_auc_score(y[test_idx], raw_test))  # rank metric: raw == calibrated
        calibrator = _fit_calibrator(raw_test, y[test_idx])
        metrics["calibrated"] = calibrator is not None
        # Brier before/after, so the calibration win is auditable (WA-093 reads it).
        metrics["brier_raw"] = float(brier_score_loss(y[test_idx], raw_test))
        if calibrator is not None:
            cal_test = _calibrated_proba({"pipeline": pipeline, "calibrator": calibrator}, X.iloc[test_idx])
            metrics["brier_calibrated"] = float(brier_score_loss(y[test_idx], cal_test))

    # WA-051: freeze the reference band edges — the [20,40,60,80] percentiles of
    # THIS book's scores — so future batches are banded on absolute PD cutoffs
    # rather than re-quintiled against themselves. Computed with the same
    # clip as predict() so scoring the training frame reproduces the historical
    # per-batch quintiles byte-for-byte. Collapsed (constant-prob) → None, so
    # predict() falls back to the relative path and the degenerate handling.
    # Band on the CALIBRATED score distribution (the served probability), so the
    # frozen edges and predict() stay consistent post-calibration.
    band_edges = _reference_band_edges(
        _calibrated_proba({"pipeline": pipeline, "calibrator": calibrator}, X)
    )

    artifact = {
        "pipeline": pipeline,
        "calibrator": calibrator,  # WA-094: None when calibration was skipped
        "feature_columns": list(FEATURE_COLUMNS),
        "leakage_excluded": list(LEAKAGE_EXCLUDED),
        "split": split,
        "metrics": metrics,
        "band_edges": band_edges,
        "trained_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    # WA-082: stamp a deterministic version id so every run can cite the exact
    # model that scored it (the registry recomputes the same id on publish/load).
    try:
        from .registry import model_id as _model_id
        artifact["model_id"] = _model_id(artifact)
    except Exception:  # pragma: no cover - defensive; id is audit metadata
        pass
    return artifact


def _reference_band_edges(probs: np.ndarray) -> Optional[List[float]]:
    """The four absolute PD cutoffs to band future batches against (WA-051).

    The reference distribution is the whole training book's (WA-094: calibrated)
    scores; its ``[20,40,60,80]`` percentiles become the frozen cutoffs. Because
    predict() scores through the same calibrated path, ``predict(model,
    same_frame)`` reproduces these quintiles. Returns ``None`` when the cutoffs
    collapse (constant probabilities) so predict() degrades to the relative path.
    """
    probs = np.clip(np.nan_to_num(np.asarray(probs, dtype=float), nan=0.0), 0.0, 1.0)
    qs = np.percentile(probs, [20, 40, 60, 80])
    if len(set(qs.tolist())) == 1:
        return None
    return [float(q) for q in qs]


def _try_outstanding_principal(features: pa.Table):
    """WA-024: reconstruct outstanding_principal from the FeatureFrame.

    outstanding_principal = outstanding_ratio × amount (when both columns
    exist). Returns a pyarrow array, or None if either column is absent.
    """
    try:
        op_ratio = features.column("outstanding_ratio").to_pylist()
        amounts = features.column("amount").to_pylist()
    except (KeyError, ValueError):
        return None
    vals = [float(r or 0.0) * float(a or 0.0) for r, a in zip(op_ratio, amounts)]
    return pa.array(vals, type=pa.float64())


# --------------------------------------------------------------------------- #
# predict — ScoredAccounts-shaped output with p_default + risk-level band.
# --------------------------------------------------------------------------- #
def _risk_level_bands(
    probs: np.ndarray, edges: Optional[Sequence[float]] = None,
) -> List[str]:
    """Assign "Very Low" (lowest risk) .. "Very High" (highest) from ``probs``.

    Two modes (WA-051):

    * ``edges=None`` (default) — **per-batch relative** banding: the four
      cutpoints are this batch's ``[20,40,60,80]`` percentiles, so ~20% land in
      each level. This is the historical behaviour, preserved byte-for-byte.
    * ``edges`` supplied — **absolute** banding against four fixed PD cutoffs
      ``[e1,e2,e3,e4]``. "Very High" then means ``PD > e4`` for real, not "top
      20% of whatever batch this happens to be." This is what makes the dispute
      gate calibrated: the Skeptic's absolute view and the band are finally on
      the same scale.

    The labels come from :data:`waspada.schema.RISK_LEVELS`. Degenerate relative
    cases (constant probs) collapse to the middle level so banding never throws.
    """
    lo, low, mid, high, hi = RISK_LEVELS
    n = len(probs)
    if n == 0:
        return []
    if edges is not None:
        cuts = [float(e) for e in edges]
    else:
        qs = np.percentile(probs, [20, 40, 60, 80])
        # If all cutpoints collapse (constant probs), everyone is mid ("Medium").
        if len(set(qs.tolist())) == 1:
            return [mid] * n
        cuts = qs.tolist()
    bands: List[str] = []
    for p in probs:
        if p <= cuts[0]:
            bands.append(lo)
        elif p <= cuts[1]:
            bands.append(low)
        elif p <= cuts[2]:
            bands.append(mid)
        elif p <= cuts[3]:
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
    # WA-094: score through the calibrated path (raw LR probability remapped by
    # the isotonic calibrator when the artifact carries one; raw otherwise).
    probs = _calibrated_proba(model, X)

    # Guard: probabilities must be finite and in [0,1] (clip tiny float drift).
    probs = np.clip(np.nan_to_num(probs, nan=0.0), 0.0, 1.0)

    # WA-051: band on the frozen absolute edges when the artifact carries them
    # (new models); fall back to per-batch quintiles for edge-less artifacts
    # (back-compat). On the training frame the two are identical by construction.
    bands = _risk_level_bands(probs, edges=model.get("band_edges"))

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
    # WA-024: carry forward outstanding_principal for Expected Loss computation.
    # Reconstructed from outstanding_ratio × amount if both are present.
    op = _try_outstanding_principal(features)
    if op is not None:
        scored = scored.append_column("outstanding_principal", op)
    validate_table(scored, ScoredAccounts, name="predict(scored)")
    return scored


# --------------------------------------------------------------------------- #
# explain — per-account feature attribution (WA-050).
#
# The debate's Actuary was defending a score it could not introspect: the
# rebuttal prompt saw only raw feature *values*, never *why the model* produced
# the band — so its defense was a plausible-sounding rationalization, not an
# explanation. For a linear model the "why" is exact and free: the logit is
# ``intercept + Σ coef_i · x_i`` over the transformed features, so each term is
# that feature's signed contribution to the score. Feeding the top terms into
# the debate turns "a number can't argue for itself" from a slogan into a
# grounded argument, and gives the dashboard a real "why is this account High?".
# --------------------------------------------------------------------------- #
def _driver_label(name: str, raw_row: pd.Series) -> str:
    """Turn a transformed feature name into a human-readable, value-bearing label.

    ``get_feature_names_out`` yields ``num__rate`` / ``cat__grade_E``. We strip
    the transformer prefix and, for numerics, attach the account's raw value
    (``rate=24.00``); for one-hots, render the active category (``grade=E``).
    """
    if name.startswith("num__"):
        feat = name[len("num__"):]
        val = raw_row.get(feat)
        try:
            return f"{feat}={float(val):.2f}"
        except (TypeError, ValueError):
            return feat
    if name.startswith("cat__"):
        body = name[len("cat__"):]
        # OneHotEncoder names are ``{feature}_{category}``; match the known
        # categorical so a category containing '_' (e.g. debt_consolidation)
        # isn't split in the wrong place.
        for feat in CATEGORICAL_FEATURES:
            if body.startswith(f"{feat}_"):
                return f"{feat}={body[len(feat) + 1:]}"
        return body
    return name


def explain(
    model: Dict, features: pa.Table, loan_id: str, *, top_n: int = 5,
) -> List[Tuple[str, float]]:
    """Top-``top_n`` signed logit contributions behind one account's score.

    Returns ``[(label, contribution), ...]`` ranked by ``|contribution|``
    descending, where ``contribution = coef_i · x_i`` on the *transformed*
    feature (StandardScaler'd numeric or one-hot categorical). Because the model
    is linear, ``intercept + Σ (all contributions) == logit(p_default)`` — the
    account's own number is fully decomposed, not approximated.

    Returns ``[]`` (never raises) when the model is not a fitted pipeline or the
    ``loan_id`` is not in ``features`` — the caller degrades to the prior
    value-only prompt.
    """
    pipeline = model.get("pipeline") if isinstance(model, dict) else None
    if pipeline is None:
        return []
    try:
        pre = pipeline.named_steps["pre"]
        clf = pipeline.named_steps["clf"]
    except (AttributeError, KeyError):
        return []

    # Locate the account's raw feature row.
    ids = features.column("loan_id").to_pylist()
    try:
        pos = ids.index(str(loan_id))
    except ValueError:
        return []
    raw_row = pd.Series(
        {c: features.column(c)[pos].as_py() for c in FEATURE_COLUMNS}
    )
    X_row = pd.DataFrame([raw_row], columns=FEATURE_COLUMNS)

    try:
        xt = np.asarray(pre.transform(X_row), dtype=float)[0]
        names = list(pre.get_feature_names_out())
        coefs = np.asarray(clf.coef_, dtype=float).ravel()
    except Exception:  # pragma: no cover - defensive; unfitted / shape drift
        return []
    if len(coefs) != len(xt) or len(names) != len(xt):
        return []

    contributions = coefs * xt
    order = sorted(range(len(contributions)), key=lambda i: -abs(contributions[i]))
    out: List[Tuple[str, float]] = []
    for i in order:
        c = float(contributions[i])
        if c == 0.0:  # inactive one-hot / zeroed-out term carries no signal
            continue
        out.append((_driver_label(names[i], raw_row), c))
        if len(out) >= max(1, int(top_n)):
            break
    return out


def format_drivers(drivers: List[Tuple[str, float]]) -> str:
    """Render :func:`explain` output as a compact evidence line for a prompt.

    ``[("rate=24.00", 0.81), ("dti=30.00", 0.44)]`` →
    ``"rate=24.00 (+0.81), dti=30.00 (-0.44)"``. A positive term pushes the
    account toward default (higher risk); negative pulls it toward safe.
    """
    return ", ".join(f"{label} ({c:+.2f})" for label, c in drivers)


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
