"""QA — Data validation: schema/null/range + OSS ingest path + portfolio DQ.

Findings from these tests are summarized in tests/qa/REPORT.md under the
"Data validation" section. Each test names the finding ID it backs.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

import pyarrow as pa
import pyarrow.compute as pc
import pytest

from waspada.schema import FeatureFrame, RawLoans, validate_table
from waspada.features.collections import build_features, build_label

from .conftest import oss_configured

# Range bounds reasoned from the LendingClub-derived domain (documented in
# REPORT.md). Used to flag out-of-range synthetic values.
_DOMAIN_BOUNDS = {
    "amount": (0, None),          # strictly > 0
    "term": (1, 120),             # months; LC uses 36/60, allow headroom
    "rate": (0, 40),              # annual %, never negative, cap absurd
    "dti": (0, 100),              # ratio, 0..~100
    "annual_income": (0, None),   # strictly >= 0
    "outstanding_principal": (0, None),
    "total_paid": (0, None),
}


# ----------------------------------------------------------------- synthetic
class TestRawLoansContract:
    """The synthetic fixture must itself satisfy the contract (test-of-test)."""

    def test_raw_table_validates_as_rawloans_superset(self, raw_table):
        # If this fails, the QA fixture is broken before we test the build.
        validate_table(raw_table, RawLoans, name="qa raw_table")

    def test_raw_table_has_no_nulls(self, raw_table):
        offenders = []
        for f in dataclasses.fields(RawLoans):
            if pc.sum(pc.is_null(raw_table.column(f.name))).as_py() > 0:
                offenders.append(f.name)
        assert not offenders, f"nulls in RawLoans fields: {offenders}"

    def test_raw_table_numerics_in_domain(self, raw_table):
        """Flag negative or absurd values (F-DV-04 family)."""
        for col, (lo, hi) in _DOMAIN_BOUNDS.items():
            arr = raw_table.column(col)
            n_below = pc.sum(pc.less(arr, pa.scalar(lo))).as_py()
            assert n_below == 0, f"{col}: {n_below} rows below domain min {lo}"
            if hi is not None:
                n_above = pc.sum(pc.greater(arr, pa.scalar(hi))).as_py()
                assert n_above == 0, f"{col}: {n_above} rows above domain max {hi}"


class TestFeatureFrameContract:
    """build_features output must satisfy the FeatureFrame contract + DQ."""

    def test_build_features_validates_and_no_nulls(self, raw_table, as_of_date):
        out = build_features(raw_table, as_of=as_of_date)
        validate_table(out, FeatureFrame, name="qa featureframe")
        offenders = []
        for f in dataclasses.fields(FeatureFrame):
            if pc.sum(pc.is_null(out.column(f.name))).as_py() > 0:
                offenders.append(f.name)
        assert not offenders, f"nulls in FeatureFrame fields: {offenders}"

    def test_label_is_bool_and_two_classes_present(self, raw_table, as_of_date):
        out = build_features(raw_table, as_of=as_of_date)
        label = out.column("label_default")
        assert pa.types.is_boolean(label.type)
        distinct = set(label.to_pylist())
        # The fixture has both Charged Off/Default (True) and others (False).
        assert distinct == {True, False}, f"label should have both classes, got {distinct}"

    def test_payment_ratio_non_negative_finite(self, raw_table, as_of_date):
        out = build_features(raw_table, as_of=as_of_date)
        pr = out.column("payment_ratio").to_pylist()
        assert all(isinstance(x, float) for x in pr)
        assert all(x >= 0.0 for x in pr), "negative payment_ratio"
        # real data CAN exceed 1.0 (interest), so we only assert finiteness here.
        import math
        assert all(math.isfinite(x) for x in pr), "non-finite payment_ratio"

    def test_outstanding_ratio_in_unit_range(self, raw_table, as_of_date):
        # outstanding_principal <= amount should hold on a clean snapshot;
        # the BQ probe confirms 0 rows violate this on the live book.
        out = build_features(raw_table, as_of=as_of_date)
        orr = out.column("outstanding_ratio").to_pylist()
        assert all(0.0 <= x <= 1.0 for x in orr), f"outstanding_ratio out of [0,1]: {orr}"


# ---------------------------------------------------------------- live OSS
@pytest.mark.skipif(not oss_configured(),
                    reason="OSS creds not configured; live data-quality tests skipped")
class TestLiveOSSDataQuality:
    """Backs REPORT.md findings F-DV-* with numbers from the live 1M-row book.

    These run real (cheap LIMIT/aggregate) reads against the OSS bucket. They
    encode the data-quality invariants observed in the probe; if the upstream
    synthetic data regresses, these flag it.
    """

    def test_fetch_loans_returns_rawloans_superset(self, oss_env):
        from waspada.data import OSSClient
        client = OSSClient()
        table = client.fetch_loans(lane="collections", limit=50)
        assert isinstance(table, pa.Table)
        assert 0 < table.num_rows <= 50
        validate_table(table, RawLoans, name="live fetch_loans")

    def test_live_book_has_no_nulls_in_contract_fields(self, oss_env):
        from waspada.data import OSSClient
        client = OSSClient()
        # A 10k sample is enough to catch systemic null issues cheaply.
        table = client.fetch_loans(lane="collections", limit=10000)
        offenders = []
        for f in dataclasses.fields(RawLoans):
            if pc.sum(pc.is_null(table.column(f.name))).as_py() > 0:
                offenders.append(f.name)
        assert not offenders, f"nulls in live RawLoans fields: {offenders}"

    def test_live_amounts_and_ratios_are_domain_clean(self, oss_env):
        from waspada.data import OSSClient
        client = OSSClient()
        table = client.fetch_loans(lane="collections", limit=10000)
        amt = table.column("amount")
        assert pc.sum(pc.less_equal(amt, pa.scalar(0.0))).as_py() == 0, "amount <= 0"
        outp = table.column("outstanding_principal")
        # outstanding_principal must never exceed amount (no over-advance).
        assert pc.sum(pc.greater(outp, amt)).as_py() == 0, "outstanding_principal > amount"

    def test_live_label_distribution_via_full_read_not_biased_limit(self, oss_env):
        """F-DV-05 (MAJOR finding): the loan-portfolio object is physically
        clustered by ``current_status`` (offsets 0..88097 are all 'Charged
        Off'). Because ``fetch_loans(limit=N)`` reads the whole object then
        slices the first N rows client-side (no server-side query/ORDER BY),
        a LIMIT sample returns a **100%-default** slice for N <= 88098 — a
        wildly biased sample. The full-object default rate is ~14.2%. This
        test computes the rate over the *entire* object (unbiased) and
        asserts it is plausible; the biased LIMIT rate is documented in the
        finding. Any sample-based check that trusts ``fetch_loans(limit=N)``
        to be representative is wrong — see REPORT.md F-DV-05.
        """
        from waspada.data import OSSClient
        client = OSSClient()
        table = client.fetch_loans(lane="collections")  # no limit: the whole object
        label = build_label(table)
        n = len(label)
        pos = pc.sum(pc.cast(label, pa.int64())).as_py()
        rate = pos / n
        assert 0.05 < rate < 0.30, f"full-object default rate {rate:.3f} outside band"

    def test_live_limit_sample_is_biased_clustered_by_status(self, oss_env):
        """F-DV-05 (evidence): demonstrate the LIMIT bias concretely. A 5k LIMIT
        sample is ~100% default while the full object is ~14%. This is the
        repro for the finding — it is expected to show the bias, hence the
        assertion encodes the bias rather than failing on it."""
        from waspada.data import OSSClient
        client = OSSClient()
        biased = client.fetch_loans(lane="collections", limit=5000)
        label = build_label(biased)
        n = len(label)
        pos = pc.sum(pc.cast(label, pa.int64())).as_py()
        biased_rate = pos / n
        # Document: the LIMIT sample is ~100% default (clustered object).
        # If this ever drops below 0.9, the clustering changed and the finding
        # should be re-evaluated.
        assert biased_rate >= 0.9, (
            f"expected the LIMIT-bias repro to show >=90% default rate; got "
            f"{biased_rate:.3f}. Object row order may have changed — re-check "
            f"finding F-DV-05."
        )
