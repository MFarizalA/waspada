"""Data producer acceptance — source -> OSS Raw partition (the ingest half).

Ties get_source (WA-089) to the partitioned OSS write path (dt=<YYYYMMDD>). The consumer
(WA-047 resolve_key) reads exactly this key back. Offline: dry-run writes a local parquet;
the OSS path is tested with an injected fake client.
"""
from __future__ import annotations

import dataclasses
from io import BytesIO

import pyarrow.parquet as pq
import pytest

from waspada.data.producer import run_producer
from waspada.data.sources import SyntheticSource
from waspada.schema import RawLoans, validate_table

_RAW_COLS = [f.name for f in dataclasses.fields(RawLoans)]


class FakeOSS:
    def __init__(self): self.writes = {}
    def put_table(self, table, key, *, bucket=None):
        buf = BytesIO(); pq.write_table(table, buf)
        self.writes[key] = buf.getvalue()
        return len(self.writes[key])


def test_dry_run_writes_local_partition_parquet(tmp_path):
    out = str(tmp_path / "loans.parquet")
    info = run_producer(source="synthetic", as_of="20260718", limit=50, dry_run_path=out)
    assert info["source"] == "synthetic"
    assert info["rows"] == 50
    assert info["key"] == "loans/dt=20260718/loans.parquet"   # the exact key the resolver will read
    back = pq.read_table(out).select(_RAW_COLS)
    assert back.num_rows == 50
    validate_table(back, RawLoans, name="producer-dry-run")


def test_uploads_to_oss_at_partition_key():
    oss = FakeOSS()
    info = run_producer(source="synthetic", as_of="20260718", limit=25, oss_client=oss)
    assert info["key"] == "loans/dt=20260718/loans.parquet"
    assert info["rows"] == 25 and info["bytes"] > 0
    # the partition object landed, RawLoans-valid
    back = pq.read_table(BytesIO(oss.writes["loans/dt=20260718/loans.parquet"])).select(_RAW_COLS)
    assert back.num_rows == 25
    validate_table(back, RawLoans, name="producer-oss")


def test_producer_key_round_trips_with_the_resolver():
    """The producer writes exactly what WA-047's resolver reads: same {prefix}/dt=.../loans.parquet."""
    from waspada.data.oss import latest_partition_key
    oss = FakeOSS()
    run_producer(source="synthetic", as_of="20260716", limit=5, oss_client=oss)
    run_producer(source="synthetic", as_of="20260718", limit=5, oss_client=oss)
    keys = list(oss.writes)
    assert latest_partition_key(keys, prefix="loans") == "loans/dt=20260718/loans.parquet"


def test_as_of_defaults_to_today_yyyymmdd():
    oss = FakeOSS()
    info = run_producer(source="synthetic", limit=1, oss_client=oss)
    part = info["partition"]
    assert len(part) == 8 and part.isdigit()               # YYYYMMDD
    assert info["key"] == f"loans/dt={part}/loans.parquet"


def test_custom_prefix():
    oss = FakeOSS()
    info = run_producer(source="synthetic", as_of="20260718", limit=1, prefix="raw/loans", oss_client=oss)
    assert info["key"] == "raw/loans/dt=20260718/loans.parquet"
