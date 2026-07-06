"""Collections feature + label engineering tests (WA-004 acceptance).

CPU-only by design: these build a tiny synthetic ``RawLoans`` frame in Arrow and
exercise :mod:`waspada.features.collections` (the pyarrow reference path). The
GPU/cuDF path is exercised separately by a WSL smoke check (see
``gpu/run_features.py``) and is NOT required here.
"""
from __future__ import annotations

import datetime as dt
from typing import List

import pyarrow as pa
import pytest

from waspada.features.collections import (
    DEFAULT_STATUSES,
    assert_no_nulls,
    build_features,
    build_label,
    delinquency_bucket,
    is_default,
)
from waspada.schema import FeatureFrame, RawLoans, schema_from_dataclass

# Required (non-nullable) FeatureFrame fields — every one must be populated.
REQUIRED_FIELDS = [f.name for f in __import__("dataclasses").fields(FeatureFrame)]


# -------------------------------------------------------------------------- #
# Fixtures — a tiny synthetic RawLoans table, hand-built for determinism.
# -------------------------------------------------------------------------- #
def _raw_loans_rows() -> list[dict]:
    """Three rows exercising the label's two classes + edge cases.

    L1 "Charged Off" → True ; L2 "Current" → False ; L3 "Fully Paid" → False.
    L4 "Default"     → True ; L5 "Late (16-30 days)" → False (not terminal).
    issue_dates straddle the as-of so loan_age covers a range.
    """
    return [
        dict(loan_id="L1", amount=15000.0, term=36, rate=13.56, grade="C",
             annual_income=58000.0, dti=18.2, issue_date=dt.date(2022, 3, 15),
             purpose="debt_consolidation", region="West",
             outstanding_principal=4200.0, total_paid=11800.0,
             current_status="Charged Off"),
        dict(loan_id="L2", amount=8000.0, term=36, rate=7.5, grade="A",
             annual_income=92000.0, dti=6.0, issue_date=dt.date(2024, 1, 1),
             purpose="credit_card", region="Northeast",
             outstanding_principal=5600.0, total_paid=2400.0,
             current_status="Current"),
        dict(loan_id="L3", amount=24000.0, term=60, rate=11.0, grade="B",
             annual_income=110000.0, dti=12.5, issue_date=dt.date(2021, 6, 20),
             purpose="home_improvement", region="South",
             outstanding_principal=0.0, total_paid=24000.0,
             current_status="Fully Paid"),
        dict(loan_id="L4", amount=5000.0, term=36, rate=22.0, grade="E",
             annual_income=40000.0, dti=28.0, issue_date=dt.date(2023, 9, 1),
             purpose="medical", region="Midwest",
             outstanding_principal=3000.0, total_paid=2000.0,
             current_status="Default"),
        dict(loan_id="L5", amount=12000.0, term=60, rate=9.0, grade="B",
             annual_income=70000.0, dti=14.0, issue_date=dt.date(2023, 2, 10),
             purpose="car", region="West",
             outstanding_principal=7000.0, total_paid=5000.0,
             current_status="Late (16-30 days)"),
    ]


def _raw_table() -> pa.Table:
    rows = _raw_loans_rows()
    cols = {f.name: [] for f in __import__("dataclasses").fields(RawLoans)}
    for r in rows:
        for name in cols:
            cols[name].append(r[name])
    return pa.table(cols, schema=schema_from_dataclass(RawLoans))


@pytest.fixture
def raw_table() -> pa.Table:
    return _raw_table()


@pytest.fixture
def as_of_date() -> dt.date:
    """Canonical as-of (matches conftest's 2024-12-01 snapshot date)."""
    return dt.date(2024, 12, 1)


# -------------------------------------------------------------------------- #
# build_label — eventual charge-off / default only (NOT a 30-day roll).
# -------------------------------------------------------------------------- #
def test_label_charged_off_is_true():
    """The WA-004 hand-built case: Charged Off → True, Current → False."""
    assert is_default("Charged Off") is True
    assert is_default("Current") is False
    assert is_default("Fully Paid") is False


def test_label_default_is_true():
    """Default (terminal) → True; in-flight delinquency (Late) → False."""
    assert is_default("Default") is True
    assert is_default("Late (16-30 days)") is False


def test_build_label_matches_hand_built_case(raw_table):
    """build_label returns bool array: [True, False, False, True, False]."""
    labels = build_label(raw_table)
    assert labels.type == pa.bool_()
    assert labels.to_pylist() == [True, False, False, True, False]


def test_build_label_case_insensitive_and_null_safe():
    """Statuses match case-insensitively; empty/unknown status → False (not True)."""
    assert is_default("charged off") is True
    assert is_default("DEFAULT") is True
    assert is_default("") is False
    assert is_default("Does Not Exist") is False


def test_default_statuses_are_the_two_terminal_defaults():
    """The frozen default set is exactly {charged off, default} (lowercased)."""
    assert DEFAULT_STATUSES == frozenset({"charged off", "default"})


# -------------------------------------------------------------------------- #
# delinquency_bucket — a feature (coarse bucket), never leaks nulls.
# -------------------------------------------------------------------------- #
def test_delinquency_bucket_known():
    assert delinquency_bucket("Current") == "0"
    assert delinquency_bucket("Fully Paid") == "0"
    assert delinquency_bucket("Charged Off") == "Default"
    assert delinquency_bucket("Default") == "Default"
    assert delinquency_bucket("In Grace Period") == "1-30"
    assert delinquency_bucket("Late (16-30 days)") == "16-30"
    assert delinquency_bucket("Late (31-120 days)") == "31-120"


