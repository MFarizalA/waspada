"""Smoke test for the Alibaba Cloud OSS ingest layer.

Skipped when OSS credentials are not configured (CI, fresh checkouts). When
creds *are* present, exercises the live bucket: ``object_meta`` reports a
non-zero size and freshness; ``fetch_loans`` returns an Arrow table that is a
superset of the frozen :class:`RawLoans` contract (the limit keeps it cheap).
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


def _oss_configured() -> bool:
    return bool(
        os.environ.get("OSS_BUCKET")
        and os.environ.get("OSS_ENDPOINT")
        and os.environ.get("OSS_KEY")
        and os.environ.get("OSS_ACCESS_KEY_ID")
        and os.environ.get("OSS_ACCESS_KEY_SECRET")
    )


# Capture the live OSS values at module load (before conftest's autouse
# _clean_env fixture strips BQ_* for test isolation — OSS_* isn't stripped by
# that fixture, but we mirror the same restore pattern for consistency and in
# case a future conftest change adds OSS_* to the cleared set).
_OSS_ENV = {k: os.environ.get(k, "") for k in (
    "OSS_BUCKET", "OSS_ENDPOINT", "OSS_KEY",
    "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET",
)}


@pytest.fixture(autouse=True)
def _restore_oss_env(monkeypatch):
    """Re-apply the OSS env captured at module load so the live smoke test can
    actually authenticate, regardless of what other fixtures clear."""
    for k, v in _OSS_ENV.items():
        if v:
            monkeypatch.setenv(k, v)
    yield


pytestmark = pytest.mark.skipif(
    not _oss_configured(),
    reason="OSS credentials not configured (set OSS_BUCKET, OSS_ENDPOINT, "
    "OSS_KEY, OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET to run this live smoke test).",
)


def test_object_meta_reports_nonzero_size():
    from waspada.config import load_config
    from waspada.data import OSSClient

    cfg = load_config().require_oss()
    client = OSSClient(cfg)
    meta = client.object_meta()

    assert meta["size_bytes"] > 0, f"expected non-zero size, got {meta['size_bytes']}"
    assert meta["freshness"], "freshness should be reported"


def test_fetch_loans_is_rawloans_superset():
    from waspada.data import OSSClient
    from waspada.schema import RawLoans, validate_table

    client = OSSClient()
    # Small LIMIT keeps the smoke test cheap while still exercising the read path.
    table = client.fetch_loans(lane="collections", limit=10)

    assert isinstance(table, pa.Table)
    assert table.num_rows > 0
    assert table.num_rows <= 10
    # validate_table checks the RawLoans fields are present with compatible types.
    validate_table(table, RawLoans, name="fetch_loans result")
