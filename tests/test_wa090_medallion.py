"""WA-090 — medallion Silver/Gold write acceptance.

Activates the dead Staging/Mart tiers: FeatureFrame -> Silver, DashboardPayload -> Gold, via
the shared OSS write path, partitioned by dt=<YYYYMMDD>. Guarded + best-effort: writes only when
OSS *and* the target bucket are configured; never raises. Offline (no creds) it's a pure no-op,
so the pipeline is unchanged.
"""
from __future__ import annotations

import dataclasses
import json
from io import BytesIO

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from waspada.agents.__main__ import _sample_raw_table
from waspada.data.medallion import MedallionWriter
from waspada.schema import RawLoans, validate_table

_RAW_COLS = [f.name for f in dataclasses.fields(RawLoans)]


class FakeOSS:
    """Captures medallion writes: (bucket, key) -> bytes."""
    def __init__(self):
        self.writes = {}

    def put_table(self, table, key, *, bucket=None):
        buf = BytesIO(); pq.write_table(table, buf)
        self.writes[(bucket, key)] = buf.getvalue()

    def put_object(self, key, data, *, bucket=None):
        self.writes[(bucket, key)] = data


@pytest.fixture
def buckets(monkeypatch):
    monkeypatch.setenv("OSS_STAGING_BUCKET", "waspada-prod-staging")
    monkeypatch.setenv("OSS_MART_BUCKET", "waspada-prod-mart")


def test_write_silver_lands_features_partitioned(buckets):
    oss = FakeOSS()
    w = MedallionWriter(client=oss, as_of="20260718")
    ff = _sample_raw_table(n=40, seed=2)
    key = w.write_silver(ff, aggregates={"queries_run": [{"tool": "correlation"}]})
    assert key == "features/dt=20260718/features.parquet"
    # parquet landed in the Staging bucket, round-trips
    raw = oss.writes[("waspada-prod-staging", key)]
    back = pq.read_table(BytesIO(raw)).select(_RAW_COLS)
    assert back.num_rows == 40
    validate_table(back, RawLoans, name="silver-roundtrip")
    # aggregates landed alongside
    agg = oss.writes[("waspada-prod-staging", "features/dt=20260718/aggregates.json")]
    assert json.loads(agg)["queries_run"][0]["tool"] == "correlation"


def test_write_gold_lands_payload_partitioned(buckets):
    oss = FakeOSS()
    w = MedallionWriter(client=oss, as_of="20260718")
    payload = {"work_list": [{"loan_id": "lc-1", "recommended_action": "call"}], "alerts": []}
    key = w.write_gold(payload)
    assert key == "payload/dt=20260718/payload.json"
    got = json.loads(oss.writes[("waspada-prod-mart", key)])
    assert got["work_list"][0]["loan_id"] == "lc-1"


def test_no_staging_bucket_skips_silver(monkeypatch):
    monkeypatch.delenv("OSS_STAGING_BUCKET", raising=False)
    oss = FakeOSS()
    assert MedallionWriter(client=oss).write_silver(_sample_raw_table(n=5)) is None
    assert not oss.writes


def test_offline_no_oss_is_noop(monkeypatch):
    # no injected client and no OSS creds -> _oss() is None -> pure no-op
    for k in ("OSS_RAW_BUCKET", "OSS_ENDPOINT", "OSS_KEY", "OSS_ACCESS_KEY_ID",
              "OSS_ACCESS_KEY_SECRET", "OSS_STAGING_BUCKET", "OSS_MART_BUCKET"):
        monkeypatch.delenv(k, raising=False)
    w = MedallionWriter()
    assert w.write_silver(_sample_raw_table(n=5)) is None
    assert w.write_gold({"work_list": []}) is None


def test_write_failure_is_swallowed(buckets):
    class Boom(FakeOSS):
        def put_table(self, *a, **k): raise RuntimeError("oss down")
    w = MedallionWriter(client=Boom(), as_of="20260718")
    assert w.write_silver(_sample_raw_table(n=5)) is None  # best-effort: no raise


def test_none_inputs_are_noop(buckets):
    w = MedallionWriter(client=FakeOSS(), as_of="20260718")
    assert w.write_silver(None) is None
    assert w.write_gold(None) is None
