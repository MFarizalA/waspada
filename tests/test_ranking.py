"""Ranking, segmentation & alert tests (WA-006 acceptance).

Synthetic scored table, asserts: top-N order + recommended_action by band,
segment math (NPL ratio + vintage default rate + status mix), an alert fires
on a deteriorating cohort, and the assembled payload is JSON-serializable and
shape-matches the DashboardPayload contract.
"""
from __future__ import annotations

import json
from typing import List

import pyarrow as pa
import pytest

from waspada.insight.ranking import (
    ACTION_BY_BAND,
    alerts,
    rank,
    segment_health,
    summarize_alerts,
    to_dashboard_payload,
)
from waspada.schema import ScoredAccounts, schema_from_dataclass


# --------------------------------------------------------------------------- #
# Helpers — build a ScoredAccounts table (contract + the extra columns the
# model layer appends for the insight layer).
# --------------------------------------------------------------------------- #
def _scored_table(rows: list[dict]) -> pa.Table:
    """Assemble a ScoredAccounts-contract table + insight extras.

    Required contract columns: loan_id, p_default, score_band, segment,
    recommended_action. Extra (non-contract) columns carried for the insight
    layer: issue_year, delinquency_status, label_default.
    """
    seg_type = schema_from_dataclass(ScoredAccounts).field("segment").type
    base = pa.table(
        {
            "loan_id": pa.array([r["loan_id"] for r in rows], type=pa.string()),
            "p_default": pa.array([r["p_default"] for r in rows], type=pa.float64()),
            "score_band": pa.array([r["score_band"] for r in rows], type=pa.string()),
            "segment": pa.array([r["segment"] for r in rows], type=seg_type),
            "recommended_action": pa.array(
                [r.get("recommended_action", "") for r in rows], type=pa.string()
            ),
        },
        schema=schema_from_dataclass(ScoredAccounts),
    )
    return (
        base
        .append_column("issue_year", pa.array([r["issue_year"] for r in rows], type=pa.int64()))
        .append_column("delinquency_status", pa.array([r["delinquency_status"] for r in rows], type=pa.string()))
        .append_column("label_default", pa.array([r["label_default"] for r in rows], type=pa.bool_()))
    )


@pytest.fixture
def scored_mixed() -> pa.Table:
    """10 rows across all five risk levels, two vintages, mixed delinquency/default."""
    rows = [
        # High-risk (Very High), default cohort 2022
        dict(loan_id="H1", p_default=0.95, score_band="Very High",
             segment={"product": "debt_consolidation", "region": "West"},
             issue_year=2022, delinquency_status="Default", label_default=True),
        dict(loan_id="H2", p_default=0.91, score_band="Very High",
             segment={"product": "credit_card", "region": "South"},
             issue_year=2022, delinquency_status="Default", label_default=True),
        # Mid (Medium/High), watch band, mixed delinquency
        dict(loan_id="M3", p_default=0.60, score_band="High",
             segment={"product": "car", "region": "Midwest"},
             issue_year=2022, delinquency_status="31-120", label_default=False),
        dict(loan_id="M4", p_default=0.55, score_band="Medium",
             segment={"product": "medical", "region": "Northeast"},
             issue_year=2023, delinquency_status="0", label_default=False),
        dict(loan_id="M5", p_default=0.50, score_band="Medium",
             segment={"product": "car", "region": "West"},
             issue_year=2023, delinquency_status="16-30", label_default=False),
        # Low (Very Low/Low), auto-cure band, performing
        dict(loan_id="L6", p_default=0.20, score_band="Low",
             segment={"product": "credit_card", "region": "South"},
             issue_year=2023, delinquency_status="0", label_default=False),
        dict(loan_id="L7", p_default=0.10, score_band="Very Low",
             segment={"product": "home_improvement", "region": "Midwest"},
             issue_year=2023, delinquency_status="0", label_default=False),
        dict(loan_id="L8", p_default=0.05, score_band="Very Low",
             segment={"product": "car", "region": "Northeast"},
             issue_year=2023, delinquency_status="0", label_default=False),
        # 2021 vintage — fully performing, low default rate (control cohort)
        dict(loan_id="L9", p_default=0.08, score_band="Very Low",
             segment={"product": "credit_card", "region": "West"},
             issue_year=2021, delinquency_status="0", label_default=False),
        dict(loan_id="L10", p_default=0.12, score_band="Low",
             segment={"product": "medical", "region": "South"},
             issue_year=2021, delinquency_status="0", label_default=False),
    ]
    return _scored_table(rows)


