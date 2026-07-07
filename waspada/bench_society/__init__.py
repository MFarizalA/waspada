"""Efficiency benchmark — Agent Society vs single-agent baseline (WA-017).

Proves Track 3's "measurable efficiency gain over single-agent baselines"
with the honesty discipline HACKATHON.md § benchmark demands. The story is
NOT a raw speed contest — it is decision-quality-per-cost.

Two arms on the SAME vintage hold-out test slice (real ``label_default``
ground truth):

* **Single-agent baseline** — one ``qwen3.7-plus`` call per test account
  (raw features in, band+action out). Needs a live Qwen brain
  (``DASHSCOPE_API_KEY`` + the ``openai`` SDK). When those are absent the
  arm reports ``status="not_run"`` with a reason — never a fabricated number.
* **Agent Society** — sklearn scores 100% of the book (0 LLM calls); the
  Skeptic/Judge audit only the top-K riskiest accounts. Deterministic on the
  mock brain, so this arm ALWAYS runs and its recall/precision/escalation
  numbers are REAL.

Metrics emitted (per arm, per K): recall@call-tier, precision@call-tier,
LLM-calls-per-account, wall-clock P50/P95, escalation rate.

Committed snapshot: ``AGENT_SOCIETY_BENCH.json``.
"""
from .run_bench import (
    DEFAULT_K_SWEEP,
    DEFAULT_N_ACCOUNTS,
    DEFAULT_SEED,
    ArmsResult,
    BenchResult,
    SingleArmResult,
    build_holdout_slice,
    run_benchmark,
    run_society_arm,
    run_single_agent_baseline,
    metrics_from_predictions,
)

__all__ = [
    "DEFAULT_K_SWEEP",
    "DEFAULT_N_ACCOUNTS",
    "DEFAULT_SEED",
    "ArmsResult",
    "BenchResult",
    "SingleArmResult",
    "build_holdout_slice",
    "run_benchmark",
    "run_society_arm",
    "run_single_agent_baseline",
    "metrics_from_predictions",
]
