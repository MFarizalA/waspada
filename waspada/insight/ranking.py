"""Ranking, segmentation & the dashboard payload (WA-006).

Turns a :class:`ScoredAccounts`-shaped table into the decision-support output:

  * :func:`rank` — sort by ``p_default`` desc, attach ``recommended_action``
    by band (``call`` / ``watch`` / ``auto-cure``), cap at ``top_n``.
  * :func:`segment_health` — cross-sectional :class:`PortfolioHealth`
    aggregates (NPL ratio, vintage default rate, status mix).
  * :func:`alerts` — flag cohorts whose ``npl_ratio`` or
    ``vintage_default_rate`` breaches a configurable threshold.
  * :func:`to_dashboard_payload` — the JSON-serializable
    :class:`DashboardPayload` the frontend reads.

Pure CPU/Arrow — no GPU required here (the GPU work ends at scoring).

Data it reads
-------------
The scored table is a superset of :class:`ScoredAccounts` (the model layer in
:mod:`waspada.model.risk` appends ``issue_year``, ``delinquency_status`` and
``label_default`` so this layer can compute cohort health without re-reading
the raw frame). All three extra columns are monitoring/aggregation aids, never
model features (the leakage guard lives in :mod:`waspada.model.risk`).
"""
from __future__ import annotations

from typing import Dict, List, Optional

import pyarrow as pa
import pyarrow.compute as pc

from ..schema import DashboardPayload, PortfolioHealth, Alert, ScoredAccounts, validate_table

__all__ = [
    "ACTION_BY_BAND",
    "DEFAULT_NPL_THRESHOLD",
    "DEFAULT_VINTAGE_THRESHOLD",
    "EXPECTED_LOSS_LGD",
    "rank",
    "segment_health",
    "alerts",
    "to_dashboard_payload",
    "summarize_alerts",
]

# WA-024: Loss Given Default assumption for Expected Loss (PD × LGD × EAD).
# 45% is the Basel foundation-IRB benchmark for unsecured consumer credit —
# labeled as an assumption, not measured. EAD = outstanding_principal
# (defensible for amortizing installment loans; no revolving/undrawn component).
EXPECTED_LOSS_LGD = 0.45

# Recommended action by risk level. "Very High" → call; "Medium"/"High" →
# watch; "Very Low"/"Low" → auto-cure. Keys are the frozen
# waspada.schema.RISK_LEVELS vocabulary; values match the contract's
# call/watch/auto-cure set.
ACTION_BY_BAND: Dict[str, str] = {
    "Very High": "call",
    "High": "watch",
    "Medium": "watch",
    "Low": "auto-cure",
    "Very Low": "auto-cure",
}

# Default cohort-deterioration thresholds (configurable per call). A vintage
# cohort fires an alert when its default rate exceeds the portfolio baseline
# by this margin; the portfolio NPL ratio alerts when it crosses this level.
DEFAULT_NPL_THRESHOLD = 0.20       # 20% of book delinquent/default
DEFAULT_VINTAGE_THRESHOLD = 0.15   # 15% cohort default rate

# Statuses that count as "non-performing" for the NPL ratio: the terminal
# default set plus in-flight delinquency buckets. (True roll rates need a
# monthly panel — deferred — so this is a cross-sectional NPL proxy.)
_NPL_BUCKETS = {"Default", "31-120", "16-30"}