# --------------------------------------------------------------------------- #
# rank — ordering, recommended_action by band, top_n cap, determinism.
# --------------------------------------------------------------------------- #
def test_rank_orders_by_p_default_desc(scored_mixed):
    wl = rank(scored_mixed, top_n=10)
    probs = [r["p_default"] for r in wl]
    assert probs == sorted(probs, reverse=True)


def test_rank_attaches_recommended_action_by_band(scored_mixed):
    wl = rank(scored_mixed, top_n=10)
    by_loan = {r["loan_id"]: r for r in wl}
    # Very High → call, Medium/High → watch, Very Low/Low → auto-cure
    assert by_loan["H1"]["recommended_action"] == "call"
    assert by_loan["M3"]["recommended_action"] == "watch"
    assert by_loan["M4"]["recommended_action"] == "watch"
    assert by_loan["L6"]["recommended_action"] == "auto-cure"
    assert by_loan["L7"]["recommended_action"] == "auto-cure"


def test_rank_caps_at_top_n(scored_mixed):
    wl = rank(scored_mixed, top_n=3)
    assert len(wl) == 3
    # Top 3 by p_default are H1, H2, M3.
    assert [r["loan_id"] for r in wl] == ["H1", "H2", "M3"]


def test_rank_deterministic_on_ties():
    """Equal p_default → sorted by loan_id asc (stable, reproducible)."""
    rows = [
        dict(loan_id="B", p_default=0.5, score_band="Medium",
             segment={"product": "x", "region": "y"},
             issue_year=2023, delinquency_status="0", label_default=False),
        dict(loan_id="A", p_default=0.5, score_band="Medium",
             segment={"product": "x", "region": "y"},
             issue_year=2023, delinquency_status="0", label_default=False),
    ]
    wl = rank(_scored_table(rows), top_n=10)
    assert [r["loan_id"] for r in wl] == ["A", "B"]


def test_rank_work_list_records_have_contract_shape(scored_mixed):
    """Each work-list record carries the ScoredAccounts JSON shape."""
    wl = rank(scored_mixed, top_n=5)
    for rec in wl:
        assert set(rec.keys()) == {"loan_id", "p_default", "score_band", "segment", "recommended_action"}
        assert set(rec["segment"].keys()) == {"product", "region"}
        assert rec["recommended_action"] in {"call", "watch", "auto-cure"}


# --------------------------------------------------------------------------- #
# segment_health — NPL ratio, vintage default rate, status mix.
# --------------------------------------------------------------------------- #
def test_segment_health_npl_ratio(scored_mixed):
    health = segment_health(scored_mixed)
    # NPL buckets = {Default, 31-120, 16-30}. In fixture: H1,H2 (Default),
    # M3 (31-120), M5 (16-30) → 4 of 10 → 0.4
    assert health["npl_ratio"] == pytest.approx(0.4, rel=1e-9)


def test_segment_health_vintage_default_rate(scored_mixed):
    health = segment_health(scored_mixed)
    vdr = health["vintage_default_rate"]
    # 2022: H1,H2 default, M3 not → 2/3 ; 2023: all 4 False → 0/4 ;
    # 2021: both False → 0/2
    assert vdr["2022"] == pytest.approx(2 / 3, rel=1e-9)
    assert vdr["2023"] == pytest.approx(0.0, rel=1e-9)
    assert vdr["2021"] == pytest.approx(0.0, rel=1e-9)


