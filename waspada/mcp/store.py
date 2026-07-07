"""Analytics data store for the MCP server (WA-015).

The single source of truth the MCP tools serve from. It holds the same
artifacts the pipeline already computed — the scored table (a superset of
:class:`~waspada.schema.ScoredAccounts`, with the monitoring columns the
insight layer added) and the feature frame (per-loan features the Skeptic
cites as evidence). Nothing is recomputed: portfolio aggregates delegate to
:func:`waspada.insight.ranking.segment_health`, so what an agent *cites* via
MCP is exactly what the pipeline *computed* and what the dashboard *shows*.

Two read-only operations back the server's two tools:

  * :meth:`AnalyticsStore.portfolio_stats` — NPL ratio, vintage default rate,
    status mix, and counts, optionally restricted to a product×region segment.
  * :meth:`AnalyticsStore.lookup_account` — the feature row for one
    ``loan_id`` (the numbers a dispute references).

The store is deliberately pure-Python over ``to_pylist()``: the MCP tool calls
are few per run (the Skeptic audits top-K), so a numpy/pandas dependency would
be overkill and would couple the server to the analytics stack.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pyarrow as pa

from ..insight.ranking import segment_health as _segment_health

__all__ = ["AnalyticsStore"]


# Delinquency buckets that count as non-performing. Kept in sync with
# :mod:`waspada.insight.ranking` (single source of truth for "what is NPL").
_NPL_BUCKETS = {"Default", "31-120", "16-30"}


class AnalyticsStore:
    """Read-only view over the scored + feature tables for the MCP tools.

    Parameters
    ----------
    scored
        The scored table the risk-model agent produced (a superset of
        :class:`~waspada.schema.ScoredAccounts` — carries ``segment``,
        ``delinquency_status``, ``issue_year``, ``label_default``).
    features
        The :class:`~waspada.schema.FeatureFrame` table analytics built (used
        for :meth:`lookup_account`). May be ``None`` when only portfolio stats
        are needed (the server accepts a stats-only configuration).
    """

    def __init__(self, scored: pa.Table, features: Optional[pa.Table] = None) -> None:
        self.scored = scored
        self.features = features
        self._loan_index: Optional[Dict[str, int]] = None

    # ----------------------------------------------------------- portfolio_stats
    def portfolio_stats(self, segment: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Portfolio-level aggregates, optionally filtered to a segment.

        Without ``segment`` this returns the whole-book view: the NPL ratio,
        vintage default rate by cohort, status mix, total account count, and
        the distinct segment count. With ``segment={"product": ..., "region":
        ...}`` the same aggregates are computed over just that product×region
        slice (either key is optional — a missing key is a wildcard).

        Delegates to :func:`waspada.insight.ranking.segment_health` so the
        numbers can never drift from what the dashboard renders.
        """
        table = self.scored
        seg_filter: Optional[Dict[str, str]] = None
        if isinstance(segment, dict) and segment:
            # Normalize: keep only non-empty product/region strings.
            seg_filter = {
                k: str(v) for k, v in segment.items()
                if k in ("product", "region") and str(v).strip()
            } or None

        if seg_filter:
            table = _filter_segment(table, seg_filter)

        n = table.num_rows
        if n == 0:
            return {
                "segment": seg_filter,
                "account_count": 0,
                "npl_ratio": 0.0,
                "vintage_default_rate": {},
                "status_mix": {},
            }

        health = _segment_health(table)
        out: Dict[str, Any] = {
            "segment": seg_filter,
            "account_count": int(n),
            "npl_ratio": float(health["npl_ratio"]),
            "vintage_default_rate": {
                str(k): float(v) for k, v in health["vintage_default_rate"].items()
            },
            "status_mix": {
                str(k): float(v) for k, v in health["status_mix"].items()
            },
        }
        # Worst vintage (the cohort a Skeptic most often cites) — honest
        # default-rate signal only; not a roll rate.
        vintage = out["vintage_default_rate"]
        if vintage:
            worst_year = max(vintage, key=lambda y: vintage[y])
            out["worst_vintage"] = {"year": worst_year, "default_rate": vintage[worst_year]}
        return out

    # ----------------------------------------------------------- lookup_account
    def lookup_account(self, loan_id: str) -> Dict[str, Any]:
        """Return the feature row for ``loan_id`` (empty dict if absent).

        This is the evidence the Skeptic cites: payment_ratio, dti,
        delinquency_status, loan_age, outstanding_ratio, grade, etc. The row
        is JSON-serializable (dates become ISO strings) so it crosses the MCP
        wire cleanly.
        """
        if self.features is None:
            return {}
        idx = self._index_of(loan_id)
        if idx is None:
            return {}
        row = {name: self.features.column(name)[idx].as_py()
               for name in self.features.column_names}
        return _jsonify_row(row)

    # ----------------------------------------------------------- internals
    def _index_of(self, loan_id: str) -> Optional[int]:
        """Return the row index for ``loan_id`` (lazily built loan_id→index)."""
        if self._loan_index is None:
            try:
                ids = self.features.column("loan_id").to_pylist()
            except (KeyError, ValueError, pa.ArrowInvalid):
                self._loan_index = {}
            else:
                self._loan_index = {str(v): i for i, v in enumerate(ids)}
        return self._loan_index.get(str(loan_id))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _filter_segment(scored: pa.Table, seg: Dict[str, str]) -> pa.Table:
    """Filter the scored table to rows matching the product×region segment."""
    import pyarrow.compute as pc

    mask = None
    try:
        segments = scored.column("segment").to_pylist()
    except (KeyError, ValueError, pa.ArrowInvalid):
        return scored.slice(0, 0)  # no segment column → empty result

    for i, s in enumerate(segments):
        match = isinstance(s, dict) and all(
            str(s.get(k, "")) == v for k, v in seg.items()
        )
        bit = True if match else False
        if mask is None:
            mask = [bit]
        else:
            mask.append(bit)
    if not mask or not any(mask):
        return scored.slice(0, 0)
    return scored.filter(pa.array(mask, type=pa.bool_()))


def _jsonify_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce a feature row's values to JSON-native types (dates → ISO)."""
    import datetime as dt

    out: Dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, (dt.date, dt.datetime)):
            out[k] = v.isoformat()
        elif isinstance(v, (bytes, bytearray)):
            out[k] = v.decode("utf-8", "replace")
        else:
            out[k] = v
    return out
