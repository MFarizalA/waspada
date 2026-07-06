"""WA-001 AC: the four contract types round-trip cleanly (schema is the seam).

Covers the explicit acceptance criterion: "a unit test round-trips a
FeatureFrame -> parquet -> back." Also exercises the full
RawLoans -> FeatureFrame -> ScoredAccounts -> DashboardPayload chain so a
silent field/type change anywhere in the contract fails here.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq

from waspada.schema import (
    Alert,
    DashboardPayload,
    FeatureFrame,
    PortfolioHealth,
    RawLoans,
    ScoredAccounts,
    Segment,
)


def test_rawloans_required_fields():
    """RawLoans has exactly the 13 frozen fields, by name."""
    expected = {
        "loan_id",
        "amount",
        "term",
        "rate",
        "grade",
        "annual_income",
        "dti",
        "issue_date",
        "purpose",
        "region",
        "outstanding_principal",
        "total_paid",
        "current_status",
    }
    assert {f.name for f in RawLoans.__dataclass_fields__.values()} == expected


def test_featureframe_required_fields():
    """FeatureFrame has exactly the frozen fields (loan_id + 7 features + label + date)."""
    expected = {
        "loan_id",
        "loan_age",
        "payment_ratio",
        "outstanding_ratio",
        "delinquency_status",
        "dti",
        "grade",
        "term",
        "label_default",
        "as_of_date",
    }
    assert {f.name for f in FeatureFrame.__dataclass_fields__.values()} == expected


def test_featureframe_parquet_roundtrip(tmp_path, sample_featureframe):
    """AC: a FeatureFrame round-trips through parquet and back unchanged."""
    ff = sample_featureframe
    rows = [
        {
            "loan_id": ff.loan_id,
            "loan_age": ff.loan_age,
            "payment_ratio": ff.payment_ratio,
            "outstanding_ratio": ff.outstanding_ratio,
            "delinquency_status": ff.delinquency_status,
            "dti": ff.dti,
            "grade": ff.grade,
            "term": ff.term,
            "label_default": ff.label_default,
            "as_of_date": ff.as_of_date,
        }
    ]

    table = pa.Table.from_pylist(rows)
    path = tmp_path / "features.parquet"
    pq.write_table(table, path)

    read_back = pq.read_table(path).to_pylist()
    assert len(read_back) == 1
    got = read_back[0]

    rebuilt = FeatureFrame(
        loan_id=got["loan_id"],
        loan_age=got["loan_age"],
        payment_ratio=got["payment_ratio"],
        outstanding_ratio=got["outstanding_ratio"],
        delinquency_status=got["delinquency_status"],
        dti=got["dti"],
        grade=got["grade"],
        term=got["term"],
        label_default=bool(got["label_default"]),
        as_of_date=got["as_of_date"],
    )
    assert rebuilt == ff


def test_scoredaccounts_segment_shape():
    """ScoredAccounts carries a Segment(product, region) -- the dashboard grouping key."""
    s = ScoredAccounts(
        loan_id="L-0001",
        p_default=0.72,
        score_band="high",
        segment=Segment(product="installment", region="West"),
        recommended_action="prioritize",
    )
    assert s.segment.product == "installment"
    assert s.segment.region == "West"
    assert 0.0 <= s.p_default <= 1.0


def test_dashboard_payload_assembles(sample_featureframe):
    """The full chain assembles into a DashboardPayload with the frozen shape."""
    scored = ScoredAccounts(
        loan_id=sample_featureframe.loan_id,
        p_default=0.72,
        score_band="high",
        segment=Segment(product="installment", region="West"),
        recommended_action="prioritize",
    )
    payload = DashboardPayload(
        work_list=[scored],
        portfolio_health=PortfolioHealth(
            npl_ratio=0.08,
            vintage_default_rate=0.12,
            status_mix={"Current": 0.70, "Late (31-120)": 0.12, "Default": 0.08, "Fully Paid": 0.10},
        ),
        alerts=[Alert(level="warn", message="West installment vintage above threshold")],
    )
    assert len(payload.work_list) == 1
    assert payload.work_list[0].loan_id == sample_featureframe.loan_id
    assert {"npl_ratio", "vintage_default_rate", "status_mix"} <= set(
        payload.portfolio_health.__dict__.keys()
    )
    assert payload.alerts[0].level == "warn"