def test_segment_health_status_mix_sums_to_one(scored_mixed):
    health = segment_health(scored_mixed)
    mix = health["status_mix"]
    assert sum(mix.values()) == pytest.approx(1.0, rel=1e-9)
    # Two Defaults in the fixture.
    assert mix["Default"] == pytest.approx(0.2, rel=1e-9)


def test_segment_health_empty_table():
    from waspada.schema import ScoredAccounts, schema_from_dataclass

    seg_type = schema_from_dataclass(ScoredAccounts).field("segment").type
    empty = pa.table(
        {
            "loan_id": pa.array([], type=pa.string()),
            "p_default": pa.array([], type=pa.float64()),
            "score_band": pa.array([], type=pa.string()),
            "segment": pa.array([], type=seg_type),
            "recommended_action": pa.array([], type=pa.string()),
        },
        schema=schema_from_dataclass(ScoredAccounts),
    )
    health = segment_health(empty)
    assert health == {"npl_ratio": 0.0, "vintage_default_rate": {}, "status_mix": {}}


# --------------------------------------------------------------------------- #
# alerts — fires on a deteriorating cohort; respects thresholds.
# --------------------------------------------------------------------------- #
def test_alert_fires_on_deteriorating_vintage(scored_mixed):
    """2022 cohort default rate (2/3 ≈ 67%) ≥ 15% threshold → alert."""
    health = segment_health(scored_mixed)
    al = alerts(health, npl_threshold=0.9, vintage_threshold=0.15)
    vintage_alerts = [a for a in al if a["metric"] == "vintage_default_rate"]
    years_flagged = {a["segment"]["vintage"] for a in vintage_alerts}
    assert "2022" in years_flagged
    assert "2023" not in years_flagged  # 0% default rate


def test_alert_fires_on_portfolio_npl(scored_mixed):
    """NPL ratio 0.4 ≥ 0.2 threshold → portfolio NPL alert."""
    health = segment_health(scored_mixed)
    al = alerts(health, npl_threshold=0.2, vintage_threshold=0.99)
    npl_alerts = [a for a in al if a["metric"] == "npl_ratio"]
    assert npl_alerts and npl_alerts[0]["value"] == pytest.approx(0.4, rel=1e-9)


def test_alerts_respect_thresholds_clean_portfolio():
    """A clean portfolio (no NPL, no defaults) fires no alerts."""
    rows = [
        dict(loan_id="C1", p_default=0.1, score_band="Very Low",
             segment={"product": "x", "region": "y"},
             issue_year=2023, delinquency_status="0", label_default=False),
    ]
    health = segment_health(_scored_table(rows))
    al = alerts(health)
    assert al == []


def test_summarize_alerts_always_returns_string(scored_mixed):
    health = segment_health(scored_mixed)
    al = alerts(health, vintage_threshold=0.15)
    s1 = summarize_alerts(al)
    s0 = summarize_alerts([])
    assert isinstance(s1, str) and s1
    assert isinstance(s0, str) and s0


# --------------------------------------------------------------------------- #
# to_dashboard_payload — JSON-serializable, matches contract.
# --------------------------------------------------------------------------- #
def test_dashboard_payload_is_json_serializable(scored_mixed):
    wl = rank(scored_mixed, top_n=5)
    health = segment_health(scored_mixed)
    al = alerts(health, vintage_threshold=0.15)
    payload = to_dashboard_payload(wl, health, al)
    # Round-trip through json (the function already does this internally,
    # but assert it explicitly as the contract guarantee).
    s = json.dumps(payload)
    back = json.loads(s)
    assert set(back.keys()) == {"work_list", "portfolio_health", "alerts"}


