"""Efficiency benchmark tests (WA-017 acceptance).

Acceptance covered here:

  * The harness runs both arms on the SAME vintage hold-out slice and emits
    every required metric key.
  * The society arm ALWAYS runs (deterministic) — its recall/precision against
    ``label_default`` are real numbers, never ``None``.
  * The single-agent baseline reports ``not_run`` with a reason when the live
    Qwen brain is unavailable — NEVER a fabricated number. (CI has no
    ``DASHSCOPE_API_KEY`` / ``openai`` SDK, so this is the enforced path.)
  * The K sweep produces the cost-quality frontier: society calls-per-account
    rises with K; recall is flat across K (scoring tier is K-independent).
  * No metric our cross-sectional snapshot can't produce is emitted (no cure
    rates, no band migration — HACKATHON.md honesty rails).
  * The committed JSON snapshot is present, well-formed, and self-consistent.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from waspada.bench_society import (
    DEFAULT_K_SWEEP,
    BenchResult,
    build_holdout_slice,
    metrics_from_predictions,
    run_benchmark,
    run_society_arm,
    run_single_agent_baseline,
)

_PKG_DIR = Path(__file__).resolve().parents[1] / "waspada" / "bench_society"
_SNAPSHOT = _PKG_DIR / "AGENT_SOCIETY_BENCH.json"

# Keys every society arm result must carry (the WA-017 acceptance metric set).
_SOCIETY_KEYS = {
    "arm", "status", "k", "n_test_accounts",
    "recall_at_call_tier", "precision_at_call_tier",
    "n_flagged_high_risk", "n_true_default", "n_true_default_caught",
    "llm_calls_total", "llm_calls_per_account",
    "wall_clock_seconds", "wall_clock_p50_seconds", "wall_clock_p95_seconds",
    "disputes_opened", "escalations", "escalation_rate", "brain",
}
_BASELINE_KEYS = {
    "arm", "status", "reason", "n_test_accounts",
    "llm_calls_per_account", "brain",
}


# --------------------------------------------------------------------------- #
# metrics_from_predictions — the recall/precision core.
# --------------------------------------------------------------------------- #
def test_metrics_recall_precision_basic():
    m = metrics_from_predictions(
        y_true=[1, 1, 0, 0, 1], y_pred_high_risk=[1, 0, 1, 0, 1],
    )
    # 3 defaults total, 2 caught (positions 0,4) → recall 2/3.
    # 3 flagged (positions 0,2,4), of which 2 are true default → precision 2/3.
    assert m["recall_at_call_tier"] == pytest.approx(2 / 3)
    assert m["precision_at_call_tier"] == pytest.approx(2 / 3)
    assert m["n_true_default"] == 3 and m["n_flagged_high_risk"] == 3
    assert m["n_true_default_caught"] == 2


def test_metrics_handles_no_positives_gracefully():
    # No true defaults → recall is None (undefined), not a divide-by-zero crash.
    m = metrics_from_predictions(y_true=[0, 0, 0], y_pred_high_risk=[1, 0, 0])
    assert m["recall_at_call_tier"] is None
    assert m["precision_at_call_tier"] == pytest.approx(0.0)  # 0 caught / 1 flagged


def test_metrics_handles_no_flags_gracefully():
    # Nothing flagged → precision is None; recall is 0 (caught none of the positives).
    m = metrics_from_predictions(y_true=[1, 1, 0], y_pred_high_risk=[0, 0, 0])
    assert m["recall_at_call_tier"] == pytest.approx(0.0)
    assert m["precision_at_call_tier"] is None


# --------------------------------------------------------------------------- #
# Hold-out slice — real vintage split, real labels.
# --------------------------------------------------------------------------- #
def test_holdout_slice_is_vintage_split_with_real_labels():
    frame, train_idx, test_idx, split = build_holdout_slice(n=200, seed=11)
    assert split["method"] in {"vintage", "shuffle_fallback"}
    assert len(train_idx) + len(test_idx) == frame.num_rows
    assert len(test_idx) > 0
    # The test slice carries real label_default ground truth.
    labels = frame.take(test_idx).column("label_default").to_pylist()
    assert any(labels) and not all(labels), "test slice must contain both classes"


# --------------------------------------------------------------------------- #
# Society arm — always runs, real numbers, deterministic.
# --------------------------------------------------------------------------- #
def test_society_arm_always_runs_with_real_metrics():
    frame, train_idx, test_idx, _ = build_holdout_slice(n=200, seed=11)
    res = run_society_arm(frame, train_idx, test_idx, k=8)
    assert res.status == "ran"
    assert res.arm == "society"
    assert res.k == 8
    assert res.brain == "sklearn+mock-deterministic"
    # Real quality numbers (not None) against label_default ground truth.
    assert res.recall_at_call_tier is not None and 0.0 <= res.recall_at_call_tier <= 1.0
    assert res.precision_at_call_tier is not None and 0.0 <= res.precision_at_call_tier <= 1.0
    # Cost: scoring tier is 0 LLM calls; only the debate budget spends calls.
    assert res.llm_calls_total > 0  # the debate fired
    assert res.llm_calls_per_account is not None and res.llm_calls_per_account < 1.0
    # The bounded budget ceiling: ≤ K×3 calls over N.
    assert res.llm_calls_total <= 8 * 3
    # Latency measured.
    assert res.wall_clock_seconds is not None and res.wall_clock_seconds > 0
    # Governance: disputes + escalations counted (never negative).
    assert res.disputes_opened >= 0 and res.escalations >= 0
    assert 0.0 <= res.escalation_rate <= 1.0


def test_society_recall_is_flat_across_k_calls_rise_with_k():
    """The cost-quality frontier shape: recall K-independent, calls rise with K."""
    frame, train_idx, test_idx, _ = build_holdout_slice(n=300, seed=11)
    results = [run_society_arm(frame, train_idx, test_idx, k=k) for k in (4, 8, 16)]
    recalls = [r.recall_at_call_tier for r in results]
    precisions = [r.precision_at_call_tier for r in results]
    calls = [r.llm_calls_per_account for r in results]
    # Recall + precision are identical across K (scoring tier is K-independent).
    assert len(set(recalls)) == 1, f"recall must be flat across K, got {recalls}"
    assert len(set(precisions)) == 1, f"precision must be flat across K, got {precisions}"
    # Calls-per-account is non-decreasing in K (more accounts audited).
    assert calls == sorted(calls), f"calls/account must rise with K, got {calls}"


def test_society_bounded_debate_budget_ceiling_holds():
    """Worst case ≤ K×3 LLM calls (the deterministic cost ceiling)."""
    frame, train_idx, test_idx, _ = build_holdout_slice(n=300, seed=11)
    for k in (4, 8, 16):
        res = run_society_arm(frame, train_idx, test_idx, k=k)
        assert res.llm_calls_total <= k * 3, f"K={k}: budget breached"


# --------------------------------------------------------------------------- #
# Single-agent baseline — not_run without the live brain (CI's enforced path).
# --------------------------------------------------------------------------- #
def test_baseline_not_run_without_qwen_creds(monkeypatch):
    """No DASHSCOPE_API_KEY (CI) → baseline reports not_run, never a fabricated number."""
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    frame, _, test_idx, _ = build_holdout_slice(n=100, seed=11)
    res = run_single_agent_baseline(frame, test_idx)
    assert res.status == "not_run"
    assert res.reason  # a non-empty reason
    # The honesty rail: no fabricated quality numbers.
    assert res.recall_at_call_tier is None
    assert res.precision_at_call_tier is None
    assert res.llm_calls_per_account is None
    assert res.brain == "qwen3.7-plus"


# --------------------------------------------------------------------------- #
# Full benchmark — shape + frontier + hero number.
# --------------------------------------------------------------------------- #
def test_run_benchmark_emits_all_required_metric_keys():
    result = run_benchmark(n=200, seed=11, k_sweep=(4, 8))
    assert isinstance(result, BenchResult)
    d = result.to_dict()
    # Top-level shape.
    assert d["schema_version"] == "1"
    assert set(d) >= {"schema_version", "generated_at", "n_accounts", "seed",
                      "k_sweep", "split", "arms", "hero_number", "notes"}
    # Society arms carry every required key.
    for arm in d["arms"]["society"]:
        assert _SOCIETY_KEYS <= set(arm.keys()), f"missing keys: {_SOCIETY_KEYS - set(arm.keys())}"
        assert arm["status"] == "ran"
    # Baseline carries the not_run honesty rail (no creds in CI).
    base = d["arms"]["single_agent_baseline"]
    assert _BASELINE_KEYS <= set(base.keys())
    assert base["status"] == "not_run"
    # Hero number references the cheapest society point.
    h = d["hero_number"]
    assert h["society_llm_calls_per_account_min"] is not None
    assert h["baseline_llm_calls_per_account"] == 1.0
    assert h["baseline_status"] == "not_run"


def test_benchmark_cost_quality_frontier_is_producible():
    """The frontier (recall vs calls/account across K) is derivable from the JSON."""
    result = run_benchmark(n=200, seed=11, k_sweep=(4, 8, 16))
    points = [
        (a.llm_calls_per_account, a.recall_at_call_tier, a.k)
        for a in result.arms.society
    ]
    assert len(points) == 3
    # The frontier is monotonic in calls (K rises → calls rise).
    calls = [p[0] for p in points]
    assert calls == sorted(calls)


# --------------------------------------------------------------------------- #
# Honesty rails — no uncomputable metrics are ever emitted.
# --------------------------------------------------------------------------- #
def test_no_uncomputable_metrics_in_result():
    """Cure rates and band migration never appear as emitted metric FIELDS
    (HACKATHON.md rails). The honesty notes are allowed to NAME the excluded
    metrics to disclose the boundary — that disclosure is a credibility feature,
    not a leak. We check the metric keys/values, not the prose notes."""
    result = run_benchmark(n=150, seed=11, k_sweep=(4,))
    # Walk every metric key + nested key in the result (excluding notes prose).
    def _walk(obj, prefix=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "notes":  # prose disclosure, not a metric
                    continue
                yield from _walk(v, f"{prefix}.{k}" if prefix else k)
        elif isinstance(obj, list):
            for item in obj:
                yield from _walk(item, prefix)
        else:
            yield prefix
    metric_paths = set(_walk(result.to_dict()))
    forbidden = {"cure_rate", "cure rates", "band_migration", "roll_rate",
                 "true_band_migration"}
    leaked = [p for p in metric_paths if any(bad in p.lower() for bad in forbidden)]
    assert not leaked, f"uncomputable metric keys leaked: {leaked}"
    # The honesty notes explicitly disclose the exclusion (credibility feature).
    joined = " ".join(result.notes).lower()
    assert "not computable" in joined or "never reported" in joined


# --------------------------------------------------------------------------- #
# Committed snapshot — present, well-formed, self-consistent.
# --------------------------------------------------------------------------- #
def test_committed_snapshot_exists_and_is_well_formed():
    assert _SNAPSHOT.exists(), f"committed snapshot missing: {_SNAPSHOT}"
    data = json.loads(_SNAPSHOT.read_text())
    assert data["schema_version"] == "1"
    assert data["k_sweep"] == list(DEFAULT_K_SWEEP)
    assert len(data["arms"]["society"]) == len(DEFAULT_K_SWEEP)
    # Every society arm ran and carries the full metric set.
    for arm in data["arms"]["society"]:
        assert arm["status"] == "ran"
        assert _SOCIETY_KEYS <= set(arm.keys())
        assert arm["recall_at_call_tier"] is not None  # real number, never None


def test_committed_snapshot_hero_number_is_self_consistent():
    data = json.loads(_SNAPSHOT.read_text())
    h = data["hero_number"]
    society_points = data["arms"]["society"]
    cheapest = min(society_points, key=lambda a: a["llm_calls_per_account"])
    assert h["society_llm_calls_per_account_min"] == cheapest["llm_calls_per_account"]
    assert h["society_llm_calls_per_account_k"] == cheapest["k"]
    assert h["society_recall_at_call_tier"] == cheapest["recall_at_call_tier"]


def test_committed_snapshot_baseline_is_honestly_not_run_without_creds():
    """The committed baseline is not_run (CI has no creds) — never a fabricated number."""
    data = json.loads(_SNAPSHOT.read_text())
    base = data["arms"]["single_agent_baseline"]
    # In the offline commit environment the baseline cannot run; if it did run
    # (re-generated with creds), that's also acceptable — just assert the
    # status vocabulary and that not_run carries no fabricated quality.
    assert base["status"] in {"ran", "not_run"}
    if base["status"] == "not_run":
        assert base["reason"]
        assert base["recall_at_call_tier"] is None
        assert base["precision_at_call_tier"] is None
