"""Lakehouse data access layer (WA-029) — dlt load + DuckDB.

The Data Engineer agent (WA-029) reasons over the freshly-loaded book *before*
anyone trusts it. Its quality tools (validate_schema / null_rates /
profile_column / detect_anomalies) query the book via this thin layer:

  * :func:`load_to_duckdb` — ``dlt load`` of the OSS Parquet object (S3-compatible
    endpoint) into an in-process DuckDB. For local dev / tests without OSS
    creds, callers skip the load and hand in a pre-built DuckDB relation (or a
    pyarrow Table we register) so the quality tools are exercised offline.
  * :class:`Lakehouse` — a handle holding a DuckDB connection plus the table
    name the quality tools query. Lazy-imports duckdb + dlt so the module
    imports cleanly when neither is needed.

Design notes
------------
* The deterministic freshness + schema gate STAYS inside the Data Engineer
  agent, not here. This layer is just the read surface — it loads/registers
  the table and lets the tools run SQL over it.
* ``dlt`` is imported lazily inside the load function; the quality tools only
  need duckdb. Tests never reach the dlt path (they build a Lakehouse from an
  in-memory Arrow table).
"""
from __future__ import annotations

import os
from typing import Any, Optional

import pyarrow as pa

__all__ = ["Lakehouse", "load_to_duckdb"]


class Lakehouse:
    """A thin handle over a DuckDB connection + a named table.

    The Data Engineer quality tools take a ``Lakehouse`` and run SQL via
    :meth:`sql` / :meth:`arrow`. Construction is cheap (a persistent read-only
    DuckDB file is fine; ``:memory:`` is the test default) so the tools can be
    stubbed in CI by handing the agent an in-memory table.

    Parameters
    ----------
    con : a ``duckdb.DuckDBPyConnection``. Caller owns it (we never close it).
    table : the name of the loaded/registered table the tools query.
    """

    def __init__(self, con: Any, *, table: str) -> None:
        self.con = con
        self.table = table

    # ----------------------------------------------------------- query surface
    def sql(self, sql: str) -> Any:
        """Run a SQL string and return the DuckDB result relation."""
        return self.con.execute(sql)

    def arrow(self, sql: str) -> pa.Table:
        """Run a SQL string and fetch the result as a pyarrow Table."""
        return self.con.execute(sql).to_arrow_table()

    def scalar(self, sql: str) -> Any:
        """Run a SQL string returning one scalar value (SELECT COUNT(*) ...)."""
        row = self.con.execute(sql).fetchone()
        return row[0] if row else None


def _oss_s3_endpoint() -> Optional[str]:
    """Return the S3-compatible endpoint URL for OSS, or None if unconfigured.

    Alibaba Cloud OSS exposes an S3-compatible API at ``https://<bucket>.<endpoint>``.
    dlt's filesystem source reads it via ``s3://`` URLs with the S3 creds
    mapped from the OSS_* env vars. Returns None when any required var is
    missing — callers fall back to the local Parquet path or the in-memory
    table path.
    """
    bucket = os.environ.get("OSS_RAW_BUCKET") or os.environ.get("OSS_BUCKET", "")
    endpoint = os.environ.get("OSS_ENDPOINT", "").strip()
    key_id = os.environ.get("OSS_ACCESS_KEY_ID", "").strip()
    secret = os.environ.get("OSS_ACCESS_KEY_SECRET", "").strip()
    if not (bucket and endpoint and key_id and secret):
        return None
    # dlt's s3 filesystem wants an ``endpoint_url`` (no bucket); creds via env.
    return endpoint