def test_dashboard_payload_has_required_keys(scored_mixed):
    wl = rank(scored_mixed, top_n=5)
    health = segment_health(scored_mixed)
    al = alerts(health)
    payload = to_dashboard_payload(wl, health, al)
    assert "work_list" in payload and isinstance(payload["work_list"], list)
    ph = payload["portfolio_health"]
    assert set(ph.keys()) == {"npl_ratio", "vintage_default_rate", "status_mix"}
    assert isinstance(payload["alerts"], list)


def test_dashboard_payload_matches_sample_fixture_shape():
    """The payload shape matches the existing sample-payload.json the frontend
    already renders (so the orchestrator's output is drop-in for the dashboard)."""
    from waspada.schema import ScoredAccounts, schema_from_dataclass

    seg_type = schema_from_dataclass(ScoredAccounts).field("segment").type
    rows = [
        dict(loan_id="X1", p_default=0.9, score_band="Very High",
             segment={"product": "credit_card", "region": "DKI Jakarta"},
             issue_year=2023, delinquency_status="Default", label_default=True),
    ]
    scored = pa.table(
        {
            "loan_id": pa.array(["X1"], type=pa.string()),
            "p_default": pa.array([0.9], type=pa.float64()),
            "score_band": pa.array(["Very High"], type=pa.string()),
            "segment": pa.array([{"product": "credit_card", "region": "DKI Jakarta"}], type=seg_type),
            "recommended_action": pa.array([""], type=pa.string()),
        },
        schema=schema_from_dataclass(ScoredAccounts),
    ).append_column("issue_year", pa.array([2023], type=pa.int64())) \
     .append_column("delinquency_status", pa.array(["Default"], type=pa.string())) \
     .append_column("label_default", pa.array([True], type=pa.bool_()))

    payload = to_dashboard_payload(rank(scored, top_n=1), segment_health(scored), alerts(segment_health(scored)))
    rec = payload["work_list"][0]
    # Same keys as the dashboard's sample-payload.json work-list entries.
    assert set(rec.keys()) == {"loan_id", "p_default", "score_band", "segment", "recommended_action"}
    assert set(rec["segment"].keys()) == {"product", "region"}


# --------------------------------------------------------------------------- #
# WA-024 — Expected Loss (PD × LGD × EAD) per-account + portfolio total.
# --------------------------------------------------------------------------- #
from waspada.insight.ranking import EXPECTED_LOSS_LGD


def _scored_table_with_op(rows: list[dict]) -> pa.Table:
    """ScoredAccounts table WITH outstanding_principal (WA-024 carry-forward)."""
    seg_type = schema_from_dataclass(ScoredAccounts).field("segment").type
    base = pa.table(
        {
            "loan_id": pa.array([r["loan_id"] for r in rows], type=pa.string()),
            "p_default": pa.array([r["p_default"] for r in rows], type=pa.float64()),
            "score_band": pa.array([r["score_band"] for r in rows], type=pa.string()),
            "segment": pa.array([r["segment"] for r in rows], type=seg_type),
            "recommended_action": pa.array(
                [r.get("recommended_action", "") for r in rows], type=pa.string()
            ),
        },
        schema=schema_from_dataclass(ScoredAccounts),
    )
    return (
        base
        .append_column("issue_year", pa.array([r["issue_year"] for r in rows], type=pa.int64()))
        .append_column("delinquency_status", pa.array([r["delinquency_status"] for r in rows], type=pa.string()))
        .append_column("label_default", pa.array([r["label_default"] for r in rows], type=pa.bool_()))
        .append_column("outstanding_principal", pa.array([r["outstanding_principal"] for r in rows], type=pa.float64()))
    )


@pytest.fixture
def scored_with_el() -> pa.Table:
    """Scored table with outstanding_principal for EL tests."""
    rows = [
        dict(loan_id="EL1", p_default=0.90, score_band="Very High",
             segment={"product": "card", "region": "West"},
             issue_year=2022, delinquency_status="Default", label_default=True,
             outstanding_principal=10000.0),
        dict(loan_id="EL2", p_default=0.50, score_band="Medium",
             segment={"product": "auto", "region": "East"},
             issue_year=2023, delinquency_status="0", label_default=False,
             outstanding_principal=4000.0),
        dict(loan_id="EL3", p_default=0.10, score_band="Very Low",
             segment={"product": "card", "region": "West"},
             issue_year=2023, delinquency_status="0", label_default=False,
             outstanding_principal=2000.0),
    ]
    return _scored_table_with_op(rows)