def test_delinquency_bucket_unknown_is_other_not_none():
    assert delinquency_bucket("zzz") == "other"
    assert delinquency_bucket("") == "other"
    assert delinquency_bucket(None) == "other"


# -------------------------------------------------------------------------- #
# build_features — shape, dtypes, derived values, no-nulls, contract-valid.
# -------------------------------------------------------------------------- #
def test_build_features_returns_featureframe_shape(raw_table, as_of_date):
    out = build_features(raw_table, as_of_date)
    assert isinstance(out, pa.Table)
    assert out.num_rows == raw_table.num_rows
    # Exactly the contract fields, no more.
    assert set(out.column_names) == set(REQUIRED_FIELDS)


def test_build_features_dtypes_match_contract(raw_table, as_of_date):
    out = build_features(raw_table, as_of_date)
    expected_schema = schema_from_dataclass(FeatureFrame)
    for f in expected_schema:
        actual = out.schema.field(f.name).type
        assert actual.equals(f.type), f"{f.name}: expected {f.type}, got {actual}"


def test_build_features_loan_age_computation(raw_table, as_of_date):
    out = build_features(raw_table, as_of_date)
    # Expected whole-month ages, clamped at 0:
    #   L1 2022-03 -> 2024-12 = 33 ; L2 2024-01 -> 2024-12 = 11
    #   L3 2021-06 -> 2024-12 = 42 ; L4 2023-09 -> 2024-12 = 15
    #   L5 2023-02 -> 2024-12 = 22
    assert out.column("loan_age").to_pylist() == [33, 11, 42, 15, 22]


def test_build_features_ratios(raw_table, as_of_date):
    out = build_features(raw_table, as_of_date)
    # payment_ratio = total_paid / amount ; outstanding_ratio = outstanding_principal / amount
    import dataclasses as dc

    raw_rows = _raw_loans_rows()
    rows = _rows_to_frame(out)
    by_id = {r["loan_id"]: r for r in rows}
    for src in raw_rows:
        rid = src["loan_id"]
        assert by_id[rid]["payment_ratio"] == pytest.approx(
            src["total_paid"] / src["amount"], rel=1e-9
        )
        assert by_id[rid]["outstanding_ratio"] == pytest.approx(
            src["outstanding_principal"] / src["amount"], rel=1e-9
        )


def _rows_to_frame(table: pa.Table) -> List[dict]:
    cols = table.column_names
    pycols = {c: table.column(c).to_pylist() for c in cols}
    return [dict(zip(cols, vals)) for vals in zip(*[pycols[c] for c in cols])]


def test_build_features_zero_amount_does_not_inf_or_nan(as_of_date):
    """amount==0 must not produce inf/nan ratios — guarded to 0.0."""
    rows = [
        dict(loan_id="Z1", amount=0.0, term=36, rate=5.0, grade="A",
             annual_income=50000.0, dti=10.0, issue_date=dt.date(2023, 1, 1),
             purpose="car", region="West",
             outstanding_principal=0.0, total_paid=0.0,
             current_status="Current"),
    ]
    cols = {f.name: [] for f in __import__("dataclasses").fields(RawLoans)}
    for r in rows:
        for name in cols:
            cols[name].append(r[name])
    raw = pa.table(cols, schema=schema_from_dataclass(RawLoans))

    out = build_features(raw, as_of_date)
    assert out.column("payment_ratio")[0].as_py() == 0.0
    assert out.column("outstanding_ratio")[0].as_py() == 0.0


def test_build_features_no_nulls_in_required_fields(raw_table, as_of_date):
    """Acceptance: no nulls in any required field."""
    out = build_features(raw_table, as_of_date)
    assert_no_nulls(out, FeatureFrame)  # raises if any null


def test_build_features_as_of_date_column(raw_table, as_of_date):
    out = build_features(raw_table, as_of_date)
    assert out.column("as_of_date").to_pylist() == [as_of_date] * out.num_rows


def test_build_features_carries_snapshot_columns(raw_table, as_of_date):
    out = build_features(raw_table, as_of_date)
    for snap in ("amount", "term", "rate", "grade", "annual_income", "dti",
                 "purpose", "region"):
        assert out.column(snap).to_pylist() == raw_table.column(snap).to_pylist()


def test_build_features_rejects_non_rawloans_input(as_of_date):
    """A table missing a RawLoans field fails loud (validate_table up front)."""
    bad = _raw_table().drop(["grade"])
    with pytest.raises(ValueError, match="missing required field"):
        build_features(bad, as_of_date)


def test_build_features_future_issue_date_clamps_loan_age(as_of_date):
    """A loan issued *after* as_of has loan_age 0 (no negative ages)."""
    rows = [
        dict(loan_id="FUT", amount=1000.0, term=12, rate=5.0, grade="A",
             annual_income=50000.0, dti=5.0, issue_date=dt.date(2025, 5, 1),
             purpose="car", region="West",
             outstanding_principal=1000.0, total_paid=0.0,
             current_status="Current"),
    ]
    cols = {f.name: [] for f in __import__("dataclasses").fields(RawLoans)}
    for r in rows:
        for name in cols:
            cols[name].append(r[name])
    raw = pa.table(cols, schema=schema_from_dataclass(RawLoans))
    out = build_features(raw, as_of_date)
    assert out.column("loan_age")[0].as_py() == 0
