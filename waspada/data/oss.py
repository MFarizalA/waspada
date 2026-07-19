"""The shared engine's data door: a reusable Alibaba Cloud OSS client.

One client serves both decision lanes. It reads creds from the environment
(via :mod:`waspada.config`), pulls the committed loan-portfolio Parquet object
from an Alibaba Cloud OSS bucket, and returns it as Arrow.

This mirrors the original data access pattern — the pipeline never pushed SQL logic
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
import re
from io import BytesIO
from typing import Any, Optional

import pyarrow as pa
import pyarrow.parquet as pq

from ..config import COLLECTIONS, ORIGINATION, Config, load_config
from ..schema import RawLoans, validate_table

__all__ = ["OSSClient", "fetch_loans", "latest_partition_key"]

# Columns we keep for the RawLoans contract — the exact field names, in the
# dataclass declaration order. Selecting explicitly (rather than trusting the
# object's columns as-is) means a stray column added upstream never silently
# reshapes the contract.
_RAW_LOANS_COLUMNS: tuple[str, ...] = tuple(f.name for f in dataclasses.fields(RawLoans))

# WA-047: the OSS layout is date-partitioned -- ``{prefix}/dt=<YYYYMMDD>/loans.parquet``
# (owner convention). ``YYYYMMDD`` sorts lexicographically == chronologically, so the
# newest partition is a plain ``max()`` over the date strings.
_PARTITION_RE = re.compile(r"(?:^|/)dt=(\d{8})(?:/|$)")


def latest_partition_key(
    keys, *, prefix: str, filename: str = "loans.parquet", as_of: Optional[str] = None,
) -> Optional[str]:
    """Resolve the target partition's object key from a list of OSS keys (pure).

    Matches keys shaped ``{prefix}/dt=<YYYYMMDD>/{filename}``. With ``as_of`` (a
    ``YYYYMMDD`` string) returns that exact partition's key; otherwise the **latest**
    (max ``YYYYMMDD``). Returns ``None`` when nothing matches -- so the caller can fail
    loud or fall back. Kept pure (no OSS calls) so the resolution logic is unit-tested
    without a live bucket.
    """
    pfx = prefix.strip("/")
    cands = []  # (dt, key)
    for k in keys:
        if not k.endswith(filename):
            continue
        if pfx and not k.startswith(pfx + "/"):
            continue
        m = _PARTITION_RE.search(k)
        if m:
            cands.append((m.group(1), k))
    if not cands:
        return None
    if as_of:
        for dt, k in cands:
            if dt == str(as_of):
                return k
        return None
    return max(cands, key=lambda dk: dk[0])[1]


def _creds_configured() -> bool:
    """True iff the OSS Raw bucket/endpoint/key + AccessKey pair are all set."""
    return bool(
        (os.environ.get("OSS_RAW_BUCKET") or os.environ.get("OSS_BUCKET"))
        and os.environ.get("OSS_ENDPOINT")
        and os.environ.get("OSS_KEY")
        and os.environ.get("OSS_ACCESS_KEY_ID")
        and os.environ.get("OSS_ACCESS_KEY_SECRET")
    )


class OSSClient:
    """Thin wrapper over ``oss2`` returning Arrow tables from a committed
    Parquet object (the loan-portfolio snapshot) in Alibaba Cloud OSS.

    Built from the ``OSS_RAW_BUCKET``/``OSS_ENDPOINT`` env vars. :meth:`fetch_loans`
    downloads the object, reads it as Parquet, and returns Arrow. There is no
    server-side query — the whole object is the "table" — so ``limit`` is
    applied client-side after the read, not pushed down.
    """

    def __init__(self, config: Optional[Config] = None, *, _bucket: Any = None) -> None:
        if not _creds_configured():
            raise RuntimeError(
                "OSS credentials not configured: set OSS_RAW_BUCKET, OSS_ENDPOINT, "
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
            # WA-057: read from the Raw bucket (Bronze layer).
            raw_bucket = os.environ.get("OSS_RAW_BUCKET") or os.environ.get("OSS_BUCKET", "")
            self._bucket = oss2.Bucket(
                auth, os.environ["OSS_ENDPOINT"], raw_bucket
            )

    # ------------------------------------------------------------------ core
    def object_meta(self, key: Optional[str] = None) -> dict:
        """Return ``{size_bytes, freshness}`` for the loan-portfolio object.

        ``freshness`` is the object's ``last_modified`` as an ISO-8601 UTC
        string — the equivalent of a warehouse ``table_meta().freshness``, used
        the same way (a freshness signal, not a schema report; OSS objects
        don't carry a server-side schema the way a SQL warehouse table does).
        """
        meta = self._bucket.head_object(key or self._cfg.oss_key)
        last_modified = meta.headers.get("Last-Modified")
        return {
            "size_bytes": int(meta.headers.get("Content-Length", 0)),
            "freshness": last_modified,
        }

    # ---------------------------------------------------- WA-047 partition resolver
    def list_keys(self, prefix: str) -> list:
        """List all object keys under ``prefix`` (paged), via ``oss2`` list_objects."""
        keys: list = []
        marker = ""
        while True:
            res = self._bucket.list_objects(prefix=prefix, marker=marker, max_keys=1000)
            keys.extend(o.key for o in getattr(res, "object_list", []) or [])
            if not getattr(res, "is_truncated", False):
                break
            marker = getattr(res, "next_marker", "") or ""
        return keys

    def resolve_key(self, *, prefix: Optional[str] = None, as_of: Optional[str] = None) -> str:
        """Resolve the object key to read.

        When a **prefix** is configured (arg, or ``OSS_PREFIX`` env) the layout is
        date-partitioned: list ``{prefix}/`` and pick the latest ``dt=<YYYYMMDD>``
        partition (or the pinned ``as_of`` / ``OSS_AS_OF``). Otherwise fall back to the
        fixed flat ``oss_key`` -- the pre-WA-047 behaviour, byte-for-byte.
        """
        pfx = (prefix if prefix is not None else os.environ.get("OSS_PREFIX", "")).strip()
        if not pfx:
            return self._cfg.oss_key  # fallback: fixed flat object (back-compat)
        filename = os.environ.get("OSS_PARTITION_FILE", "loans.parquet")
        as_of = as_of or os.environ.get("OSS_AS_OF") or None
        keys = self.list_keys(pfx.rstrip("/") + "/")
        key = latest_partition_key(keys, prefix=pfx, filename=filename, as_of=as_of)
        if key is None:
            raise FileNotFoundError(
                f"no dt=YYYYMMDD partition under OSS prefix {pfx!r}"
                + (f" for as_of={as_of}" if as_of else "")
                + f" (looking for {filename!r})"
            )
        return key

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
        key = self.resolve_key()
        data = self._bucket.get_object(key).read()
        table = pq.read_table(BytesIO(data))
        table = table.select(_RAW_LOANS_COLUMNS)
        if limit is not None:
            table = table.slice(0, int(limit))
        validate_table(table, RawLoans, name=f"fetch_loans({lane})")
        return table


    # ------------------------------------------------------- OSS write path (shared)
    def _bucket_for(self, bucket: Optional[str]) -> Any:
        """Resolve an oss2 bucket handle. ``None`` -> the client's (Raw) bucket; a named
        bucket (Staging/Mart) gets a cached handle sharing this client's auth+endpoint.

        The FC RAM write policy grants PutObject/DeleteObject on staging+mart (WA-057);
        this is the single write door the medallion writes (WA-090), the versioned model
        binary (WA-082), and dispute-memory-OSS all go through -- build once, reuse.
        """
        if not bucket:
            return self._bucket
        handles = self.__dict__.setdefault("_bucket_handles", {})
        if bucket not in handles:
            import oss2  # lazy: only the multi-bucket write path needs a fresh handle
            auth = oss2.Auth(
                os.environ["OSS_ACCESS_KEY_ID"], os.environ["OSS_ACCESS_KEY_SECRET"]
            )
            handles[bucket] = oss2.Bucket(auth, os.environ["OSS_ENDPOINT"], bucket)
        return handles[bucket]

    def put_object(self, key: str, data: Any, *, bucket: Optional[str] = None) -> None:
        """Write ``data`` (bytes or a file-like) to ``key`` in ``bucket`` (default: Raw).

        Fail-loud: a write that can't complete raises (unlike the best-effort SLS audit
        sink) -- a silently-dropped medallion/model write would be a correctness bug.
        """
        self._bucket_for(bucket).put_object(key, data)

    def get_bytes(self, key: str, *, bucket: Optional[str] = None) -> bytes:
        """Read ``key`` from ``bucket`` (default: Raw) as raw bytes.

        The read counterpart of :meth:`put_object` — the WA-082 model registry reads
        the pickled model + ``latest.json`` manifest through this. Raises on a missing
        key so the caller can fall back (train per-run).
        """
        return self._bucket_for(bucket).get_object(key).read()

    def put_table(self, table: pa.Table, key: str, *, bucket: Optional[str] = None) -> int:
        """Write a pyarrow Table as Parquet to ``key``; returns the number of bytes written.

        The convenience used by the medallion writers (FeatureFrame -> Staging, payload ->
        Mart) and any partitioned land (``{prefix}/dt=<YYYYMMDD>/loans.parquet``).
        """
        buf = BytesIO()
        pq.write_table(table, buf)
        payload = buf.getvalue()
        self.put_object(key, payload, bucket=bucket)
        return len(payload)


def fetch_loans(lane: str = COLLECTIONS, *, limit: Optional[int] = None) -> pa.Table:
    """Module-level convenience: a fresh :class:`OSSClient` then ``fetch_loans``."""
    return OSSClient().fetch_loans(lane=lane, limit=limit)
