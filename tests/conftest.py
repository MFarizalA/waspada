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
    """Ensure tests see a deterministic env (no leaked .env values).

    Tests must run offline. ``WASPADA_LLM_PROVIDER`` is forced to ``mock``
    so no test path that goes through :func:`get_llm` reaches the network if
    a developer's ``.env`` happens to set qwen/gemini. Individual tests that
    need a specific brain inject it directly (not via the env var).
    """
    for key in (
        "OSS_BUCKET", "OSS_ENDPOINT", "OSS_KEY",
        "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET",
        "WASPADA_LANE",
        # Strip live API keys so no test path reaches the network if a
        # developer's .env happens to set them.
        "DASHSCOPE_API_KEY", "GEMINI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WASPADA_LLM_PROVIDER", "mock")
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
        amount=15000.0,
        term=36,
        rate=13.56,
        grade="C",
        annual_income=58000.0,
        dti=18.2,
        purpose="debt_consolidation",
        region="West",
        loan_age=33,
        payment_ratio=11800.0 / 15000.0,
        outstanding_ratio=4200.0 / 15000.0,
        delinquency_status="Default",
        label_default=True,
        as_of_date=dt.date.fromisoformat(as_of),
    )
