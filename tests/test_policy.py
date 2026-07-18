"""WA-032 acceptance — the human-configurable RiskPolicy decision matrix.

The invariants:
  * ``RiskPolicy.default()`` equals the code constants it replaces, so a run with
    no policy file is byte-identical to today (the regression anchor).
  * ``load_policy`` reads + validates the committed JSON; drift (bad action /
    band / threshold) fails loud with ``ValueError``.
  * Editing the matrix changes ``rank()`` output and editing thresholds changes
    ``alerts()`` output — with **no code change**.
"""
from __future__ import annotations

import json

import pyarrow as pa
import pytest

from waspada.insight.ranking import (
    ACTION_BY_BAND,
    DEFAULT_NPL_THRESHOLD,
    DEFAULT_VINTAGE_THRESHOLD,
    _NPL_BUCKETS,
    alerts,
    rank,
    segment_health,
)
from waspada.policy import DEFAULT_POLICY_PATH, RiskPolicy, load_policy


# --------------------------------------------------------------------------- #
# default() == the constants it replaces
# --------------------------------------------------------------------------- #
def test_default_policy_equals_the_code_constants():
    p = RiskPolicy.default()
    assert p.band_to_action == ACTION_BY_BAND
    assert p.npl_threshold == DEFAULT_NPL_THRESHOLD
    assert p.vintage_threshold == DEFAULT_VINTAGE_THRESHOLD
    assert p.npl_buckets == frozenset(_NPL_BUCKETS)


def test_committed_default_file_matches_default():
    """The shipped default_policy.json must equal RiskPolicy.default() — the file
    a human edits starts from the exact current behaviour."""
    loaded = load_policy(str(DEFAULT_POLICY_PATH))
    assert loaded == RiskPolicy.default()


def test_load_policy_none_reads_the_committed_default():
    assert load_policy(None) == RiskPolicy.default()


# --------------------------------------------------------------------------- #
# Validation — fail loud on drift
# --------------------------------------------------------------------------- #
def _write(tmp_path, obj) -> str:
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


def test_rejects_out_of_vocabulary_action(tmp_path):
    path = _write(tmp_path, {"band_to_action": {"Very High": "shred"}})
    with pytest.raises(ValueError, match="action"):
        load_policy(path)


def test_rejects_unknown_band(tmp_path):
    path = _write(tmp_path, {"band_to_action": {"Extreme": "call"}})
    with pytest.raises(ValueError, match="band"):
        load_policy(path)


def test_rejects_out_of_range_threshold(tmp_path):
    path = _write(tmp_path, {"band_to_action": {"High": "call"}, "npl_threshold": 1.5})
    with pytest.raises(ValueError, match="npl_threshold"):
        load_policy(path)


def test_missing_explicit_file_fails_loud():
    with pytest.raises(ValueError, match="not found"):
        load_policy("does/not/exist.json")


# --------------------------------------------------------------------------- #
# Editing the policy changes behaviour — no code change
# --------------------------------------------------------------------------- #
def _scored(rows):
    return pa.table({
        "loan_id": pa.array([r["loan_id"] for r in rows], pa.string()),
        "p_default": pa.array([r["p"] for r in rows], pa.float64()),
        "score_band": pa.array([r["band"] for r in rows], pa.string()),
        "segment": pa.array([{"product": "card", "region": "West"} for _ in rows]),
        "recommended_action": pa.array(["" for _ in rows], pa.string()),
        "delinquency_status": pa.array([r.get("d", "Current") for r in rows], pa.string()),
        "label_default": pa.array([r.get("label", False) for r in rows], pa.bool_()),
        "issue_year": pa.array([2023 for _ in rows], pa.int64()),
    })


def test_editing_band_to_action_changes_rank_output(tmp_path):
    scored = _scored([{"loan_id": "L1", "p": 0.8, "band": "High"}])

    # Default: High → watch.
    assert rank(scored)[0]["recommended_action"] == "watch"

    # A policy that escalates High → call, with no code change.
    path = _write(tmp_path, {
        "band_to_action": {**ACTION_BY_BAND, "High": "call"},
        "npl_threshold": DEFAULT_NPL_THRESHOLD,
        "vintage_threshold": DEFAULT_VINTAGE_THRESHOLD,
        "npl_buckets": list(_NPL_BUCKETS),
    })
    pol = load_policy(path)
    assert rank(scored, action_by_band=pol.band_to_action)[0]["recommended_action"] == "call"


def test_custom_npl_threshold_changes_alerts(tmp_path):
    # 25% of the book is in default → npl_ratio 0.25.
    rows = [{"loan_id": f"L{i}", "p": 0.5, "band": "Medium",
             "d": "Default" if i == 0 else "Current"} for i in range(4)]
    health = segment_health(_scored(rows))
    assert health["npl_ratio"] == pytest.approx(0.25)

    # Default threshold 0.20 → the 0.25 NPL fires an alert.
    assert any(a["metric"] == "npl_ratio" for a in alerts(health))
    # A stricter policy (threshold 0.30) → no NPL alert, no code change.
    assert not any(a["metric"] == "npl_ratio"
                   for a in alerts(health, npl_threshold=0.30))


def test_custom_npl_buckets_change_the_ratio():
    rows = [{"loan_id": "L1", "p": 0.5, "band": "Medium", "d": "16-30"},
            {"loan_id": "L2", "p": 0.5, "band": "Medium", "d": "Current"}]
    scored = _scored(rows)
    # Default buckets count 16-30 as NPL → ratio 0.5.
    assert segment_health(scored)["npl_ratio"] == pytest.approx(0.5)
    # A policy that only counts hard Default → 16-30 no longer NPL → ratio 0.0.
    assert segment_health(scored, npl_buckets=frozenset({"Default"}))["npl_ratio"] == 0.0


# --------------------------------------------------------------------------- #
# End-to-end: the policy reaches the payload through InsightAgent
# --------------------------------------------------------------------------- #
def test_insight_agent_applies_the_policy():
    from waspada.agents import AgentContext, MockLLM
    from waspada.agents.base import ApprovalGate
    from waspada.agents.insight import InsightAgent
    from waspada.agents.protocol import AgentResult, Status

    scored = _scored([{"loan_id": "L1", "p": 0.8, "band": "High"}])
    ctx = AgentContext(lane="collections", data_handles={"scored_accounts": scored})
    ctx = ctx.with_result(AgentResult(status=Status.OK, agent="risk_model",
                                      artifact_ref="scored_accounts"))

    escalate = RiskPolicy(
        band_to_action={**ACTION_BY_BAND, "High": "call"},
        npl_threshold=DEFAULT_NPL_THRESHOLD,
        vintage_threshold=DEFAULT_VINTAGE_THRESHOLD,
        npl_buckets=frozenset(_NPL_BUCKETS),
    )
    agent = InsightAgent(MockLLM(), gate=ApprovalGate(auto_approve=True),
                         top_n=10, policy=escalate)
    agent.run(ctx)
    payload = ctx.data_handles["dashboard_payload"]
    assert payload["work_list"][0]["recommended_action"] == "call"
