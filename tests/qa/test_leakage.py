"""QA — Data-leakage check: the critical gate.

Asserts that ``label_default`` is never derivable from a model feature, that
the ``delinquency_status`` proxy is excluded from the model matrix, and that
the vintage train/test windows do not overlap. Documents the cross-sectional
provenance reasoning for the payment-ratio features.

Findings -> tests/qa/REPORT.md "Data leakage" section.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter

import pyarrow as pa
import pytest

from waspada.features.collections import build_features, delinquency_bucket, is_default
from waspada.schema import FeatureFrame, RawLoans

from .conftest import synthetic_raw_rows


# ----------------------------------------------------------------- rule set
# The fields the FeatureFrame carries that must NEVER enter the model matrix.
# (Mirror of waspada.model.risk.LEAKAGE_EXCLUDED; duplication is intentional so
# the QA suite fails even if someone edits risk.py to loosen the guard.)
LEAKAGE_FIELDS = {
    "loan_id",               # identifier
    "delinquency_status",    # == delinquency_bucket(current_status) -> encodes label
    "label_default",         # the label itself
    "as_of_date",            # snapshot metadata, not predictive
}


class TestLabelNotDerivableFromFeatures:
    """F-LK-01: the label must not be a deterministic function of any feature."""

    def test_delinquency_status_is_deterministic_proxy_for_label(self):
        """F-LK-01 (documented finding): delinquency_bucket('Charged Off'/'Default')
        == 'Default', and is_default('Charged Off'/'Default') == True, so
        delinquency_status == 'Default' is a BIJECTION with label_default.
        It is therefore excluded from the model — this test pins the fact so a
        future change that promotes it to a feature fails loudly here.
        """
        for terminal in ("Charged Off", "Default"):
            assert delinquency_bucket(terminal) == "Default"
            assert is_default(terminal) is True
        for nonterminal in ("Current", "Fully Paid", "Late (16-30 days)",
                            "Late (31-120 days)", "In Grace Period"):
            assert delinquency_bucket(nonterminal) != "Default"
            assert is_default(nonterminal) is False

    def test_label_default_equals_is_default_of_current_status(self, raw_table, as_of_date):
        """F-LK-02: confirm the label is exactly is_default(current_status)
        (no hidden extra logic that could widen the leakage surface)."""
        out = build_features(raw_table, as_of=as_of_date)
        raw_status = [r["current_status"] for r in synthetic_raw_rows()]
        derived = [is_default(s) for s in raw_status]
        assert out.column("label_default").to_pylist() == derived


class TestModelFeatureMatrixIsLeakageSafe:
    """F-LK-03: the actual columns risk.py feeds the estimator must exclude
    the leakage set. This is the load-bearing guard."""

    def test_model_feature_columns_exclude_leakage_set(self):
        from waspada.model.risk import FEATURE_COLUMNS, LEAKAGE_EXCLUDED
        feats = set(FEATURE_COLUMNS)
        # None of the leakage fields may appear in the model matrix.
        leaked = feats & LEAKAGE_FIELDS
        assert not leaked, (
            f"LEAK: these leakage fields are model features: {leaked}. "
            "They encode or are the label."
        )
        # And the documented LEAKAGE_EXCLUDED in risk.py must cover our set.
        assert set(LEAKAGE_EXCLUDED) >= LEAKAGE_FIELDS - {"as_of_date"} or \
               set(LEAKAGE_EXCLUDED) >= LEAKAGE_FIELDS, (
            f"risk.py LEAKAGE_EXCLUDED={set(LEAKAGE_EXCLUDED)} does not cover "
            f"the required leakage set {LEAKAGE_FIELDS}"
        )

    def test_delinquency_status_not_in_numeric_or_categorical(self):
        """Belt-and-braces: explicitly assert the proxy is in neither tuple."""
        from waspada.model.risk import NUMERIC_FEATURES, CATEGORICAL_FEATURES
        assert "delinquency_status" not in NUMERIC_FEATURES
        assert "delinquency_status" not in CATEGORICAL_FEATURES

    def test_label_and_id_not_in_feature_columns(self):
        from waspada.model.risk import FEATURE_COLUMNS
        assert "label_default" not in FEATURE_COLUMNS
        assert "loan_id" not in FEATURE_COLUMNS


class TestPaymentRatioProvenance:
    """F-LK-04 (documented, not a blocker): payment_ratio and outstanding_ratio
    are derived from total_paid / outstanding_principal — cross-sectional
    snapshot totals, NOT post-outcome panel data. The owner's ruling
    (schema.py L20-27) establishes the source as a single snapshot.

    These tests document the reasoning and assert the ratios are computed
    from snapshot fields only (no future information)."""

    def test_payment_ratio_is_total_paid_over_amount(self, raw_table, as_of_date):
        out = build_features(raw_table, as_of=as_of_date)
        amounts = raw_table.column("amount").to_pylist()
        paid = raw_table.column("total_paid").to_pylist()
        got = out.column("payment_ratio").to_pylist()
        for a, p, g in zip(amounts, paid, got):
            assert abs(g - (p / a if a else 0.0)) < 1e-9

    def test_outstanding_ratio_is_outstanding_over_amount(self, raw_table, as_of_date):
        out = build_features(raw_table, as_of=as_of_date)
        amounts = raw_table.column("amount").to_pylist()
        outp = raw_table.column("outstanding_principal").to_pylist()
        got = out.column("outstanding_ratio").to_pylist()
        for a, o, g in zip(amounts, outp, got):
            assert abs(g - (o / a if a else 0.0)) < 1e-9


class TestVintageSplitIntegrity:
    """F-LK-05: train/test vintage windows must not overlap (out-of-time test)."""

    def test_vintage_split_windows_disjoint(self):
        from waspada.model.risk import _vintage_split
        # Build a multi-vintage FeatureFrame-shaped table via build_features.
        import dataclasses
        rows = synthetic_raw_rows()
        cols = {f.name: [] for f in dataclasses.fields(RawLoans)}
        for r in rows:
            for name in cols:
                cols[name].append(r[name])
        from waspada.schema import schema_from_dataclass
        raw = pa.table(cols, schema=schema_from_dataclass(RawLoans))
        frame = build_features(raw, as_of=dt.date(2024, 12, 1))

        train_idx, test_idx, split = _vintage_split(frame, 0.7)
        # Indices themselves must be disjoint.
        assert set(train_idx.tolist() if hasattr(train_idx, "tolist") else train_idx) \
               .isdisjoint(test_idx.tolist() if hasattr(test_idx, "tolist") else test_idx)
        # And vintage years must not overlap when the split is a real vintage split.
        if split.get("method") == "vintage":
            ty = set(split["train_years"]); ey = set(split["test_years"])
            assert ty.isdisjoint(ey), f"vintage years overlap: {ty & ey}"
        # If it fell back to shuffle (single cohort), document that in REPORT.
