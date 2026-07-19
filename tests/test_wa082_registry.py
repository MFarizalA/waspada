"""WA-082 — PD model registry (version + publish/load to OSS).

Pins the registry core with a fake in-memory OSS client (no network):
  1. model_id is deterministic + stable, and changes when the model changes;
  2. dumps/loads round-trips the artifact (incl. the WA-094 calibrator);
  3. publish_model writes the versioned binary + a latest.json manifest, and
     load_published_model reads it back to a model that scores identically;
  4. a pinned model_id loads that exact version;
  5. train() stamps a model_id, and the monitoring record carries it (WA-082
     lineage on the model card);
  6. the risk_model agent serves per-run training by default (opt-in guarded).
"""
from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pyarrow as pa
import pytest

from waspada.model.monitoring import build_monitor_record
from waspada.model.registry import (
    dumps_model,
    load_published_model,
    loads_model,
    model_id,
    model_manifest,
    publish_model,
)
from waspada.model.risk import predict, train
from waspada.schema import FeatureFrame, schema_from_dataclass


class FakeOSS:
    """Minimal in-memory stand-in for OSSClient (put_object / get_bytes)."""

    def __init__(self):
        self.store: dict[tuple[str | None, str], bytes] = {}

    def put_object(self, key, data, *, bucket=None):
        if not isinstance(data, (bytes, bytearray)):
            data = bytes(data)
        self.store[(bucket, key)] = bytes(data)

    def get_bytes(self, key, *, bucket=None):
        try:
            return self.store[(bucket, key)]
        except KeyError:
            raise FileNotFoundError(key)


def _feature_frame(rows, as_of):
    import dataclasses

    cols = {f.name: [] for f in dataclasses.fields(FeatureFrame)}
    for r in rows:
        for name in cols:
            cols[name].append(r[name])
    return pa.table(cols, schema=schema_from_dataclass(FeatureFrame))


def _frame(n=200, seed=5):
    rng = np.random.default_rng(seed)
    as_of = dt.date(2024, 12, 1)
    years = [2019, 2020, 2021, 2022, 2023]
    rows = []
    for i in range(n):
        risky = rng.random() < 0.5
        rows.append(dict(
            loan_id=f"L{i:04d}", amount=float(rng.uniform(2000, 25000)),
            term=int(rng.choice([36, 60])),
            rate=float(rng.uniform(18, 28) if risky else rng.uniform(4, 10)),
            grade=("E" if risky else "A"),
            annual_income=float(rng.uniform(30000, 120000)),
            dti=float(rng.uniform(22, 35) if risky else rng.uniform(2, 12)),
            purpose=str(rng.choice(["credit_card", "debt_consolidation", "car", "medical"])),
            region=str(rng.choice(["West", "South", "Midwest", "Northeast"])),
            loan_age=int(rng.integers(6, 48)),
            payment_ratio=float(rng.uniform(0.0, 0.3) if risky else rng.uniform(0.6, 1.0)),
            outstanding_ratio=float(rng.uniform(0.0, 1.0)),
            delinquency_status=("Default" if risky else "0"),
            label_default=bool(risky), as_of_date=as_of,
        ))
    return _feature_frame(rows, as_of)


def test_model_id_deterministic_and_change_sensitive():
    m1 = train(_frame(seed=5))
    m2 = train(_frame(seed=5))   # same data → same fitted model → same id
    m3 = train(_frame(seed=9))   # different data → different id
    assert model_id(m1) == model_id(m2)
    assert model_id(m1) != model_id(m3)
    assert model_id(m1).startswith("pd-lr-")


def test_dumps_loads_roundtrip_scores_identically():
    frame = _frame()
    model = train(frame)
    restored = loads_model(dumps_model(model))
    a = predict(model, frame).column("p_default").to_pylist()
    b = predict(restored, frame).column("p_default").to_pylist()
    assert a == b


def test_publish_and_load_latest():
    frame = _frame()
    model = train(frame)
    oss = FakeOSS()

    manifest = publish_model(model, oss, bucket="staging")
    # binary + manifest both written
    assert (("staging", manifest["key"]) in oss.store)
    assert (("staging", "models/pd/latest.json") in oss.store)
    stored_manifest = json.loads(oss.get_bytes("models/pd/latest.json", bucket="staging"))
    assert stored_manifest["model_id"] == manifest["model_id"] == model_id(model)

    loaded = load_published_model(oss, bucket="staging")
    assert loaded["model_id"] == model_id(model)
    a = predict(model, frame).column("p_default").to_pylist()
    b = predict(loaded, frame).column("p_default").to_pylist()
    assert a == b


def test_load_pinned_version():
    frame = _frame()
    model = train(frame)
    oss = FakeOSS()
    publish_model(model, oss, bucket="staging")
    mid = model_id(model)

    loaded = load_published_model(oss, model_id=mid, bucket="staging")
    assert loaded["model_id"] == mid


def test_load_missing_raises():
    with pytest.raises((FileNotFoundError, KeyError)):
        load_published_model(FakeOSS(), bucket="staging")


def test_manifest_shape():
    model = train(_frame())
    man = model_manifest(model)
    for key in ("model_id", "kind", "trained_at", "schema_version", "feature_columns", "auc"):
        assert key in man
    assert man["kind"] == "logistic_regression"


def test_train_stamps_model_id_and_monitor_record_carries_it():
    frame = _frame()
    model = train(frame)
    assert model.get("model_id", "").startswith("pd-lr-")
    scored = predict(model, frame)
    rec = build_monitor_record(model, scored, frame)
    assert rec["model_id"] == model["model_id"]


def test_agent_defaults_to_per_run_training(monkeypatch):
    """Without WASPADA_PD_MODEL_SOURCE=oss the serve hook returns None (train)."""
    from waspada.agents.risk_model import RiskModelAgent

    monkeypatch.delenv("WASPADA_PD_MODEL_SOURCE", raising=False)
    agent = RiskModelAgent()
    assert agent._load_published_model() is None
