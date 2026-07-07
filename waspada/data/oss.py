"""The shared engine's data door: a reusable Alibaba Cloud OSS client.

One client serves both decision lanes. It reads creds from the environment
(via :mod:`waspada.config`), pulls the committed loan-portfolio Parquet object
from an Alibaba Cloud OSS bucket, and returns it as Arrow.

This mirrors what the (retired) BigQuery client did on Google Cloud, but as a
bulk blob read rather than a SQL query: the pipeline never pushed SQL logic
(joins/aggregation) to the warehouse — every prior "fetch" was already a full
``SELECT *``-style bulk pull, with all real processing happening locally in
this Python process. A full-table read off object storage is a closer match
to that actual access pattern, not a downgrade — OSS just skips the
warehouse-provisioning ceremony a bulk read never needed.
"""
from __future__ import annotations

import dataclasses
import os
from io import BytesIO
from typing import Any, Optional

import pyarrow as pa
import pyarrow.parquet as pq

from ..config import COLLECTIONS, ORIGINATION, Config, load_config
from ..schema import RawLoans, validate_table

__all__ = ["OSSClient", "fetch_loans"]

# Columns we keep for the RawLoans contract — the exact field names, in the
# dataclass declaration order. Selecting explicitly (rather than trusting the
# object's columns as-is) means a stray column added upstream never silently
# reshapes the contract.
_RAW_LOANS_COLUMNS: tuple[str, ...] = tuple(f.name for f in dataclasses.fields(RawLoans))


def _creds_configured() -> bool:
    """True iff the OSS bucket/endpoint/key + AccessKey pair are all set.

    We check the AccessKey env vars are *set* (not that they're valid) so a
    misconfigured key still fails loudly at fetch time with the SDK's own
    auth error, rather than a confusing error from this gate.
    """
    return bool(
        os.environ.get("OSS_BUCKET")
        and os.environ.get("OSS_ENDPOINT")
        and os.environ.get("OSS_KEY")
        and os.environ.get("OSS_ACCESS_KEY_ID")
        and os.environ.get("OSS_ACCESS_KEY_SECRET")
    )


class OSSClient:
    """Thin wrapper over ``oss2`` returning Arrow tables from a committed
    Parquet object (the loan-portfolio snapshot) in Alibaba Cloud OSS.

    Built from the ``OSS_BUCKET``/``OSS_ENDPOINT`` env vars. :meth:`fetch_loans`
    downloads the object, reads it as Parquet, and returns Arrow. There is no
    server-side query — the whole object is the "table" — so ``limit`` is
    applied client-side after the read, not pushed down.
    """

    def __init__(self, config: Optional[Config] = None, *, _bucket: Any = None) -> None:
        if not _creds_configured():
            raise RuntimeError(
                "OSS credentials not configured: set OSS_BUCKET, OSS_ENDPOINT, "
                "OSS_KEY, OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET (see .env.example)."
            )
        self._cfg = config or load_config()
        # Lazily import so the module is importable without the SDK installed
        # (tests that only check the no-creds gate never touch oss2).
        if _bucket is not None:
            self._bucket = _bucket
        else:
            import oss2

            auth = oss2.Auth(
                os.environ["OSS_ACCESS_KEY_ID"], os.environ["OSS_ACCESS_KEY_SECRET"]
            )
            self._bucket = oss2.Bucket(
                auth, os.environ["OSS_ENDPOINT"], self._cfg.oss_bucket
            )

    # ------------------------------------------------------------------ core
    def object_meta(self, key: Optional[str] = None) -> dict:
        """Return ``{size_bytes, freshness}`` for the loan-portfolio object.

        ``freshness`` is the object's ``last_modified`` as an ISO-8601 UTC
        string — the equivalent of BigQuery's ``table_meta().freshness``, used
        the same way (a freshness signal, not a schema report; OSS objects
        don't carry a server-side schema the way a BQ table does).
        """
        meta = self._bucket.head_object(key or self._cfg.oss_key)
        last_modified = meta.headers.get("Last-Modified")
        return {
            "size_bytes": int(meta.headers.get("Content-Length", 0)),
            "freshness": last_modified,
        }

    def fetch_loans(self, lane: str = COLLECTIONS, *, limit: Optional[int] = None) -> pa.Table:
        """Return a ``RawLoans``-shaped Arrow table for ``lane``.

        Downloads the configured object, reads it as Parquet, selects exactly
        the :class:`RawLoans` columns (declaration order), and validates the
        result. ``limit`` slices the first N rows client-side (the whole
        object is already in memory — there's no warehouse to push a LIMIT
        into).
        """
        if lane not in (COLLECTIONS, ORIGINATION):
            raise ValueError(
                f"lane={lane!r} is invalid; must be 'collections' or 'origination'"
            )
        data = self._bucket.get_object(self._cfg.oss_key).read()
        table = pq.read_table(BytesIO(data))
        table = table.select(_RAW_LOANS_COLUMNS)
        if limit is not None:
            table = table.slice(0, int(limit))
        validate_table(table, RawLoans, name=f"fetch_loans({lane})")
        return table


def fetch_loans(lane: str = COLLECTIONS, *, limit: Optional[int] = None) -> pa.Table:
    """Module-level convenience: a fresh :class:`OSSClient` then ``fetch_loans``."""
    return OSSClient().fetch_loans(lane=lane, limit=limit)