def test_expected_loss_lgd_constant():
    """LGD is 45% (the labeled assumption)."""
    assert EXPECTED_LOSS_LGD == 0.45


def test_rank_adds_expected_loss_when_op_present(scored_with_el):
    """WA-024: work-list rows carry expected_loss when outstanding_principal exists."""
    wl = rank(scored_with_el, top_n=3)
    for rec in wl:
        assert "expected_loss" in rec
    # EL1: 0.90 × 0.45 × 10000 = 4050.0
    by_id = {r["loan_id"]: r for r in wl}
    assert by_id["EL1"]["expected_loss"] == pytest.approx(0.90 * 0.45 * 10000.0)
    assert by_id["EL2"]["expected_loss"] == pytest.approx(0.50 * 0.45 * 4000.0)
    assert by_id["EL3"]["expected_loss"] == pytest.approx(0.10 * 0.45 * 2000.0)


def test_rank_omits_expected_loss_when_op_absent(scored_mixed):
    """WA-024: no outstanding_principal → no expected_loss key (older payloads valid)."""
    wl = rank(scored_mixed, top_n=5)
    for rec in wl:
        assert "expected_loss" not in rec


def test_segment_health_includes_expected_loss(scored_with_el):
    """WA-024: portfolio_health carries total_expected_loss (the portfolio sum —
    distinct from each row's expected_loss, and the key the frontend reads)."""
    health = segment_health(scored_with_el)
    assert "total_expected_loss" in health
    expected = (
        0.90 * 0.45 * 10000.0 +
        0.50 * 0.45 * 4000.0 +
        0.10 * 0.45 * 2000.0
    )
    assert health["total_expected_loss"] == pytest.approx(expected)


def test_segment_health_omits_expected_loss_without_op(scored_mixed):
    """WA-024: no outstanding_principal → no total_expected_loss in health."""
    health = segment_health(scored_mixed)
    assert "total_expected_loss" not in health


def test_dashboard_payload_includes_expected_loss(scored_with_el):
    """WA-024: to_dashboard_payload forwards total_expected_loss in
    portfolio_health under the key the frontend + fixture use."""
    wl = rank(scored_with_el, top_n=3)
    health = segment_health(scored_with_el)
    al = alerts(health)
    payload = to_dashboard_payload(wl, health, al)
    assert "total_expected_loss" in payload["portfolio_health"]
    # Work-list rows also carry the per-account expected_loss.
    assert all("expected_loss" in r for r in payload["work_list"])


def test_dashboard_payload_without_el_still_valid(scored_mixed):
    """WA-024: older payloads without EL stay valid (no key, no crash)."""
    wl = rank(scored_mixed, top_n=5)
    health = segment_health(scored_mixed)
    payload = to_dashboard_payload(wl, health, alerts(health))
    assert "total_expected_loss" not in payload["portfolio_health"]
    assert all("expected_loss" not in r for r in payload["work_list"])
    # Still JSON-serializable.
    json.dumps(payload)


def test_expected_loss_zero_outstanding():
    """WA-024: an account with zero outstanding has zero expected_loss."""
    rows = [
        dict(loan_id="Z1", p_default=0.99, score_band="Very High",
             segment={"product": "x", "region": "y"},
             issue_year=2023, delinquency_status="0", label_default=False,
             outstanding_principal=0.0),
    ]
    scored = _scored_table_with_op(rows)
    wl = rank(scored, top_n=1)
    assert wl[0]["expected_loss"] == 0.0
    health = segment_health(scored)
    assert health["total_expected_loss"] == 0.0
