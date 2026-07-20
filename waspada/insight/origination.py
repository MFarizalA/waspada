"""Origination decision layer (WA-037) — approve/refer/reject + health + alerts.

Parallel to :mod:`waspada.insight.ranking` (whose action semantics are
collections-only). Same skeleton: a deterministic decision matrix over the
model's risk bands, portfolio-level health, threshold alerts, and the same
``DashboardPayload`` shape (lane-appropriate work_list/health keys) so the
frontend contract holds.

The debate reuses as-is: the Skeptic contests the riskiest decisions, and the
society's ruling lands as the additive ``final_band`` column — the decision is
derived from *that* when present (same WA-048 discipline as collections), so an
overridden application actually changes its approve/refer/reject outcome.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import pyarrow as pa

from ..schema import ORIGINATION_ACTIONS, RISK_LEVELS, OriginationHealth, ScoredApplications, validate_table
from .ranking import Alert, _safe_get

__all__ = [
    "ORIGINATION_ACTION_BY_BAND",
    "DEFAULT_PROJECTED_DEFAULT_THRESHOLD",
    "DEFAULT_APPROVAL_RATE_FLOOR",
    "decide",
    "origination_health",
    "origination_alerts",
]

# The default decision matrix — risk band → origination action. Policy-owned
# (a RiskPolicy/parameter-matrix override lands via ``action_by_band=``, the
# same override discipline as ranking.rank); this constant is the fallback.
ORIGINATION_ACTION_BY_BAND: Dict[str, str] = {
    "Very Low": "approve",
    "Low": "approve",
    "Medium": "refer",       # human review
    "High": "reject",
    "Very High": "reject",
}

# Alert thresholds (policy-ownable, mirroring ranking's DEFAULT_*_THRESHOLD).
DEFAULT_PROJECTED_DEFAULT_THRESHOLD = 0.15   # projected default rate of the approved book
DEFAULT_APPROVAL_RATE_FLOOR = 0.20           # unusually low approval rate → drift alert


def decide(
    scored: pa.Table,
    top_n: int = 50,
    *,
    action_by_band: Optional[Dict[str, str]] = None,
) -> List[Dict[str, object]]:
    """Sort by ``p_default`` desc, attach approve/refer/reject, cap at ``top_n``.

    Returns JSON-serializable dicts shaped like :class:`ScoredApplications`
    rows. Deterministic on ties by ``application_id``. WA-048 discipline: when
    the debate adjudicated (additive ``final_band``), the action derives from
    the society's band — the ruling reaches the decision, not just the
    transcript. ``p_default``/``score_band`` are never rewritten.
    """
    validate_table(scored, ScoredApplications, name="decide(scored)")
    matrix = dict(action_by_band) if action_by_band else dict(ORIGINATION_ACTION_BY_BAND)
    bad = sorted({a for a in matrix.values() if a not in ORIGINATION_ACTIONS})
    if bad:
        raise ValueError(f"decide: invalid origination action(s) {bad}; must be in {list(ORIGINATION_ACTIONS)}")

    n = scored.num_rows
    probs = scored.column("p_default").to_pylist()
    bands = scored.column("score_band").to_pylist()
    ids = scored.column("application_id").to_pylist()
    segments = scored.column("segment").to_pylist()

    fb_col = _safe_get(scored, "final_band")
    final_bands = fb_col.to_pylist() if fb_col is not None else None
    or_col = _safe_get(scored, "override_reason")
    override_reasons = or_col.to_pylist() if or_col is not None else None

    order = sorted(range(n), key=lambda i: (-probs[i], str(ids[i])))
    out: List[Dict[str, object]] = []
    for i in order[: max(0, int(top_n))]:
        band = bands[i]
        decisive = (final_bands[i] or band) if final_bands is not None else band
        seg = segments[i]
        rec: Dict[str, object] = {
            "application_id": ids[i],
            # The loan_id alias keeps the frontend (work-list, drawer, debate
            # links) lane-agnostic — same alias discipline as the backend.
            "loan_id": ids[i],
            "p_default": float(probs[i]),
            "score_band": band,
            "segment": ({"product": seg.get("product", ""), "region": seg.get("region", "")}
                        if isinstance(seg, dict) else {"product": "", "region": ""}),
            "recommended_action": matrix.get(decisive, "refer"),
        }
        if final_bands is not None:
            rec["final_band"] = decisive
            if decisive != band:
                rec["override_reason"] = (
                    override_reasons[i] if override_reasons is not None else ""
                ) or ""
        out.append(rec)
    return out


def origination_health(
    scored: pa.Table,
    *,
    action_by_band: Optional[Dict[str, str]] = None,
) -> OriginationHealth:
    """Book-level origination aggregates (approval rate, projected default, mix).

    The approval decision used here honours the same matrix (+ any adjudicated
    ``final_band``) as :func:`decide`, so the health numbers describe the
    decisions actually shipped.
    """
    validate_table(scored, ScoredApplications, name="origination_health(scored)")
    matrix = dict(action_by_band) if action_by_band else dict(ORIGINATION_ACTION_BY_BAND)

    n = scored.num_rows
    probs = scored.column("p_default").to_pylist()
    bands = scored.column("score_band").to_pylist()
    fb_col = _safe_get(scored, "final_band")
    final_bands = fb_col.to_pylist() if fb_col is not None else None
    amt_col = _safe_get(scored, "amount")
    amounts = amt_col.to_pylist() if amt_col is not None else None

    approved_idx: List[int] = []
    band_counts: Dict[str, int] = {level: 0 for level in RISK_LEVELS}
    for i in range(n):
        band = bands[i]
        decisive = (final_bands[i] or band) if final_bands is not None else band
        if band in band_counts:
            band_counts[band] += 1
        if matrix.get(decisive, "refer") == "approve":
            approved_idx.append(i)

    approval_rate = (len(approved_idx) / n) if n else 0.0
    projected = (sum(probs[i] for i in approved_idx) / len(approved_idx)) if approved_idx else 0.0
    volume = (sum(float(amounts[i] or 0.0) for i in approved_idx) if amounts is not None else 0.0)

    return {
        "approval_rate": round(approval_rate, 4),
        "projected_default_rate": round(projected, 4),
        "band_mix": {level: round(band_counts[level] / n, 4) if n else 0.0 for level in RISK_LEVELS},
        "approved_volume": round(volume, 2),
    }


def origination_alerts(
    health: OriginationHealth,
    *,
    projected_default_threshold: float = DEFAULT_PROJECTED_DEFAULT_THRESHOLD,
    approval_rate_floor: float = DEFAULT_APPROVAL_RATE_FLOOR,
) -> List[Alert]:
    """Threshold alerts over the origination book (mirrors ranking.alerts)."""
    out: List[Alert] = []
    pdr = float(health["projected_default_rate"])
    if pdr > projected_default_threshold:
        out.append({
            "metric": "projected_default_rate",
            "value": pdr,
            "threshold": projected_default_threshold,
            "message": (f"Projected default rate of the approved book is {pdr:.1%}, "
                        f"above the {projected_default_threshold:.1%} threshold."),
            "segment": None,
        })
    ar = float(health["approval_rate"])
    if ar < approval_rate_floor:
        out.append({
            "metric": "approval_rate",
            "value": ar,
            "threshold": approval_rate_floor,
            "message": (f"Approval rate is {ar:.1%}, below the {approval_rate_floor:.1%} floor — "
                        "possible band-mix drift in incoming applications."),
            "segment": None,
        })
    return out
