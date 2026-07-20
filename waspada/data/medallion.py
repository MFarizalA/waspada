"""Medallion writers (WA-090) — activate the OSS Silver/Gold tiers.

The IaC provisions Staging + Mart buckets and grants FC PutObject, but nothing ever wrote to
them (a three-bucket medallion running as one live tier). This lands the Data Analyst's
features in **Silver** and the served payload in **Gold**, via the shared OSS write path.

**Guarded + best-effort:** writes only when OSS *and* the target bucket are configured, and
**never raise into the pipeline** — a failed cache write must not fail the run (mirrors the SLS
audit sink). Partitioned by ``dt=<YYYYMMDD>`` (the owner convention), so each run is an immutable
partition and the dashboard can read the latest Gold payload instantly.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any, Optional

import pyarrow as pa

__all__ = ["MedallionWriter"]


def _today_yyyymmdd() -> str:
    return dt.date.today().strftime("%Y%m%d")


class MedallionWriter:
    """Land Silver (features) + Gold (payload) in OSS. All methods are best-effort and
    return the written key or ``None`` when skipped (no OSS / no bucket / write failed)."""

    def __init__(self, client: Any = None, *, as_of: Optional[str] = None) -> None:
        # ``client`` is injected in tests; otherwise an OSSClient is built lazily when creds exist.
        self._client = client
        self.as_of = as_of

    def _partition(self) -> str:
        return str(self.as_of) if self.as_of else _today_yyyymmdd()

    def _oss(self) -> Optional[Any]:
        if self._client is not None:
            return self._client
        from .oss import OSSClient, _creds_configured
        if not _creds_configured():
            return None
        try:
            return OSSClient()
        except Exception:  # pragma: no cover - defensive: never fail the run to build a client
            return None

    def write_silver(
        self, feature_frame: Optional[pa.Table], aggregates: Optional[dict] = None,
        *, bucket: Optional[str] = None,
    ) -> Optional[str]:
        """FeatureFrame (+ optional aggregates) -> OSS Staging (Silver), partitioned."""
        oss = self._oss()
        bkt = bucket or os.environ.get("OSS_STAGING_BUCKET") or None
        if oss is None or feature_frame is None or not bkt:
            return None  # guarded: no OSS / no data / no staging bucket -> skip
        try:
            key = f"features/dt={self._partition()}/features.parquet"
            oss.put_table(feature_frame, key, bucket=bkt)
            if aggregates:
                oss.put_object(
                    f"features/dt={self._partition()}/aggregates.json",
                    json.dumps(aggregates, default=str).encode("utf-8"), bucket=bkt,
                )
            return key
        except Exception:  # best-effort: a failed cache write never fails the run
            return None

    def write_gold(self, payload: Any, *, bucket: Optional[str] = None) -> Optional[str]:
        """DashboardPayload -> OSS Mart (Gold), partitioned — the instant cached demo path."""
        oss = self._oss()
        bkt = bucket or os.environ.get("OSS_MART_BUCKET") or None
        if oss is None or payload is None or not bkt:
            return None
        try:
            key = f"payload/dt={self._partition()}/payload.json"
            oss.put_object(key, json.dumps(payload, default=str).encode("utf-8"), bucket=bkt)
            return key
        except Exception:
            return None
