"""QA — Pipeline integration: ingest→features→model→ranking→payload, and the
DashboardPayload contract against the dashboard fixture.

Drives the full chain on the synthetic fixture (the same shape the build tests
use) and asserts the payload the frontend consumes validates against the frozen
contract and matches the committed dashboard fixture's shape.

Findings -> tests/qa/REPORT.md "Pipeline integration" section.
"""
from __future__ import annotations

import datetime as dt
import json
import dataclasses
from pathlib import Path

import pyarrow as pa
import pytest

from waspada.schema import (
    DashboardPayload, RawLoans, schema_from_dataclass, validate_table,
)
from waspada.features.collections import build_features

from .conftest import synthetic_raw_rows, _REPO_ROOT  # type: ignore[attr-defined]


def _full_pipeline(frame):
    """Run features→model→rank→payload and return the DashboardPayload dict."""
    from waspada.model.risk import train, predict
    from waspada.insight.ranking import (
        rank, segment_health, alerts, to_dashboard_payload,
    )
    model = train(frame)
    scored = predict(model, frame)
    work_list = rank(scored, top_n=10)
    health = segment_health(scored)
    alert_list = alerts(health)
    return to_dashboard_payload(work_list, health, alert_list)


class TestEndToEndPipeline:
    def test_ingest_to_features_to_payload_round_trip(self, raw_table, as_of_date):
        frame = build_features(raw_table, as_of=as_of_date)
        validate_table(frame, __import__("waspada.schema", fromlist=["FeatureFrame"]).FeatureFrame,
                       name="integration featureframe")
        payload = _full_pipeline(frame)
        # The three required DashboardPayload keys.
        assert set(payload.keys()) >= {"work_list", "portfolio_health", "alerts"}
        assert isinstance(payload["work_list"], list)
        assert payload["portfolio_health"]["npl_ratio"] >= 0.0
        # JSON-serializable end to end (to_dashboard_payload guarantees this).
        json.dumps(payload)

    def test_payload_work_list_records_have_contract_shape(self, raw_table, as_of_date):
        frame = build_features(raw_table, as_of=as_of_date)
        payload = _full_pipeline(frame)
        expected = {"loan_id", "p_default", "score_band", "segment", "recommended_action"}
        for rec in payload["work_list"]:
            assert expected <= set(rec.keys()), f"row missing fields: {expected - set(rec)}"
            assert isinstance(rec["segment"], dict)
            assert {"product", "region"} <= set(rec["segment"].keys())

    def test_full_chain_produces_actions_in_allowed_set(self, raw_table, as_of_date):
        frame = build_features(raw_table, as_of=as_of_date)
        payload = _full_pipeline(frame)
        allowed = {"call", "watch", "auto-cure"}
        for rec in payload["work_list"]:
            assert rec["recommended_action"] in allowed


class TestDashboardPayloadAgainstFixture:
    """The committed dashboard fixture must match the live pipeline output shape."""

    @pytest.fixture(scope="class")
    @classmethod
    def fixture_payload(cls):
        path = _REPO_ROOT / "dashboard/fixtures/sample-payload.json"
        return json.loads(path.read_text())

    def test_fixture_has_required_top_level_keys(self, fixture_payload):
        assert {"work_list", "portfolio_health", "alerts"} <= set(fixture_payload.keys())

    def test_fixture_work_list_records_match_contract(self, fixture_payload):
        """F-PI-01: every work_list row has the ScoredAccount fields (additive keys OK)."""
        required = {"loan_id", "p_default", "score_band", "segment", "recommended_action"}
        for rec in fixture_payload["work_list"]:
            assert required <= set(rec.keys()), (
                f"fixture row keys {set(rec.keys())} missing required {required}"
            )

    def test_fixture_p_default_in_unit_interval(self, fixture_payload):
        for rec in fixture_payload["work_list"]:
            assert 0.0 <= rec["p_default"] <= 1.0, rec["p_default"]

    def test_fixture_actions_in_allowed_set(self, fixture_payload):
        allowed = {"call", "watch", "auto-cure"}
        for rec in fixture_payload["work_list"]:
            assert rec["recommended_action"] in allowed

    def test_fixture_status_mix_sums_to_one(self, fixture_payload):
        """F-PI-02 (minor): portfolio_health.status_mix should sum to ~1.0."""
        sm = fixture_payload["portfolio_health"]["status_mix"]
        total = sum(sm.values())
        assert abs(total - 1.0) < 1e-6, f"status_mix sums to {total}, not 1.0"

    def test_fixture_all_one_band_is_a_real_run_not_placeholder(self, fixture_payload):
        """F-PI-03 (documented): the fixture's work_list is all Very High / 'call' with
        p_default in a tight [0.97,0.98] band. This is the top-N of a real
        scoring run (rank() sorts p_default desc), NOT the old placeholder
        (which was p_default=1.0 for every row). Assert the old placeholder
        shape is gone."""
        probs = [r["p_default"] for r in fixture_payload["work_list"]]
        assert not all(p == 1.0 for p in probs), "fixture reverted to p=1.0 placeholder"
        # Sorted descending within a believable band.
        assert probs == sorted(probs, reverse=True), "work_list not sorted by p_default desc"

    @pytest.mark.xfail(
        reason="F-PI-04 (minor finding): vintage_default_rate cohort keys include "
               "'2024' but the source data issue_date spans 2021-2023 only. This is "
               "issue_year_from_frame() month-floor reconstruction drift. Flips to "
               "XPASS when the reconstruction is corrected.",
        strict=False,
    )
    def test_fixture_vintage_keys_are_plausible(self, fixture_payload):
        """F-PI-04 (minor finding): vintage_default_rate contains a '2024' cohort
        key but the live book's issue_date max is 2023-12-31. The '2024' key is
        an artifact of issue_year reconstruction from loan_age (floor division).
        xfail sentinel — documents the finding; flips to XPASS if fixed."""
        vdr = fixture_payload["portfolio_health"]["vintage_default_rate"]
        unexpected = set(vdr.keys()) - {"2021", "2022", "2023"}
        assert not unexpected, (
            f"vintage_default_rate has cohort keys {unexpected} not present in "
            f"the source data (issue_date spans 2021-2023 only)."
        )


class TestAlertSegmentShape:
    """F-PI-05 (finding): the Python contract's Alert.segment is permissive
    (Optional[Dict[str,str]]), but ranking.py emits vintage alerts with
    segment={'vintage': '<year>'} which has no product/region keys, while the
    TS types.ts narrows segment to `Segment | null` ({product, region})."""

    def test_python_contract_allows_vintage_segment_dict(self):
        """The Python side accepts any Dict[str,str], so a vintage alert is
        contract-legal in Python. This test documents that the mismatch only
        bites on the TS side."""
        from waspada.insight.ranking import to_dashboard_payload
        # Build a payload with a vintage-shaped segment.
        payload = to_dashboard_payload(
            work_list=[],
            health={"npl_ratio": 0.0, "vintage_default_rate": {}, "status_mix": {}},
            alert_list=[{
                "metric": "vintage_default_rate", "value": 0.2, "threshold": 0.15,
                "message": "x", "segment": {"vintage": "2023"},
            }],
        )
        assert payload["alerts"][0]["segment"] == {"vintage": "2023"}
        # And it's JSON-serializable.
        json.dumps(payload)