# --------------------------------------------------------------------------- #
# rank — ordered work-list with recommended_action by band.
# --------------------------------------------------------------------------- #
def rank(scored: pa.Table, top_n: int = 50) -> List[Dict[str, object]]:
    """Sort by ``p_default`` desc, attach ``recommended_action``, cap at ``top_n``.

    Returns a list of JSON-serializable dicts shaped like
    :class:`ScoredAccounts` rows (``loan_id``, ``p_default``, ``score_band``,
    ``segment``, ``recommended_action``). Deterministic on ties by ``loan_id``.
    """
    validate_table(scored, ScoredAccounts, name="rank(scored)")

    n = scored.num_rows
    probs = scored.column("p_default").to_pylist()
    bands = scored.column("score_band").to_pylist()
    loan_ids = scored.column("loan_id").to_pylist()
    segments = scored.column("segment").to_pylist()
    actions_in = scored.column("recommended_action").to_pylist()

    # WA-024: optional outstanding_principal for Expected Loss computation.
    op_col = _safe_get(scored, "outstanding_principal")
    outstanding = op_col.to_pylist() if op_col is not None else None

    # Sort indices by (p_default desc, loan_id asc) for determinism.
    order = sorted(range(n), key=lambda i: (-probs[i], str(loan_ids[i])))
    top = order[: max(0, int(top_n))]

    out: List[Dict[str, object]] = []
    for i in top:
        band = bands[i]
        action = ACTION_BY_BAND.get(band, actions_in[i] or "watch")
        seg = segments[i]
        rec: Dict[str, object] = {
            "loan_id": loan_ids[i],
            "p_default": float(probs[i]),
            "score_band": band,
            "segment": {"product": seg.get("product", ""), "region": seg.get("region", "")} if isinstance(seg, dict) else {"product": "", "region": ""},
            "recommended_action": action,
        }
        # WA-024: additive optional — only present when outstanding_principal exists.
        if outstanding is not None:
            rec["expected_loss"] = float(probs[i]) * EXPECTED_LOSS_LGD * float(outstanding[i] or 0.0)
        out.append(rec)
    return out


# --------------------------------------------------------------------------- #
# segment_health — cross-sectional PortfolioHealth aggregates.
# --------------------------------------------------------------------------- #
def _safe_get(scored: pa.Table, name: str) -> Optional[pa.Array]:
    """Return the column if present (extra/non-contract columns are optional)."""
    try:
        return scored.column(name)
    except (KeyError, ValueError):
        return None


def _portfolio_expected_loss(scored: pa.Table) -> Dict[str, float]:
    """WA-024: portfolio total Expected Loss (PD × LGD × EAD).

    Returns ``{"expected_loss": total}`` when ``outstanding_principal`` is
    present, or ``{}`` (omits the key entirely) when absent — so older
    payloads without it stay valid.
    """
    op_col = _safe_get(scored, "outstanding_principal")
    if op_col is None:
        return {}
    probs = scored.column("p_default").to_pylist()
    outstanding = op_col.to_pylist()
    total = sum(float(p) * EXPECTED_LOSS_LGD * float(op or 0.0)
                for p, op in zip(probs, outstanding))
    return {"expected_loss": total}


def segment_health(scored: pa.Table) -> PortfolioHealth:
    """Compute the cross-sectional :class:`PortfolioHealth` aggregates.

    * ``npl_ratio`` — fraction of accounts in a delinquent/default bucket
      (from ``delinquency_status``; falls back to 0.0 if absent).
    * ``vintage_default_rate`` — observed default rate (``label_default``)
      keyed by ``issue_year`` cohort (empty dict if either column absent).
    * ``status_mix`` — proportion of accounts per ``delinquency_status``
      bucket (empty dict if the column is absent).

    Pure Python counts over ``to_pylist()`` — the scored table is small here
    (the work-list scale), so a numpy/pandas dependency would be overkill.
    """
    validate_table(scored, ScoredAccounts, name="segment_health(scored)")
    n = scored.num_rows
    if n == 0:
        return {"npl_ratio": 0.0, "vintage_default_rate": {}, "status_mix": {}}

    delinq = _safe_get(scored, "delinquency_status")
    label = _safe_get(scored, "label_default")
    issue_year = _safe_get(scored, "issue_year")

    # NPL ratio from delinquency buckets.
    if delinq is not None:
        buckets = delinq.to_pylist()
        npl = sum(1 for b in buckets if b in _NPL_BUCKETS) / n
        status_counts: Dict[str, int] = {}
        for b in buckets:
            status_counts[b] = status_counts.get(b, 0) + 1
        status_mix = {k: v / n for k, v in status_counts.items()}
    else:
        npl = 0.0
        status_mix = {}

    # Vintage default rate (observed) by issue_year cohort.
    vintage: Dict[str, float] = {}
    if label is not None and issue_year is not None:
        years = issue_year.to_pylist()
        labels = label.to_pylist()
        cohort_total: Dict[str, int] = {}
        cohort_default: Dict[str, int] = {}
        for y, lb in zip(years, labels):
            key = str(int(y))
            cohort_total[key] = cohort_total.get(key, 0) + 1
            if lb:
                cohort_default[key] = cohort_default.get(key, 0) + 1
        vintage = {k: cohort_default.get(k, 0) / cohort_total[k] for k in cohort_total}

    return {
        "npl_ratio": float(npl),
        "vintage_default_rate": vintage,
        "status_mix": status_mix,
        **_portfolio_expected_loss(scored),
    }


