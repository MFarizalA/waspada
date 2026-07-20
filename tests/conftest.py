"""Shared pytest fixtures for WASPADA tests.

Keeps the package importable without a real ``.env``, and gives
tests a deterministic snapshot date so feature/label tests are reproducible.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
from datetime import date

import pyarrow as pa
import pytest

from waspada.schema import RawLoans, schema_from_dataclass


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Ensure tests see a deterministic env (no leaked .env values).

    Tests must run offline. ``WASPADA_LLM_PROVIDER`` is forced to ``mock``
    so no test path that goes through :func:`get_llm` reaches the network if
    a developer's ``.env`` happens to set qwen. Individual tests that
    need a specific brain inject it directly (not via the env var).
    """
    for key in (
        "OSS_BUCKET", "OSS_ENDPOINT", "OSS_KEY",
        "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET",
        "WASPADA_LANE",
        # Strip live API keys so no test path reaches the network if a
        # developer's .env happens to set them.
        "DASHSCOPE_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WASPADA_LLM_PROVIDER", "mock")
    yield


@pytest.fixture(autouse=True)
def _mock_oss_probe_in_tests(monkeypatch):
    """Tests run without OSS credentials; force the startup probe to pass.

    Each test that hits ``/api/run`` or ``/api/run/stream`` is responsible for
    injecting its own data stub via the DataEngineerAgent tool registry. The
    probe only ensures the endpoint returns 200 instead of 503 at the gate.
    """
    import api.main as main_mod
    monkeypatch.setattr(main_mod, "_probe_oss", lambda: (True, "mock-probe-ok"))
    yield


@pytest.fixture(autouse=True)
def _set_dummy_oss_creds_in_tests(monkeypatch):
    """Provide dummy OSS credentials so :class:`OSSClient` can instantiate in
    tests that patch the underlying bucket; the startup probe is also patched
    so real network calls are not attempted."""
    monkeypatch.setenv("OSS_RAW_BUCKET", "waspada-test-raw")
    monkeypatch.setenv("OSS_ENDPOINT", "oss-test.aliyuncs.com")
    monkeypatch.setenv("OSS_KEY", "loans.parquet")
    monkeypatch.setenv("OSS_ACCESS_KEY_ID", "AK-test")
    monkeypatch.setenv("OSS_ACCESS_KEY_SECRET", "SK-test")
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

# --------------------------------------------------------------------------- #
# Shared synthetic data helpers (moved from test_wa016_debate to avoid cross- #
# module imports that break on Windows hosts).                                #
# --------------------------------------------------------------------------- #
@pytest.fixture
def _raw_rows():
    def _make(n: int = 60, seed: int = 11) -> list[dict]:
        import numpy as np
        rng = np.random.default_rng(seed)
        issue_years = [2019, 2020, 2021, 2022, 2023]
        rows: list[dict] = []
        for i in range(n):
            iy = int(issue_years[i % len(issue_years)])
            im = int(rng.integers(1, 13))
            risky = rng.random() < 0.5
            if risky:
                rate = float(rng.uniform(18, 28)); dti_ = float(rng.uniform(22, 35))
                grade = "E"; op = float(rng.uniform(0.5, 0.9)); tp = float(rng.uniform(0.0, 0.3))
                status = "Charged Off"
            else:
                rate = float(rng.uniform(4, 10)); dti_ = float(rng.uniform(2, 12))
                grade = "A"; op = float(rng.uniform(0.0, 0.3)); tp = float(rng.uniform(0.6, 1.0))
                status = "Current"
            rows.append(dict(
                loan_id=f"R{i:04d}", amount=float(rng.uniform(2000, 25000)),
                term=int(rng.choice([36, 60])), rate=rate, grade=grade,
                annual_income=float(rng.uniform(30000, 120000)), dti=dti_,
                issue_date=dt.date(iy, im, 1),
                purpose=str(rng.choice(["credit_card", "debt_consolidation", "car", "medical"])),
                region=str(rng.choice(["West", "South", "Midwest", "Northeast"])),
                outstanding_principal=float(rng.uniform(100, 5000)) * op,
                total_paid=float(rng.uniform(100, 5000)) * tp,
                current_status=status,
            ))
        return rows
    return _make


@pytest.fixture
def _raw_table():
    def _make(rows: list[dict]) -> pa.Table:
        cols = {f.name: [] for f in dataclasses.fields(RawLoans)}
        for r in rows:
            for name in cols:
                cols[name].append(r[name])
        return pa.table(cols, schema=schema_from_dataclass(RawLoans))
    return _make


@pytest.fixture
def _stub_fetch():
    def _make(table: pa.Table):
        def _fetch(*, lane="collections", limit=None):
            return table
        return _fetch
    return _make
