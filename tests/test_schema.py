"""Contract tests for the frozen WASPADA schemas (WA-001 acceptance).

Covers: the four contract types are exported (frozen dataclasses + a JSON
TypedDict); a FeatureFrame round-trips through parquet via Arrow;
schema_from_dataclass / validate_table accept a superset and reject
missing/mistyped fields. The WSL/GPU smoke test lives in test_wsl_smoke.py
(skipped in-container).
"""
from __future__ import annotations

import datetime as dt

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import waspada
from waspada import schema as schema_mod
from waspada.schema import (
    DashboardPayload,
    FeatureFrame,
    RawLoans,
    ScoredAccounts,
    Segment,
    schema_from_dataclass,
    validate_table,
)


# --------------------------------------------------------------------------
# Exports — downstream tickets cite these names verbatim.
# --------------------------------------------------------------------------
def test_package_exports_four_contract_types():
    # three frozen dataclasses + one JSON TypedDict
    import dataclasses

    assert dataclasses.is_dataclass(RawLoans)
    assert dataclasses.is_dataclass(FeatureFrame)
    assert dataclasses.is_dataclass(ScoredAccounts)
    assert DashboardPayload.__name__ == "DashboardPayload"
    # re-exported from the package root too
    assert waspada.RawLoans is RawLoans
    assert waspada.FeatureFrame is FeatureFrame
    assert waspada.ScoredAccounts is ScoredAccounts


def test_scored_accounts_has_nested_segment_dataclass():
    """ScoredAccounts.segment is the frozen Segment dataclass (product, region)."""
    import dataclasses

    fields = {f.name for f in __import__("dataclasses").fields(ScoredAccounts)}
    assert "segment" in fields
    assert dataclasses.is_dataclass(Segment)
    seg_fields = {f.name for f in dataclasses.fields(Segment)}
    assert seg_fields == {"product", "region"}


def test_feature_frame_label_is_bool_eventual_default():
    """label_default is bool (eventual default), NOT a 30-day roll."""
    from typing import get_type_hints

    hints = get_type_hints(FeatureFrame)
    assert hints["label_default"] is bool


# --------------------------------------------------------------------------
# Fixtures — a hand-built FeatureFrame (3 rows) as a list of dataclasses.
# --------------------------------------------------------------------------
def _feature_frame_rows():
    return [
        FeatureFrame(
            loan_id="L1", amount=1000.0, term=36, rate=10.0, grade="A",
            annual_income=60000.0, dti=10.0, purpose="car", region="CA",
            loan_age=12, payment_ratio=0.3, outstanding_ratio=0.7,
            delinquency_status="0", label_default=False, as_of_date=dt.date(2024, 1, 1),
        ),
        FeatureFrame(
            loan_id="L2", amount=2000.0, term=60, rate=12.5, grade="C",
            annual_income=45000.0, dti=22.0, purpose="house", region="TX",
            loan_age=30, payment_ratio=0.55, outstanding_ratio=0.45,
            delinquency_status="31-60", label_default=True, as_of_date=dt.date(2024, 1, 1),
        ),
        FeatureFrame(
            loan_id="L3", amount=3000.0, term=36, rate=8.0, grade="B",
            annual_income=80000.0, dti=15.0, purpose="debt_consolidation", region="NY",
            loan_age=6, payment_ratio=0.1, outstanding_ratio=0.9,
            delinquency_status="0", label_default=False, as_of_date=dt.date(2024, 1, 1),
        ),
    ]


def _rows_to_arrow(rows, dc) -> pa.Table:
    """Materialize a list of dataclass rows into an Arrow table matching the dc."""
    cols = {f.name: [] for f in __import__("dataclasses").fields(dc)}
    for r in rows:
        for name in cols:
            cols[name].append(getattr(r, name))
    return pa.table(cols, schema=schema_from_dataclass(dc))


# --------------------------------------------------------------------------
# WA-001 acceptance: a FeatureFrame round-trips through parquet.
# --------------------------------------------------------------------------
def test_feature_frame_parquet_roundtrip(tmp_path):
    tbl = _rows_to_arrow(_feature_frame_rows(), FeatureFrame)
    out = tmp_path / "feature_frame.parquet"
    pq.write_table(tbl, out)

    back = pq.read_table(out)
    validate_table(back, FeatureFrame, name="FeatureFrame")

    assert back.num_rows == 3
    assert back.column("loan_id").to_pylist() == ["L1", "L2", "L3"]
    assert back.column("label_default").to_pylist() == [False, True, False]
    assert back.column("as_of_date").to_pylist() == [dt.date(2024, 1, 1)] * 3


# --------------------------------------------------------------------------
# validate_table: superset OK, missing/mistyped rejected.
# --------------------------------------------------------------------------
def test_validate_table_accepts_superset():
    base = _rows_to_arrow(_feature_frame_rows(), FeatureFrame)
    extra = base.append_column("extra_internal_col", pa.array(["x", "y", "z"], pa.string()))
    validate_table(extra, FeatureFrame, name="FeatureFrame")  # no raise


def test_validate_table_rejects_missing_field():
    base = _rows_to_arrow(_feature_frame_rows(), FeatureFrame)
    dropped = base.drop(["payment_ratio"])
    with pytest.raises(ValueError, match="missing required field"):
        validate_table(dropped, FeatureFrame, name="FeatureFrame")


def test_validate_table_rejects_type_mismatch():
    base = _rows_to_arrow(_feature_frame_rows(), FeatureFrame)
    bad = base.drop(["amount"]).append_column(
        "amount", pa.array(["a", "b", "c"], pa.string())
    )
    with pytest.raises(ValueError, match="type mismatch"):
        validate_table(bad, FeatureFrame, name="FeatureFrame")


# --------------------------------------------------------------------------
# Dashboard payload type is constructible (frontend hand-off shape).
# --------------------------------------------------------------------------
def test_dashboard_payload_is_typeddict_shape():
    payload: DashboardPayload = {
        "work_list": [
            {
                "loan_id": "L2",
                "p_default": 0.83,
                "score_band": "Very High",
                "segment": {"product": "installment", "region": "TX"},
                "recommended_action": "call",
            }
        ],
        "portfolio_health": {
            "npl_ratio": 0.12,
            "vintage_default_rate": {"2021": 0.08, "2022": 0.11},
            "status_mix": {"Current": 0.7, "Charged Off": 0.12},
        },
        "alerts": [
            {
                "metric": "npl_ratio",
                "value": 0.25,
                "threshold": 0.15,
                "message": "TX installment NPL above threshold",
                "segment": {"product": "installment", "region": "TX"},
            }
        ],
    }
    assert set(payload.keys()) == {"work_list", "portfolio_health", "alerts"}
