"""WA-093 — PD model monitoring & drift observability.

The PD model is fit fresh every run, so the classic *stale-model* drift can't
happen. The real blind spots with a shifting book are (a) **covariate shift**
(handled by serving absolute bands — WA-051/WA-094) and (b) **no cross-run
metric**. This module supplies the second: a compact, JSON-serialisable
per-run **monitoring record** plus a **Population Stability Index** (PSI) to
quantify feature drift against a reference cohort.

Everything here is pure (Arrow/NumPy in, dict out) and network-free — the
orchestrator/insight layer decides where to persist the record (OSS Gold
sibling, SLS). Absent a reference cohort, the record still carries the run
metrics; PSI is simply omitted (drift needs two points to compare).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import numpy as np
import pyarrow as pa

from ..schema import RISK_LEVELS
from .risk import CATEGORICAL_FEATURES, FEATURE_COLUMNS, NUMERIC_FEATURES

__all__ = [
    "population_stability_index",
    "categorical_psi",
    "feature_psi",
    "build_monitor_record",
    "PSI_MODERATE",
    "PSI_SIGNIFICANT",
]

# Industry-standard PSI thresholds: < 0.1 stable, 0.1–0.25 moderate shift,
# > 0.25 significant shift. We flag at 0.2 (worth a look) and 0.25 (act).
PSI_MODERATE = 0.2
PSI_SIGNIFICANT = 0.25

_EPS = 1e-6


def population_stability_index(
    reference: Sequence[float], current: Sequence[float], *, bins: int = 10
) -> float:
    """PSI between a numeric ``reference`` and ``current`` distribution.

    ``PSI = Σ (cur% − ref%) · ln(cur% / ref%)`` over equal-frequency bins whose
    edges come from the reference's quantiles (so the reference is, by
    construction, uniform across bins). Non-finite values are dropped; empty or
    degenerate inputs return ``0.0`` (no measurable drift).
    """
    ref = np.asarray(reference, dtype=float)
    cur = np.asarray(current, dtype=float)
    ref = ref[np.isfinite(ref)]
    cur = cur[np.isfinite(cur)]
    if ref.size == 0 or cur.size == 0:
        return 0.0

    edges = np.unique(np.percentile(ref, np.linspace(0, 100, bins + 1)))
    if edges.size < 2:
        return 0.0  # constant reference — nothing to bin against
    edges = edges.astype(float)
    edges[0], edges[-1] = -np.inf, np.inf

    ref_frac = np.clip(np.histogram(ref, bins=edges)[0] / ref.size, _EPS, None)
    cur_frac = np.clip(np.histogram(cur, bins=edges)[0] / cur.size, _EPS, None)
    return float(np.sum((cur_frac - ref_frac) * np.log(cur_frac / ref_frac)))


def categorical_psi(reference: Sequence, current: Sequence) -> float:
    """PSI over category frequencies (for one-hot / string features).

    Same formula as :func:`population_stability_index` but the "bins" are the
    union of categories seen in either distribution.
    """
    ref = [x for x in reference if x is not None]
    cur = [x for x in current if x is not None]
    if not ref or not cur:
        return 0.0
    cats = sorted(set(ref) | set(cur))
    n_ref, n_cur = len(ref), len(cur)
    from collections import Counter

    rc, cc = Counter(ref), Counter(cur)
    psi = 0.0
    for c in cats:
        r = max(rc.get(c, 0) / n_ref, _EPS)
        u = max(cc.get(c, 0) / n_cur, _EPS)
        psi += (u - r) * np.log(u / r)
    return float(psi)


def feature_psi(
    reference: pa.Table, current: pa.Table, *, columns: Optional[Sequence[str]] = None
) -> Dict[str, float]:
    """PSI per model feature between a reference and current FeatureFrame.

    Numeric features (``NUMERIC_FEATURES``) use binned PSI; categoricals
    (``CATEGORICAL_FEATURES``) use category-frequency PSI. Columns absent from
    either table are skipped. Returns ``{feature: psi}`` (empty if no overlap).
    """
    cols = list(columns) if columns is not None else list(FEATURE_COLUMNS)
    ref_names = set(reference.column_names)
    cur_names = set(current.column_names)
    out: Dict[str, float] = {}
    for col in cols:
        if col not in ref_names or col not in cur_names:
            continue
        ref_vals = reference.column(col).to_pylist()
        cur_vals = current.column(col).to_pylist()
        if col in NUMERIC_FEATURES:
            out[col] = population_stability_index(ref_vals, cur_vals)
        elif col in CATEGORICAL_FEATURES:
            out[col] = categorical_psi(ref_vals, cur_vals)
    return out


def _band_distribution(scored: pa.Table) -> Dict[str, float]:
    """Share of accounts per RISK_LEVEL (the served band mix)."""
    try:
        bands = scored.column("score_band").to_pylist()
    except (KeyError, ValueError, pa.ArrowInvalid):
        return {}
    n = len(bands)
    if n == 0:
        return {}
    from collections import Counter

    counts = Counter(str(b) for b in bands)
    return {level: round(counts.get(level, 0) / n, 4) for level in RISK_LEVELS}


def _observed_default_rate(scored: pa.Table, features: Optional[pa.Table]) -> Optional[float]:
    """Mean ``label_default`` — the realised default rate of the scored cohort
    (a monitoring metric, never a feature). Read from ``scored`` (carried
    forward by predict) or the feature frame; ``None`` when unavailable."""
    for tbl in (scored, features):
        if tbl is None:
            continue
        try:
            vals = tbl.column("label_default").to_pylist()
        except (KeyError, ValueError, pa.ArrowInvalid):
            continue
        flags = [bool(v) for v in vals if v is not None]
        if flags:
            return round(sum(flags) / len(flags), 4)
    return None


def build_monitor_record(
    model: Dict,
    scored: pa.Table,
    features: Optional[pa.Table] = None,
    *,
    reference: Optional[pa.Table] = None,
) -> Dict[str, object]:
    """Assemble the per-run monitoring record (JSON-serialisable).

    Carries the model's own metrics (AUC + Brier raw/calibrated from WA-094),
    the observed default rate, train/test sizes + split method, the served band
    distribution, and — when a ``reference`` FeatureFrame is supplied — per-feature
    PSI with drift flags. Never raises: a missing piece is simply omitted.
    """
    metrics = model.get("metrics", {}) if isinstance(model, dict) else {}
    split = model.get("split", {}) if isinstance(model, dict) else {}

    record: Dict[str, object] = {
        "model_id": model.get("model_id") if isinstance(model, dict) else None,  # WA-082 lineage
        "auc": metrics.get("auc"),
        "brier_raw": metrics.get("brier_raw"),
        "brier_calibrated": metrics.get("brier_calibrated"),
        "calibrated": bool(metrics.get("calibrated", False)),
        "n_train": metrics.get("n_train"),
        "n_test": metrics.get("n_test"),
        "split_method": split.get("method"),
        "n_scored": scored.num_rows,
        "observed_default_rate": _observed_default_rate(scored, features),
        "band_distribution": _band_distribution(scored),
        "trained_at": model.get("trained_at") if isinstance(model, dict) else None,
    }

    # PSI vs the reference cohort (drift). Needs both the reference and the
    # current feature frame; absent either, drift is simply not measured.
    if reference is not None and features is not None:
        psi = feature_psi(reference, features)
        if psi:
            record["psi"] = {k: round(v, 4) for k, v in psi.items()}
            record["drift_flags"] = sorted(
                [k for k, v in psi.items() if v > PSI_MODERATE]
            )
            record["drift_significant"] = sorted(
                [k for k, v in psi.items() if v > PSI_SIGNIFICANT]
            )
            record["max_psi"] = round(max(psi.values()), 4)

    return record