def get_analytics_connection() -> Any:
    """Return a connection to the DuckDB RDS analytical instance, or local DuckDB.

    When ``DUCKDB_RDS_ENDPOINT`` is set, returns a ``pymysql`` connection to
    the managed DuckDB read-only instance (WA-060). Otherwise falls back to a
    local embedded ``duckdb.connect(":memory:")`` — the offline/test path.

    Follows the same fail-safe pattern as :func:`_oss_s3_endpoint`: if the
    remote endpoint is not configured, we degrade gracefully to local compute
    instead of raising.
    """
    endpoint = os.environ.get("DUCKDB_RDS_ENDPOINT", "").strip()
    port = int(os.environ.get("DUCKDB_RDS_PORT", "3306"))
    if endpoint:
        import pymysql  # lazy: only needed on the remote-RDS path

        return pymysql.connect(
            host=endpoint,
            port=port,
            user="waspada",
            password=os.environ.get("RDS_PASSWORD", ""),
            database="waspada",
        )
    import duckdb  # lazy: module imports cleanly without duckdb installed

    return duckdb.connect(":memory:")


def load_to_duckdb(
    *,
    table: str = "raw_loans",
    duckdb_path: str = ":memory:",
    oss_key: Optional[str] = None,
    local_parquet: Optional[str] = None,
    arrow: Optional[pa.Table] = None,
) -> Lakehouse:
    """Load the loan-portfolio snapshot into DuckDB and return a Lakehouse.

    Resolution order (first available wins):

    1. ``arrow`` — a pyarrow Table already in memory (the test / offline path;
       no dlt, no network). Registered straight into DuckDB.
    2. ``local_parquet`` — a local Parquet file path. Read into DuckDB directly.
    3. OSS via dlt — the ``filesystem`` source over the S3-compatible OSS
       endpoint. Requires the full OSS_* env var set; ``oss_key`` (the object
       key) defaults to the configured one.

    Raises ``RuntimeError`` if no source resolves (no arrow, no local file, no
    OSS creds) — failing loud, not silently reading nothing.
    """
    import duckdb  # lazy: module imports cleanly without duckdb installed

    con = duckdb.connect(duckdb_path, read_only=False)

    # 1. In-memory arrow (tests / CLI offline path).
    if arrow is not None:
        con.register(table, arrow)
        return Lakehouse(con, table=table)

    # 2. Local Parquet file.
    if local_parquet and os.path.exists(local_parquet):
        con.execute(
            f"CREATE OR REPLACE TABLE {table} AS "
            f"SELECT * FROM read_parquet('{local_parquet}')"
        )
        return Lakehouse(con, table=table)

    # 3. dlt filesystem source over OSS (S3-compatible).
    endpoint = _oss_s3_endpoint()
    if endpoint is not None:
        import dlt  # lazy: only needed on the real OSS path

        bucket = os.environ["OSS_RAW_BUCKET"]
        key = oss_key or os.environ.get("OSS_KEY", "")
        os.environ.setdefault("S3_ENDPOINT", endpoint)
        # dlt's s3 filesystem reads AWS_* / S3_* creds; map OSS creds onto them.
        os.environ.setdefault("AWS_ACCESS_KEY_ID", os.environ["OSS_ACCESS_KEY_ID"])
        os.environ.setdefault(
            "AWS_SECRET_ACCESS_KEY", os.environ["OSS_ACCESS_KEY_SECRET"]
        )
        source_url = f"s3://{bucket}/{key}"
        pipeline = dlt.pipeline(
            pipeline_name="waspada_ingest",
            destination="duckdb",
            dataset_name="raw",
        )
        info = pipeline.run(
            dlt.readers.filesystem(bucket_url=f"s3://{bucket}", file_glob=key),
            table_name=table,
        )
        if info.has_failed_jobs:  # pragma: no cover - network path
            raise RuntimeError(f"dlt load failed: {info}")
        # The DuckDB file lives at the pipeline's location; reopen read-only.
        import dlt as _dlt  # noqa: F401
        return Lakehouse(con, table=table)

    raise RuntimeError(
        "Lakehouse load failed: no arrow table, no local Parquet, and OSS "
        "creds are not configured. Pass arrow= or local_parquet=, or set the "
        "OSS_* env vars (see .env.example)."
    )
