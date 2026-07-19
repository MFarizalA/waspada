"""WA-095 — the human parameter matrix (RiskPolicy extension + provenance).

Backend foundation: RiskPolicy now carries the full governance matrix (band→
action, alert thresholds, dispute_gap, arbiter_confidence, audit_k, top_n), with
per-run construction (policy_from_dict), validation guardrails, and a deterministic
policy_id for run provenance. Pins:
  1. default() still matches the code constants (regression anchor);
  2. new knobs default to the owning modules' constants (no drift);
  3. policy_from_dict accepts a partial matrix and round-trips through to_dict;
  4. policy_id is deterministic + changes when the matrix changes;
  5. validation rejects out-of-vocabulary / out-of-range values;
  6. load_policy (file path) shares the same validator.
"""
from __future__ import annotations

import json

import pytest

from waspada.agents.arbiter import ARBITER_CONFIDENCE_THRESHOLD
from waspada.agents.risk_auditor import DISPUTE_GAP
from waspada.insight.ranking import ACTION_BY_BAND, DEFAULT_NPL_THRESHOLD
from waspada.policy import RiskPolicy, load_policy, policy_from_dict


def test_default_matches_constants_including_new_knobs():
    p = RiskPolicy.default()
    assert p.band_to_action == dict(ACTION_BY_BAND)
    assert p.npl_threshold == float(DEFAULT_NPL_THRESHOLD)
    assert p.dispute_gap == int(DISPUTE_GAP)
    assert p.arbiter_confidence == float(ARBITER_CONFIDENCE_THRESHOLD)
    assert p.audit_k == 8 and p.top_n == 50


def test_partial_matrix_fills_from_default():
    p = policy_from_dict({"audit_k": 12, "dispute_gap": 3})
    assert p.audit_k == 12 and p.dispute_gap == 3
    # untouched knobs fall back to defaults
    assert p.band_to_action == RiskPolicy.default().band_to_action
    assert p.top_n == 50


def test_to_dict_roundtrips():
    p = policy_from_dict({"top_n": 30, "arbiter_confidence": 0.7})
    again = policy_from_dict(p.to_dict())
    assert again.to_dict() == p.to_dict()


def test_policy_id_deterministic_and_change_sensitive():
    a = policy_from_dict({"audit_k": 8})
    b = policy_from_dict({"audit_k": 8})
    c = policy_from_dict({"audit_k": 9})
    assert a.policy_id() == b.policy_id()
    assert a.policy_id() != c.policy_id()
    assert a.policy_id().startswith("policy-")


@pytest.mark.parametrize("bad", [
    {"band_to_action": {"Nope": "call"}},          # unknown band
    {"band_to_action": {"Very High": "teleport"}},  # invalid action
    {"npl_threshold": 1.5},                          # out of [0,1]
    {"arbiter_confidence": -0.1},                    # out of [0,1]
    {"dispute_gap": 0},                              # out of [1,4]
    {"dispute_gap": 9},
    {"audit_k": 0},                                  # must be positive
    {"top_n": 0},
])
def test_validation_rejects_bad_values(bad):
    with pytest.raises(ValueError):
        policy_from_dict(bad)


def test_load_policy_file_shares_validation(tmp_path):
    good = tmp_path / "policy.json"
    good.write_text(json.dumps({"audit_k": 5, "dispute_gap": 2}), encoding="utf-8")
    p = load_policy(str(good))
    assert p.audit_k == 5

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"top_n": -3}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_policy(str(bad))
