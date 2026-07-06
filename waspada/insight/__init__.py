"""Insight layer (WA-006): ranking, portfolio health, alerts, dashboard payload."""
from __future__ import annotations

from .ranking import (
    ACTION_BY_BAND,
    DEFAULT_NPL_THRESHOLD,
    DEFAULT_VINTAGE_THRESHOLD,
    alerts,
    rank,
    segment_health,
    summarize_alerts,
    to_dashboard_payload,
)

__all__ = [
    "ACTION_BY_BAND",
    "DEFAULT_NPL_THRESHOLD",
    "DEFAULT_VINTAGE_THRESHOLD",
    "rank",
    "segment_health",
    "alerts",
    "summarize_alerts",
    "to_dashboard_payload",
]
