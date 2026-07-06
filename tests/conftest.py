"""Shared pytest fixtures for WASPADA tests.

Keeps the package importable without a real ``.env`` or GCP creds, and gives
tests a deterministic snapshot date so feature/label tests are reproducible.
"""

from __future__ import annotations

import os
from datetime import date

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Ensure tests see a deterministic env (no leaked .env values)."""
    for key in ("BQ_PROJECT", "BQ_DATASET", "BQ_TABLE", "WASPADA_LANE"):
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def as_of() -> str:
    """Canonical snapshot date used across feature/payload tests."""
    return date(2024, 12, 1).isoformat()


@pytest.fixture
def sample_rawloan():
    """A single representative RawLoans row (cross-sectional snapshot)."""
    from waspada.schema import RawLoans

    return RawLoans(
        loan_id="L-0001",
        amount=15000.0,
        term=36,
        rate=13.56,
        grade="C",
        annual_income=58000.0,
        dti=18.2,
        issue_date="2022-03-15",
        purpose="debt_consolidation",
        region="West",
        outstanding_principal=4200.0,
        total_paid=11800.0,
        current_status="Charged Off",
    )


@pytest.fixture
def sample_featureframe(as_of):
    """A single FeatureFrame matching the sample RawLoans above."""
    from waspada.schema import FeatureFrame

    return FeatureFrame(
        loan_id="L-0001",
        loan_age=33,
        payment_ratio=11800.0 / 15000.0,
        outstanding_ratio=4200.0 / 15000.0,
        delinquency_status="Default",
        dti=18.2,
        grade="C",
        term=36,
        label_default=True,
        as_of_date=as_of,
    )
