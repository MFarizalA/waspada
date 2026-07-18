"""Lakehouse data access layer (WA-029) — DuckDB read surface.

The Data Engineer agent (WA-029) reasons over the freshly-loaded book *before*
anyone trusts it. Its quality tools (validate_schema / null_rates /
profile_column / detect_anomalies) query the book via this thin layer:

  * :func:`load_to_duckdb` — register an in-memory Arrow table (or a local
    Parquet file) into an in-process DuckDB and hand back a :class:`Lakehouse`.
  * :class:`Lakehouse` — a handle holding a DuckDB connection plus the table
    name the quality tools query. Lazy-imports duckdb so the module imports
    cleanly when it isn't needed.

Where the OSS read actually happens
-----------------------------------
This layer does **not** read OSS. The portfolio snapshot is fetched by
:func:`waspada.data.oss.fetch_loans` (a bulk read of the Parquet object into a
pyarrow Table); the caller then passes that table here via ``arrow=``. There is
no ``dlt`` pipeline and no ``httpfs``/``s3://`` pushdown today — the earlier
scaffold called a ``dlt.readers.filesystem`` API that does not exist and was
never wired into a real entrypoint, so it was removed (WA-047). Genuine
pushdown / partition pruning against OSS is future work (WA-047 §read-path).

Design notes
------------
* The deterministic freshness + schema gate STAYS inside the Data Engineer
  agent, not here. This layer is just the read surface — it registers the table
  and lets the tools run SQL over it.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import pyarrow as pa

__all__ = ["Lakehouse", "load_to_duckdb", "get_analytics_connection"]


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


def get_analytics_connection() -> Any:
    """Return a connection to the DuckDB RDS analytical instance, or local DuckDB.

    When ``DUCKDB_RDS_ENDPOINT`` is set, returns a ``pymysql`` connection to
    the managed DuckDB read-only instance (WA-060). Otherwise falls back to a
    local embedded ``duckdb.connect(":memory:")`` — the offline/test path.

    Fail-safe: when the remote endpoint is not configured we degrade gracefully
    to local compute instead of raising.

    NOTE (WA-047/061): the managed-DuckDB path is not yet wired to a consumer,
    and it reads ``RDS_PASSWORD`` — an env var no entrypoint sets today (the FC
    deploy injects the password inside ``DATABASE_URL``, not standalone). Until
    the WA-061 owner decision lands, this path is unreachable in production;
    don't rely on it.
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
    local_parquet: Optional[str] = None,
    arrow: Optional[pa.Table] = None,
) -> Lakehouse:
    """Register the loan-portfolio snapshot into DuckDB and return a Lakehouse.

    Resolution order (first available wins):

    1. ``arrow`` — a pyarrow Table already in memory. This is the real path: the
       ingest layer reads the OSS Parquet via
       :func:`waspada.data.oss.fetch_loans` and hands the table here.
    2. ``local_parquet`` — a local Parquet file path, read via DuckDB directly.

    OSS is **not** read in this function (see the module docstring). Raises
    ``RuntimeError`` if neither source is provided — failing loud, not silently
    reading nothing.
    """
    import duckdb  # lazy: module imports cleanly without duckdb installed

    con = duckdb.connect(duckdb_path, read_only=False)

    # 1. In-memory Arrow (the OSS-read result, or a test/CLI table).
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

    raise RuntimeError(
        "load_to_duckdb: no source. Pass arrow= (the usual path — read OSS via "
        "waspada.data.oss.fetch_loans and hand the table in) or local_parquet=."
    )
