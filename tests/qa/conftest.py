"""Shared fixtures/helpers for the QA suite.

Puts the repo root on PYTHONPATH so ``import waspada`` works from the Linux
worker (the checked-in venv is a Windows venv), seeds OSS env from ``.env`` at
collection time for the live tests, and exposes the synthetic RawLoans table
that the feature/pipeline tests reuse.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import os
import sys
from pathlib import Path

import pyarrow as pa
import pytest

# Make the package importable from the Linux worker.
_REPO_ROOT = Path(__file__).resolve().parents[2]  # tests/qa -> repo root
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Seed env from .env so the live OSS tests see creds at collection time.
try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env")
except Exception:
    pass

from waspada.schema import RawLoans, schema_from_dataclass  # noqa: E402


def _oss_configured() -> bool:
    return bool(
        os.environ.get("OSS_BUCKET")
        and os.environ.get("OSS_ENDPOINT")
        and os.environ.get("OSS_KEY")
        and os.environ.get("OSS_ACCESS_KEY_ID")
        and os.environ.get("OSS_ACCESS_KEY_SECRET")
    )


# Capture live OSS env at module load (conftest's _clean_env strips it for the
# unit tests; restore it for the live QA tests).
_OSS_ENV = {k: os.environ.get(k, "") for k in (
    "OSS_BUCKET", "OSS_ENDPOINT", "OSS_KEY",
    "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET",
)}


@pytest.fixture
def oss_env(monkeypatch):
    """Restore OSS creds for a live test (no-op when not configured)."""
    for k, v in _OSS_ENV.items():
        if v:
            monkeypatch.setenv(k, v)
    yield _oss_configured()


def oss_configured() -> bool:
    return _oss_configured()


# ----------------------------------------------------------------------- data
def synthetic_raw_rows() -> list[dict]:
    """A small, deterministic RawLoans table covering both label classes.

    Spans multiple issue_date vintages so the vintage-split and leakage tests
    have real cohorts to reason about. Mirrors the shape tests/test_features.py
    uses, kept local so the QA suite is independent of the build tests.
    """
    return [
        dict(loan_id="L1", amount=15000.0, term=36, rate=13.56, grade="C",
             annual_income=58000.0, dti=18.2, issue_date=dt.date(2021, 3, 15),
             purpose="debt_consolidation", region="Jawa Barat",
             outstanding_principal=4200.0, total_paid=11800.0,
             current_status="Charged Off"),
        dict(loan_id="L2", amount=8000.0, term=36, rate=7.5, grade="A",
             annual_income=92000.0, dti=6.0, issue_date=dt.date(2022, 1, 1),
             purpose="credit_card", region="DKI Jakarta",
             outstanding_principal=5600.0, total_paid=2400.0,
             current_status="Current"),
        dict(loan_id="L3", amount=24000.0, term=60, rate=11.0, grade="B",
             annual_income=110000.0, dti=12.5, issue_date=dt.date(2021, 6, 20),
             purpose="home_improvement", region="Jawa Timur",
             outstanding_principal=0.0, total_paid=24000.0,
             current_status="Fully Paid"),
        dict(loan_id="L4", amount=5000.0, term=36, rate=22.0, grade="E",
             annual_income=40000.0, dti=28.0, issue_date=dt.date(2023, 9, 1),
             purpose="medical", region="Bali",
             outstanding_principal=3000.0, total_paid=2000.0,
             current_status="Default"),
        dict(loan_id="L5", amount=12000.0, term=60, rate=9.0, grade="B",
             annual_income=70000.0, dti=14.0, issue_date=dt.date(2023, 2, 10),
             purpose="car", region="Banten",
             outstanding_principal=7000.0, total_paid=5000.0,
             current_status="Late (16-30 days)"),
    ]


@pytest.fixture
def raw_table() -> pa.Table:
    rows = synthetic_raw_rows()
    cols = {f.name: [] for f in dataclasses.fields(RawLoans)}
    for r in rows:
        for name in cols:
            cols[name].append(r[name])
    return pa.table(cols, schema=schema_from_dataclass(RawLoans))


@pytest.fixture
def as_of_date() -> dt.date:
    return dt.date(2024, 12, 1)
