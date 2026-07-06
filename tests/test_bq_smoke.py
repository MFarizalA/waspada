"""Smoke test for the BigQuery ingest layer (WA-002 acceptance).

Skipped when BQ credentials are not configured (CI, fresh checkouts). When creds
*are* present, exercises the live sandbox table: ``table_meta`` reports a
non-zero row count and freshness; ``fetch_loans`` returns an Arrow table that is
a superset of the frozen :class:`RawLoans` contract (the limit keeps it cheap).
"""
from __future__ import annotations

import os

# Seed env from .env at collection time so this module's skip decision sees
# real creds even when nothing else has imported waspada.config yet.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass  # python-dotenv is a dep, but don't hard-fail collection on it.

import pyarrow as pa
import pytest


def _bq_configured() -> bool:
    return bool(
        os.environ.get("BQ_PROJECT")
        and os.environ.get("BQ_DATASET")
        and os.environ.get("BQ_TABLE")
        and os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    )


# Capture the live BQ values at module load (before conftest's autouse
# _clean_env fixture strips them for test isolation). Re-applied at runtime
# by the _restore_bq_env fixture below so the live client actually authenticates.
_BQ_ENV = {k: os.environ.get(k, "") for k in (
    "BQ_PROJECT", "BQ_DATASET", "BQ_TABLE",
    "GOOGLE_APPLICATION_CREDENTIALS",
)}


@pytest.fixture(autouse=True)
def _restore_bq_env(monkeypatch):
    """Re-apply the BQ env captured at module load (conftest's _clean_env runs
    first and strips these for isolation; this restores them so the live smoke
    test can actually authenticate)."""
    for k, v in _BQ_ENV.items():
        if v:
            monkeypatch.setenv(k, v)
    yield


pytestmark = pytest.mark.skipif(
    not _bq_configured(),
    reason="BQ credentials not configured (set BQ_PROJECT, BQ_DATASET, "
    "BQ_TABLE, GOOGLE_APPLICATION_CREDENTIALS to run this live smoke test).",
)


def test_table_meta_reports_nonzero_rows():
    from waspada.config import load_config
    from waspada.data import BigQueryClient

    cfg = load_config().require_bq()
    client = BigQueryClient(cfg)
    meta = client.table_meta(f"{cfg.bq_dataset}.{cfg.bq_table}")

    assert meta["n_rows"] > 0, f"expected non-zero rows, got {meta['n_rows']}"
    assert meta["schema"], "schema list should not be empty"
    assert meta["freshness"], "freshness should be reported"
    schema_names = [f["name"] for f in meta["schema"]]
    # The RawLoans contract fields must all be present in the live table.
    from waspada.schema import RawLoans
    import dataclasses
    for name in (f.name for f in dataclasses.fields(RawLoans)):
        assert name in schema_names, f"RawLoans field {name!r} missing from BQ schema"


def test_fetch_loans_is_rawloans_superset():
    from waspada.data import BigQueryClient
    from waspada.schema import RawLoans, validate_table

    client = BigQueryClient()
    # Small LIMIT keeps the smoke test cheap while still exercising the Arrow path.
    table = client.fetch_loans(lane="collections", limit=10)

    assert isinstance(table, pa.Table)
    assert table.num_rows > 0
    assert table.num_rows <= 10
    # validate_table checks the RawLoans fields are present with compatible types
    # (superset allowed — extra columns from BQ would pass).
    validate_table(table, RawLoans, name="fetch_loans result")
