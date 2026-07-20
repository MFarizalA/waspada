"""WA-047 — OSS latest-partition resolver acceptance.

The OSS layout is date-partitioned: ``loans/dt=<YYYYMMDD>/loans.parquet``. The reader resolves
the newest ``dt=`` partition (or a pinned ``as_of``); with no prefix configured it falls back to
the fixed flat object (pre-WA-047 behaviour). ``YYYYMMDD`` sorts lexicographically ==
chronologically, so "latest" is a plain max.
"""
from __future__ import annotations

import io

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from waspada.agents.__main__ import _sample_raw_table
from waspada.data.oss import OSSClient, latest_partition_key


# --------------------------------------------------------------------------- #
# Pure resolver.
# --------------------------------------------------------------------------- #
_KEYS = [
    "loans/dt=20260716/loans.parquet",
    "loans/dt=20260718/loans.parquet",   # latest
    "loans/dt=20260717/loans.parquet",
    "loans/dt=20260718/_SUCCESS",         # marker, not the parquet
    "other/dt=20260720/loans.parquet",    # wrong prefix
    "loans/README.txt",                   # no dt=
]


def test_latest_partition_picks_newest():
    assert latest_partition_key(_KEYS, prefix="loans") == "loans/dt=20260718/loans.parquet"


def test_as_of_picks_exact_partition():
    assert latest_partition_key(_KEYS, prefix="loans", as_of="20260717") == "loans/dt=20260717/loans.parquet"


def test_as_of_missing_returns_none():
    assert latest_partition_key(_KEYS, prefix="loans", as_of="20260101") is None


def test_no_partition_returns_none():
    assert latest_partition_key(["x/y.parquet", "loans/README.txt"], prefix="loans") is None


def test_prefix_and_filename_are_respected():
    # wrong prefix + the marker file are both excluded
    got = latest_partition_key(_KEYS, prefix="loans", filename="loans.parquet")
    assert got == "loans/dt=20260718/loans.parquet"
    assert "other/" not in got and "_SUCCESS" not in got


# --------------------------------------------------------------------------- #
# resolve_key + fetch_loans against a fake OSS bucket.
# --------------------------------------------------------------------------- #
class _Obj:
    def __init__(self, key): self.key = key


class _ListResult:
    def __init__(self, objs):
        self.object_list = objs
        self.is_truncated = False
        self.next_marker = ""


class _Reader:
    def __init__(self, data): self._data = data
    def read(self): return self._data


class FakeBucket:
    """Minimal oss2.Bucket stand-in: list_objects + get_object over an in-memory map."""
    def __init__(self, data_by_key):
        self._data = data_by_key

    def list_objects(self, prefix="", marker="", max_keys=1000):
        return _ListResult([_Obj(k) for k in self._data if k.startswith(prefix)])

    def get_object(self, key):
        return _Reader(self._data[key])


def _parquet_bytes(n=40, seed=1):
    buf = io.BytesIO()
    pq.write_table(_sample_raw_table(n=n, seed=seed), buf)
    return buf.getvalue()


@pytest.fixture
def _oss_env(monkeypatch):
    for k, v in {
        "OSS_RAW_BUCKET": "waspada-prod-raw", "OSS_ENDPOINT": "oss.example.com",
        "OSS_KEY": "loans.parquet", "OSS_ACCESS_KEY_ID": "id", "OSS_ACCESS_KEY_SECRET": "sec",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("OSS_PREFIX", raising=False)
    monkeypatch.delenv("OSS_AS_OF", raising=False)


def test_resolve_key_partitioned(_oss_env, monkeypatch):
    monkeypatch.setenv("OSS_PREFIX", "loans")
    data = {f"loans/dt=2026071{d}/loans.parquet": _parquet_bytes() for d in (6, 7, 8)}
    client = OSSClient(_bucket=FakeBucket(data))
    assert client.resolve_key() == "loans/dt=20260718/loans.parquet"           # latest
    assert client.resolve_key(as_of="20260716") == "loans/dt=20260716/loans.parquet"


def test_resolve_key_fallback_flat_object(_oss_env):
    # no OSS_PREFIX -> the fixed flat oss_key (back-compat), no listing
    client = OSSClient(_bucket=FakeBucket({"loans.parquet": _parquet_bytes()}))
    assert client.resolve_key() == "loans.parquet"


def test_fetch_loans_reads_latest_partition(_oss_env, monkeypatch):
    monkeypatch.setenv("OSS_PREFIX", "loans")
    data = {
        "loans/dt=20260717/loans.parquet": _parquet_bytes(n=10, seed=1),
        "loans/dt=20260718/loans.parquet": _parquet_bytes(n=25, seed=2),   # latest -> 25 rows
    }
    client = OSSClient(_bucket=FakeBucket(data))
    tbl = client.fetch_loans(lane="collections")
    assert tbl.num_rows == 25   # read the newest partition, not the older one


def test_resolve_key_no_partition_raises(_oss_env, monkeypatch):
    monkeypatch.setenv("OSS_PREFIX", "loans")
    client = OSSClient(_bucket=FakeBucket({"loans/README.txt": b"x"}))
    with pytest.raises(FileNotFoundError, match="no dt=YYYYMMDD partition"):
        client.resolve_key()
