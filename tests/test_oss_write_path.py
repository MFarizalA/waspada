"""OSS write-path helper acceptance (the shared write door).

`oss.py` was read-only. This adds `put_object` / `put_table` — the single write path the
medallion writes (WA-090), the versioned model binary (WA-082), and dispute-memory-OSS all
go through. Offline-tested with an in-memory fake bucket; fail-loud (writes raise on failure).
"""
from __future__ import annotations

import dataclasses
from io import BytesIO

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from waspada.agents.__main__ import _sample_raw_table
from waspada.data.oss import OSSClient
from waspada.schema import RawLoans, validate_table

_RAW_COLS = [f.name for f in dataclasses.fields(RawLoans)]


class FakeBucket:
    """In-memory oss2.Bucket stand-in supporting the write + read-back path."""
    def __init__(self):
        self.store = {}

    def put_object(self, key, data):
        self.store[key] = data.getvalue() if hasattr(data, "getvalue") else (
            data.read() if hasattr(data, "read") else data)

    class _Reader:
        def __init__(self, d): self._d = d
        def read(self): return self._d

    def get_object(self, key):
        return self._Reader(self.store[key])


@pytest.fixture
def client(monkeypatch):
    for k, v in {
        "OSS_RAW_BUCKET": "waspada-prod-raw", "OSS_ENDPOINT": "oss.example.com",
        "OSS_KEY": "loans.parquet", "OSS_ACCESS_KEY_ID": "id", "OSS_ACCESS_KEY_SECRET": "sec",
    }.items():
        monkeypatch.setenv(k, v)
    return OSSClient(_bucket=FakeBucket())


def test_put_object_writes_bytes(client):
    client.put_object("models/pd/latest.json", b'{"model_id":"pd-lr-x"}')
    assert client._bucket.store["models/pd/latest.json"] == b'{"model_id":"pd-lr-x"}'


def test_put_table_round_trips_as_parquet(client):
    tbl = _sample_raw_table(n=60, seed=3)
    n_bytes = client.put_table(tbl, "features/dt=20260718/features.parquet")
    assert n_bytes > 0
    # read it back exactly as a consumer would
    raw = client._bucket.store["features/dt=20260718/features.parquet"]
    back = pq.read_table(BytesIO(raw)).select(_RAW_COLS)
    assert back.num_rows == 60
    validate_table(back, RawLoans, name="round-trip")


def test_put_object_accepts_file_like(client):
    client.put_object("k.bin", BytesIO(b"payload"))
    assert client._bucket.store["k.bin"] == b"payload"


def test_bucket_for_default_is_client_bucket(client):
    assert client._bucket_for(None) is client._bucket
    assert client._bucket_for("") is client._bucket