# --------------------------------------------------------------------------- #
# alerts — cohort deterioration flags.
# --------------------------------------------------------------------------- #
def alerts(
    health: PortfolioHealth,
    *,
    npl_threshold: float = DEFAULT_NPL_THRESHOLD,
    vintage_threshold: float = DEFAULT_VINTAGE_THRESHOLD,
) -> List[Alert]:
    """Flag cohorts whose NPL ratio or vintage default rate breaches threshold.

    Two alert families:
      * **portfolio NPL** — fires when ``health['npl_ratio']`` ≥ ``npl_threshold``.
      * **vintage deterioration** — fires per ``issue_year`` cohort whose
        ``vintage_default_rate`` ≥ ``vintage_threshold``.
    """
    out: List[Alert] = []
    npl = float(health.get("npl_ratio", 0.0))
    if npl >= npl_threshold:
        out.append({
            "metric": "npl_ratio",
            "value": npl,
            "threshold": float(npl_threshold),
            "message": f"Portfolio NPL ratio {npl:.1%} ≥ threshold {npl_threshold:.1%}",
            "segment": None,
        })

    baseline = npl  # compare each cohort to the portfolio baseline
    for year, rate in sorted(health.get("vintage_default_rate", {}).items()):
        rate = float(rate)
        if rate >= vintage_threshold:
            out.append({
                "metric": "vintage_default_rate",
                "value": rate,
                "threshold": float(vintage_threshold),
                "message": (
                    f"Vintage {year} default rate {rate:.1%} ≥ {vintage_threshold:.1%} "
                    f"(portfolio baseline {baseline:.1%})"
                ),
                "segment": {"vintage": str(year)},
            })
    return out


def summarize_alerts(alert_list: List[Alert]) -> str:
    """One human-readable alert summary line (the insight agent surfaces this).

    Returns a plain string even when there are zero alerts (the agent always
    emits ≥1 readable line per the WA-009 acceptance).
    """
    if not alert_list:
        return "No cohort deterioration alerts: all segments within thresholds."
    lines = [a["message"] for a in alert_list]
    head = lines[0] if len(lines) == 1 else f"{len(lines)} alerts — top: {lines[0]}"
    return head


# --------------------------------------------------------------------------- #
# to_dashboard_payload — the JSON hand-off to the frontend.
# --------------------------------------------------------------------------- #
def to_dashboard_payload(
    work_list: List[Dict[str, object]],
    health: PortfolioHealth,
    alert_list: List[Alert],
) -> DashboardPayload:
    """Assemble the :class:`DashboardPayload` (JSON-serializable).

    No validation library is available offline; we assert the three required
    keys are present and the shapes are JSON-native, so a malformed payload
    fails loud rather than rendering a broken dashboard.
    """
    payload: DashboardPayload = {
        "work_list": list(work_list),
        "portfolio_health": {
            "npl_ratio": float(health["npl_ratio"]),
            "vintage_default_rate": {str(k): float(v) for k, v in health["vintage_default_rate"].items()},
            "status_mix": {str(k): float(v) for k, v in health["status_mix"].items()},
            # WA-024: forward expected_loss when present (additive optional).
            **({"expected_loss": float(health["expected_loss"])}
               if "expected_loss" in health else {}),
        },
        "alerts": list(alert_list),
    }
    # Round-trip through json to guarantee JSON-serializability up front.
    import json
    json.dumps(payload)
    return payload
