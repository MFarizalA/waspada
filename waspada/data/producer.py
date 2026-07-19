"""Data producer — the ingest half of the pipeline (source -> OSS Raw, partitioned).

Ties the pluggable source (WA-089) to the partitioned OSS land (the ``dt=<YYYYMMDD>``
convention) via the shared write path: ``get_source -> RawLoans -> loans/dt=<YYYYMMDD>/
loans.parquet``. The consumer (Data Engineer) then reads the *latest* partition (WA-047,
``resolve_key``) and loads it through dlt (WA-083). One command lands a fresh, immutable,
legal-clean partition — the manual step WA-088's scheduled batch will later automate.

CLI::

    python -m waspada.data.producer --source synthetic --limit 1000            # -> OSS Raw
    python -m waspada.data.producer --source lending_club --dry-run out.parquet # local, no OSS
"""
from __future__ import annotations

import argparse
import datetime as dt
from typing import Any, Optional

import pyarrow as pa
import pyarrow.parquet as pq

from .sources import get_source

__all__ = ["run_producer", "main"]


def _today_yyyymmdd() -> str:
    return dt.date.today().strftime("%Y%m%d")


def run_producer(
    source: Optional[str] = None,
    *,
    as_of: Optional[str] = None,
    limit: Optional[int] = None,
    prefix: str = "loans",
    oss_client: Any = None,
    dry_run_path: Optional[str] = None,
) -> dict:
    """Fetch ``RawLoans`` from ``source`` and land it as a date-partitioned OSS Raw object.

    ``source`` resolves via :func:`waspada.data.sources.get_source` (arg -> ``WASPADA_DATA_SOURCE``
    env -> synthetic). Returns a summary dict. ``dry_run_path`` writes a local Parquet instead of
    OSS (for inspection/tests); otherwise ``oss_client`` (or a fresh ``OSSClient``) puts it to the
    Raw bucket at ``{prefix}/dt=<YYYYMMDD>/loans.parquet``.
    """
    src = get_source(source)
    table: pa.Table = src.fetch(limit=limit)
    part = str(as_of) if as_of else _today_yyyymmdd()
    key = f"{prefix.rstrip('/')}/dt={part}/loans.parquet"

    if dry_run_path:
        pq.write_table(table, dry_run_path)
        return {"source": src.name, "rows": table.num_rows, "partition": part,
                "key": key, "dry_run": dry_run_path}

    from .oss import OSSClient
    client = oss_client or OSSClient()
    n_bytes = client.put_table(table, key)  # -> the client's Raw bucket
    return {"source": src.name, "rows": table.num_rows, "partition": part,
            "key": key, "bytes": n_bytes}


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="WASPADA data producer: source -> OSS Raw partition.")
    ap.add_argument("--source", default=None,
                    help="lending_club | synthetic | ... (default: WASPADA_DATA_SOURCE or synthetic)")
    ap.add_argument("--as-of", default=None, help="partition date YYYYMMDD (default: today)")
    ap.add_argument("--limit", type=int, default=None, help="cap the number of rows")
    ap.add_argument("--prefix", default="loans", help="OSS key prefix (default 'loans')")
    ap.add_argument("--dry-run", default=None, metavar="PATH",
                    help="write a local Parquet instead of uploading to OSS")
    a = ap.parse_args(argv)
    info = run_producer(source=a.source, as_of=a.as_of, limit=a.limit,
                        prefix=a.prefix, dry_run_path=a.dry_run)
    print("produced:", info)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
