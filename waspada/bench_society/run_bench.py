"""run_bench.py — the efficiency benchmark harness (WA-017).

Runs the two arms on the SAME vintage hold-out test slice and emits the
cost-quality metrics. Honest by construction:

  * The **Agent Society arm is deterministic and always runs** — sklearn
    scores the test slice (0 LLM calls at the scoring tier), and a
    deterministic debate proxy (clearly labeled ``mock-deterministic``) drives
    the Skeptic/Actuary/Arbiter turns so the dispute / escalation counts and
    the bounded debate call budget are REAL, reproducible numbers.
  * The **single-agent baseline needs a live Qwen brain** (``DASHSCOPE_API_KEY``
    + the ``openai`` SDK). When either is absent the arm reports
    ``status="not_run"`` with a reason — never a fabricated number.

Honesty rails (HACKATHON.md § benchmark): only metrics computable from our
cross-sectional snapshot are reported. ``label_default`` (eventual
charge-off) IS the ground truth we recall against; cure rates and true band
migration are NOT computable from one snapshot and are never emitted.

The K sweep {4, 8, 16} traces the society's cost-quality frontier: recall of
the high-risk band is flat across K (the scoring tier is independent of K),
while LLM-calls-per-account and escalation rate rise with K — so the cheapest
society point is the smallest K that holds the recall. The baseline is the
single point at 1.0 call/account (quality = not_run without Qwen).

Run as a module to (re)generate the committed snapshot::

    python -m waspada.bench_society.run_bench --out AGENT_SOCIETY_BENCH.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pyarrow as pa

from ..agents.llm import LLM
from ..agents.orchestrator import Orchestrator
from ..agents.protocol import AgentContext, Dispute
from ..agents.risk_auditor import RiskAuditorAgent
from ..agents.risk_model import RiskModelAgent
from ..model.risk import predict as _predict, train as _train
from ..schema import FeatureFrame, RawLoans, schema_from_dataclass

__all__ = [
    "DEFAULT_K_SWEEP",
    "DEFAULT_N_ACCOUNTS",
    "DEFAULT_SEED",
    "ABLATION_K",
    "ABLATION_N_ACCOUNTS",
    "ABLATION_SEED",
    "SingleArmResult",
    "ArmsResult",
    "AblationArmResult",
    "BenchResult",
    "build_holdout_slice",
    "build_ablation_slice",
    "run_society_arm",
    "run_single_agent_baseline",
    "run_benchmark",
    "run_ablation",
    "metrics_from_predictions",
]

# Default sweep + sample size. N is deliberately small (DashScope free-quota
# burn is a stated risk); 300 accounts with a 5-vintage spread yields a
# non-degenerate vintage hold-out and stable quintile bands.
DEFAULT_K_SWEEP: Tuple[int, ...] = (4, 8, 16)
DEFAULT_N_ACCOUNTS = 300
DEFAULT_SEED = 11

# The high-risk "call tier" — the band whose recall/precision we report. This
# is the collections action surface ("Very High" → "call"); matches ACTION_BY_BAND.
HIGH_RISK_BAND = "Very High"


# --------------------------------------------------------------------------- #
# Result shapes — plain dataclasses serialized to the committed JSON.
# --------------------------------------------------------------------------- #
@dataclass
class SingleArmResult:
    """One arm's measured outcome at one K (or the baseline's single point)."""

    arm: str                     # "society" | "single_agent_baseline"
    status: str                  # "ran" | "not_run"
    reason: str = ""             # set when status == "not_run"
    k: Optional[int] = None      # society audit-K; None for the baseline
    n_test_accounts: int = 0
    # --- quality (vs label_default ground truth on the test slice) ---
    recall_at_call_tier: Optional[float] = None
    precision_at_call_tier: Optional[float] = None
    n_flagged_high_risk: int = 0
    n_true_default: int = 0
    n_true_default_caught: int = 0
    # --- cost ---
    llm_calls_total: int = 0
    llm_calls_per_account: Optional[float] = None
    # --- latency (seconds) ---
    wall_clock_seconds: Optional[float] = None
    wall_clock_p50_seconds: Optional[float] = None
    wall_clock_p95_seconds: Optional[float] = None
    # --- governance (society only) ---
    disputes_opened: int = 0
    escalations: int = 0
    escalation_rate: Optional[float] = None
    brain: str = ""              # "sklearn+mock-deterministic" | "qwen3.7-plus" | ...


@dataclass
class ArmsResult:
    society: List[SingleArmResult] = field(default_factory=list)
    single_agent_baseline: Optional[SingleArmResult] = None


@dataclass
class BenchResult:
    schema_version: str = "1"
    generated_at: str = ""
    n_accounts: int = 0
    seed: int = 0
    k_sweep: List[int] = field(default_factory=list)
    split: Dict[str, Any] = field(default_factory=dict)
    arms: ArmsResult = field(default_factory=ArmsResult)
    hero_number: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Normalize the split's numpy ints → python ints for JSON.
        return d


# --------------------------------------------------------------------------- #
# Synthetic hold-out slice — the same multi-vintage shape the test suite uses,
# fed through the REAL ingest→analytics→risk_model vintage split so both arms
# are evaluated on a genuine out-of-time-ish test slice with real labels.
# --------------------------------------------------------------------------- #
def _synthetic_raw_loans(n: int, seed: int) -> pa.Table:
    """Deterministic multi-vintage RawLoans table with three honest cohorts.

    Three cohorts exercise the full debate (the clean-polarized data of the
    test suite yields zero disputes — the skeptic correctly agrees on
    unambiguous accounts, which understates the society's real debate cost):

      * **risky-default** (~40%) — grade E, high rate/dti, low repayment →
        model nails as Very High, label_default=True. Skeptic agrees.
      * **safe-current** (~40%) — grade A, low rate/dti, high repayment →
        Very Low/Low, label_default=False. Skeptic agrees.
      * **borderline-repaying** (~20%) — grade E, high rate/dti, BUT high
        payment_ratio + low outstanding (near-settled). The model scores these
        on their risk drivers (rate/dti/grade) → often High/Very High; a skeptic seeing
        the near-settled balance challenges the band. label_default=False.
        These are the model's FALSE POSITIVES the debate exists to catch.
    """
    rng = np.random.default_rng(seed)
    issue_years = [2019, 2020, 2021, 2022, 2023]
    rows: List[dict] = []
    for i in range(n):
        iy = int(issue_years[i % len(issue_years)])
        im = int(rng.integers(1, 13))
        roll = rng.random()
        if roll < 0.35:  # risky-default
            rate = float(rng.uniform(18, 28)); dti = float(rng.uniform(22, 35))
            grade = "E"; op = float(rng.uniform(0.5, 0.9)); tp = float(rng.uniform(0.0, 0.3))
            status = "Charged Off"
        elif roll < 0.65:  # safe-current
            rate = float(rng.uniform(4, 10)); dti = float(rng.uniform(2, 12))
            grade = "A"; op = float(rng.uniform(0.0, 0.3)); tp = float(rng.uniform(0.6, 1.0))
            status = "Current"
        else:  # borderline-repaying (~35%): risky grade but near-settled
            rate = float(rng.uniform(18, 28)); dti = float(rng.uniform(22, 35))
            grade = "E"; op = float(rng.uniform(0.0, 0.15)); tp = float(rng.uniform(0.7, 1.0))
            status = "Current"
        rows.append(dict(
            loan_id=f"B{i:05d}", amount=float(rng.uniform(2000, 25000)),
            term=int(rng.choice([36, 60])), rate=rate, grade=grade,
            annual_income=float(rng.uniform(30000, 120000)), dti=dti,
            issue_date=dt.date(iy, im, 1),
            purpose=str(rng.choice(["credit_card", "debt_consolidation", "car", "medical"])),
            region=str(rng.choice(["West", "South", "Midwest", "Northeast"])),
            outstanding_principal=float(rng.uniform(100, 5000)) * op,
            total_paid=float(rng.uniform(100, 5000)) * tp,
            current_status=status,
        ))
    cols = {f.name: [] for f in __import__("dataclasses").fields(RawLoans)}
    for r in rows:
        for name in cols:
            cols[name].append(r[name])
    return pa.table(cols, schema=schema_from_dataclass(RawLoans))


def build_holdout_slice(
    n: int = DEFAULT_N_ACCOUNTS,
    seed: int = DEFAULT_SEED,
    as_of: dt.date = dt.date(2024, 12, 1),
) -> Tuple[pa.Table, np.ndarray, np.ndarray, Dict[str, Any]]:
    """Build the feature frame + vintage train/test split (REAL model path).

    Returns ``(feature_frame, train_idx, test_idx, split_info)``. The feature
    frame is built by the real analytics feature builder; the split is the
    real :func:`waspada.model.risk._vintage_split`. Both arms score ONLY the
    ``test_idx`` rows — the newer-vintage hold-out — against their real
    ``label_default`` labels.
    """
    from ..features.collections import build_features

    raw = _synthetic_raw_loans(n, seed)
    frame = build_features(raw, as_of)
    # Reuse the model's own vintage split so the hold-out matches what the
    # society's scoring tier actually trained on.
    from ..model.risk import _vintage_split
    train_idx, test_idx, split = _vintage_split(frame, 0.7)
    # JSON-friendly split info.
    split = dict(split)
    return frame, train_idx, test_idx, split


# --------------------------------------------------------------------------- #
# Metrics — recall/precision of the high-risk (call) tier vs label_default.
# --------------------------------------------------------------------------- #
def metrics_from_predictions(
    y_true: List[int], y_pred_high_risk: List[int],
) -> Dict[str, Any]:
    """Recall/precision of the high-risk flag against the default ground truth.

    ``y_true``   — 1 if the account's ``label_default`` is True (eventual default).
    ``y_pred_high_risk`` — 1 if the arm flagged the account high-risk (Very High / call).
    Returns recall, precision, f1, and the raw confusion counts so the JSON is
    self-auditing.
    """
    y_true_arr = np.asarray(y_true, dtype=np.int8)
    y_pred_arr = np.asarray(y_pred_high_risk, dtype=np.int8)
    n = len(y_true_arr)
    n_true = int(y_true_arr.sum())
    n_flagged = int(y_pred_arr.sum())
    n_caught = int(((y_true_arr == 1) & (y_pred_arr == 1)).sum())
    recall = (n_caught / n_true) if n_true else None
    precision = (n_caught / n_flagged) if n_flagged else None
    f1 = None
    if recall is not None and precision is not None and (recall + precision) > 0:
        f1 = float(2 * recall * precision / (recall + precision))
    return {
        "recall_at_call_tier": float(recall) if recall is not None else None,
        "precision_at_call_tier": float(precision) if precision is not None else None,
        "f1_at_call_tier": f1,
        "n_test_accounts": int(n),
        "n_flagged_high_risk": n_flagged,
        "n_true_default": n_true,
        "n_true_default_caught": n_caught,
    }


# --------------------------------------------------------------------------- #
# Deterministic debate brains — the society's mock-brain path (clearly labeled).
#
# The brief explicitly permits the mock-brain society arm to run because it is
# deterministic. These LLM subclasses produce JSON replies as a deterministic
# function of the account's REAL feature values embedded in the prompt, so the
# dispute/escalation counts are reproducible and grounded in the data (not a
# coin flip). The committed JSON tags every such turn ``model="mock-deterministic"``.
# --------------------------------------------------------------------------- #
_PR_RE = re.compile(r"payment_ratio=([0-9.]+)")
# Longest alternatives first so "Very High" wins over "High".
_BAND_RE = re.compile(r"band=(Very High|Very Low|Medium|High|Low)")


class _DeterministicSkepticLLM(LLM):
    """The Skeptic's deterministic brain.

    Mirrors what a real Qwen skeptic does with the ``portfolio_stats`` /
    ``lookup_account`` MCP tools: it compares the account's payment_ratio
    against the Very High cohort's typical level and challenges when the account is
    an outlier — repaying far better than its high-risk band peers. The
    fixture cites exactly this signal (``"payment_ratio=0.61 vs Very High median
    0.18"``). Grounded in a real feature, reproducible.

    A Very High/High account whose payment_ratio is well above the band's typical level
    → the skeptic reads it as ``Low`` or ``Medium`` (band likely overstates
    risk) → gap ≥ 2 → dispute. Otherwise it agrees (``High``) → no dispute.
    """

    name = "mock-deterministic"
    model_name = "mock-deterministic"

    # Approximate per-band typical payment_ratio (the cohort medians a skeptic
    # with portfolio_stats would see). High-risk bands repay little; a value
    # well above the band's level is the challenge signal.
    _BAND_TYPICAL_PR = {
        "Very Low": 0.80, "Low": 0.60, "Medium": 0.40, "High": 0.15, "Very High": 0.05,
    }
    _CHALLENGE_FACTOR = 3.0  # account repays >3x the band's typical → challenge

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, *, history=None) -> str:
        self.calls += 1
        m_pr = _PR_RE.search(prompt)
        m_band = _BAND_RE.search(prompt)
        pr = float(m_pr.group(1)) if m_pr else 0.5
        band = m_band.group(1) if m_band else "Medium"
        typical = self._BAND_TYPICAL_PR.get(band, 0.3)
        challenge = pr >= (typical * self._CHALLENGE_FACTOR) and pr >= 0.08
        if challenge and band in ("High", "Very High"):
            # How far above the band's level decides Low vs Medium.
            view = "Low" if pr >= typical * 6 else "Medium"
            conf = 0.78 if view == "Low" else 0.7
            claim = (f"repayment_ratio {pr:.2f} is ~{pr/max(typical,0.01):.0f}x the "
                     f"{band} cohort level; band likely overstates risk")
            return json.dumps({
                "auditor_view": view, "confidence": conf,
                "claim": claim, "evidence": [f"payment_ratio={pr:.2f}", f"{band}_typical={typical:.2f}"],
            })
        view, conf = "High", 0.82
        claim = "repayment pattern consistent with the band"
        return json.dumps({
            "auditor_view": view, "confidence": conf,
            "claim": claim, "evidence": [f"payment_ratio={pr:.2f}"],
        })


class _DeterministicActuaryLLM(LLM):
    """The Actuary's deterministic rebuttal brain.

    Defends the band with a confidence that tracks the SAME repayment evidence
    the Skeptic cited. A near-settled high-risk account (high payment_ratio)
    is a genuinely weak defense → low confidence → the arbiter escalates. A
    clearly-delinquent Very High account (low payment_ratio) is a strong defense.
    Grounded in the real feature; reproducible; never a coin flip.
    """

    name = "mock-deterministic"
    model_name = "mock-deterministic"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, *, history=None) -> str:
        self.calls += 1
        m_pr = re.search(r"payment_ratio=([0-9.]+)", prompt)
        pr = float(m_pr.group(1)) if m_pr else 0.5
        # Weak defense when repayment contradicts a high band; strong otherwise.
        # A Very High account repaying well is the textbook weak-defense case.
        if pr >= 0.15:
            conf = 0.52  # weak → arbiter escalates (below 0.6 threshold)
            claim = "band is the model's best estimate but repayment evidence is mixed"
        else:
            conf = 0.82
            claim = "band stands; delinquency and risk drivers are decisive"
        return json.dumps({
            "verdict": "uphold", "confidence": round(conf, 2),
            "claim": claim, "evidence": [f"payment_ratio={pr:.2f}"],
        })


class _DeterministicArbiterLLM(LLM):
    """The Arbiter's deterministic ruling brain.

    Rules on the defense strength: a weak defense (actuary confidence < 0.6,
    recoverable from the Round-2 claim text) → escalate to the human; a strong
    defense → uphold. This reproduces the real arbiter's low-confidence →
    escalate rule with deterministic inputs.
    """

    name = "mock-deterministic"
    model_name = "mock-deterministic"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, *, history=None) -> str:
        self.calls += 1
        # The arbiter prompt cites Round 2's claim + confidence. A weak defense
        # (confidence ≈ 0.5, or "mixed" in the claim) → escalate.
        weak = ("0.5" in prompt) or ("mixed" in prompt.lower())
        if weak:
            return json.dumps({
                "ruling": "escalate", "confidence": 0.5,
                "rationale": "defense unconvincing; defer to human", "evidence": [],
            })
        return json.dumps({
            "ruling": "uphold", "confidence": 0.85,
            "rationale": "model's risk drivers are persuasive", "evidence": [],
        })


# --------------------------------------------------------------------------- #
# Society arm — real sklearn scoring + deterministic debate proxy.
# --------------------------------------------------------------------------- #
def run_society_arm(
    frame: pa.Table,
    train_idx: np.ndarray,
    test_idx: np.ndarray,
    k: int,
) -> SingleArmResult:
    """Run the Agent Society arm on the hold-out test slice at audit-K.

    The scoring tier is the real sklearn model (trained on ``train_idx``,
    scored on ``test_idx``); the debate tier is the real
    :class:`RiskAuditorAgent` + the orchestrator's 3-round resolution driven by
    the deterministic brains above. Every metric is measured, not estimated.
    """
    res = SingleArmResult(arm="society", status="ran", k=k, brain="sklearn+mock-deterministic")
    n_test = int(len(test_idx))
    res.n_test_accounts = n_test
    if n_test == 0:
        res.status = "not_run"
        res.reason = "empty hold-out test slice"
        return res

    # --- Scoring tier: train on train_idx, predict on test_idx (0 LLM calls). ---
    t0 = time.perf_counter()
    full_model = _train(frame)  # fits the real pipeline (vintage split internally)
    test_frame = frame.take(test_idx)
    scored = _predict(full_model, test_frame)
    scoring_seconds = time.perf_counter() - t0

    # Quality: high-risk flag = (score_band == Very High) vs label_default.
    bands = scored.column("score_band").to_pylist()
    labels = scored.column("label_default").to_pylist()
    y_true = [int(bool(b)) for b in labels]
    y_pred = [1 if b == HIGH_RISK_BAND else 0 for b in bands]
    m = metrics_from_predictions(y_true, y_pred)
    res.recall_at_call_tier = m["recall_at_call_tier"]
    res.precision_at_call_tier = m["precision_at_call_tier"]
    res.n_flagged_high_risk = m["n_flagged_high_risk"]
    res.n_true_default = m["n_true_default"]
    res.n_true_default_caught = m["n_true_default_caught"]

    # --- Debate tier: audit top-K with the real auditor + deterministic brain. ---
    skeptic = _DeterministicSkepticLLM()
    actuary = _DeterministicActuaryLLM()
    arbiter = _DeterministicArbiterLLM()
    # Build a context the auditor can resolve on. The auditor looks up the
    # scored table via prior_results[-1].artifact_ref, so publish a result
    # pointing at the handle we stashed in data_handles.
    from ..agents.protocol import AgentResult, Status
    ctx = AgentContext(
        lane="collections",
        data_handles={"scored_accounts": scored, "feature_frame": test_frame},
        prior_results=[AgentResult(
            status=Status.OK, agent="risk_model",
            artifact_ref="scored_accounts",
            notes="bench scored_accounts",
        )],
    )
    auditor = RiskAuditorAgent(skeptic, k=k)
    audit_t0 = time.perf_counter()
    auditor.run(ctx)
    audit_seconds = time.perf_counter() - audit_t0
    disputes: List[Dispute] = list(ctx.data_handles.get("risk_disputes") or [])

    # Resolve disputes through the real 3-round flow (rebuttal + arbiter) so
    # escalation counts are the genuine terminal-state distribution. We drive
    # it directly with the agents (not via the orchestrator) so per-call latency
    # is measured precisely and the LLM call counts are exact.
    from ..agents.arbiter import ArbiterAgent
    escalations = 0
    per_call_latencies: List[float] = []
    if disputes:
        risk_model_agent = RiskModelAgent(actuary)
        arb = ArbiterAgent(arbiter)
        for d in disputes:
            # Round 2: the Actuary rebuts.
            r2_t0 = time.perf_counter()
            r2 = risk_model_agent.defend_score(d, scored, test_frame)
            d.rounds.append(r2)
            verdict = Orchestrator._rebuttal_verdict(r2.claim)
            per_call_latencies.append(time.perf_counter() - r2_t0)
            if verdict == "concede":
                d.resolution = "overridden"; d.resolved_by = "risk_model"
                continue
            if verdict == "unparsable":
                d.resolution = "escalated_approved"; d.resolved_by = "human"; escalations += 1
                continue
            # verdict == "uphold" → Round 3: the Arbiter rules.
            r3_t0 = time.perf_counter()
            ruling, _rationale, _conf, r3 = arb.rule(d)
            d.rounds.append(r3)
            per_call_latencies.append(time.perf_counter() - r3_t0)
            if ruling == "uphold":
                d.resolution = "upheld"; d.resolved_by = "arbiter"
            elif ruling == "override":
                d.resolution = "overridden"; d.resolved_by = "arbiter"
            else:  # escalate → human gate (auto-approved in this offline run)
                d.resolution = "escalated_approved"; d.resolved_by = "human"; escalations += 1

    res.disputes_opened = len(disputes)
    res.escalations = escalations
    res.escalation_rate = (escalations / len(disputes)) if disputes else 0.0

    # --- Cost: the bounded debate budget (scoring tier = 0 LLM calls). ---
    llm_calls = skeptic.calls + actuary.calls + arbiter.calls
    res.llm_calls_total = llm_calls
    res.llm_calls_per_account = llm_calls / n_test

    # --- Latency: scoring (batch) + debate per-call P50/P95. ---
    res.wall_clock_seconds = float(scoring_seconds + audit_seconds)
    # Per-account view: scoring is a vectorized batch → per-account mean; the
    # debate calls are individually timed. We combine: scoring per-account +
    # debate per-account amortized, and report P50/P95 of the debate-call
    # latencies as the decision-latency tail (the only real tail we have).
    scoring_per_account = scoring_seconds / n_test
    if per_call_latencies:
        res.wall_clock_p50_seconds = float(statistics.median(per_call_latencies))
        res.wall_clock_p95_seconds = float(np.percentile(per_call_latencies, 95))
    else:
        # No disputes → no per-call tail; report the batch per-account mean.
        res.wall_clock_p50_seconds = float(scoring_per_account)
        res.wall_clock_p95_seconds = float(scoring_per_account)
    return res


# --------------------------------------------------------------------------- #
# Single-agent baseline — one Qwen call per test account (not_run without Qwen).
# --------------------------------------------------------------------------- #
def _qwen_available() -> Tuple[bool, str]:
    """True iff a live Qwen baseline can run. Returns (ok, reason)."""
    import os
    if not os.environ.get("DASHSCOPE_API_KEY"):
        return False, "DASHSCOPE_API_KEY not set; live Qwen baseline cannot run without burning quota."
    try:
        import openai  # noqa: F401
    except ImportError:
        return False, "openai SDK not installed; install openai to run the live Qwen baseline."
    return True, ""


def _baseline_prompt(loan_id: str, row: Dict[str, Any]) -> str:
    """Raw features in → ask for band + action (the single-agent baseline)."""
    return "\n".join([
        "You are a single-agent loan-risk classifier. Score this account.",
        f"loan_id: {loan_id}",
        f"amount: {row.get('amount'):.0f}",
        f"rate: {row.get('rate'):.2f}",
        f"grade: {row.get('grade')}",
        f"dti: {row.get('dti'):.2f}",
        f"loan_age: {row.get('loan_age')}",
        f"payment_ratio: {row.get('payment_ratio'):.2f}",
        f"outstanding_ratio: {row.get('outstanding_ratio'):.2f}",
        f"delinquency_status: {row.get('delinquency_status')}",
        "Reply with ONLY a JSON object, no prose, exactly this shape:",
        '{"score_band": "Very Low|Low|Medium|High|Very High", "recommended_action": "call|watch|auto-cure"}',
    ])


def run_single_agent_baseline(
    frame: pa.Table,
    test_idx: np.ndarray,
) -> SingleArmResult:
    """Run the single-agent baseline (one qwen3.7-plus call per test account).

    Returns ``status="not_run"`` with a reason when the live brain is
    unavailable — the brief's explicit honesty rail. Never fabricates a number.
    """
    res = SingleArmResult(
        arm="single_agent_baseline", status="not_run",
        k=None, brain="qwen3.7-plus",
    )
    res.n_test_accounts = int(len(test_idx))
    ok, reason = _qwen_available()
    if not ok:
        res.reason = reason
        return res

    # --- Live path (only reached with DASHSCOPE_API_KEY + openai installed). ---
    from ..agents.llm import QwenLLM
    brain = QwenLLM(json_mode=True)  # pragma: no cover - network path
    test_frame = frame.take(test_idx)
    rows = _frame_rows(test_frame)
    y_true: List[int] = []
    y_pred: List[int] = []
    latencies: List[float] = []
    t0 = time.perf_counter()
    import json as _json
    for loan_id, row in rows:
        label = int(bool(row.get("label_default")))
        call_t0 = time.perf_counter()
        raw = brain.complete(_baseline_prompt(loan_id, row))  # pragma: no cover
        latencies.append(time.perf_counter() - call_t0)
        band = ""
        try:
            obj = _json.loads(raw)
            band = str(obj.get("score_band", "")).upper()
        except (ValueError, TypeError):
            band = ""
        y_true.append(label)
        y_pred.append(1 if band == HIGH_RISK_BAND else 0)
    res.wall_clock_seconds = time.perf_counter() - t0
    res.llm_calls_total = len(rows)
    res.llm_calls_per_account = (len(rows) / len(rows)) if rows else None
    if latencies:
        res.wall_clock_p50_seconds = float(statistics.median(latencies))
        res.wall_clock_p95_seconds = float(np.percentile(latencies, 95))
    m = metrics_from_predictions(y_true, y_pred)
    res.recall_at_call_tier = m["recall_at_call_tier"]
    res.precision_at_call_tier = m["precision_at_call_tier"]
    res.n_flagged_high_risk = m["n_flagged_high_risk"]
    res.n_true_default = m["n_true_default"]
    res.n_true_default_caught = m["n_true_default_caught"]
    res.status = "ran"
    return res


def _frame_rows(frame: pa.Table) -> List[Tuple[str, Dict[str, Any]]]:
    names = frame.column_names
    cols = {n: frame.column(n).to_pylist() for n in names}
    ids = cols["loan_id"]
    return [(ids[i], {n: cols[n][i] for n in names}) for i in range(len(ids))]


# --------------------------------------------------------------------------- #
# Top-level orchestrator — runs both arms, sweeps K, writes the hero number.
# --------------------------------------------------------------------------- #
def run_benchmark(
    n: int = DEFAULT_N_ACCOUNTS,
    seed: int = DEFAULT_SEED,
    k_sweep: Tuple[int, ...] = DEFAULT_K_SWEEP,
) -> BenchResult:
    """Run both arms on the same hold-out slice and assemble the result."""
    frame, train_idx, test_idx, split = build_holdout_slice(n=n, seed=seed)
    society_results: List[SingleArmResult] = []
    for k in k_sweep:
        society_results.append(run_society_arm(frame, train_idx, test_idx, k))
    baseline = run_single_agent_baseline(frame, test_idx)

    arms = ArmsResult(society=society_results, single_agent_baseline=baseline)
    result = BenchResult(
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        n_accounts=int(frame.num_rows),
        seed=int(seed),
        k_sweep=list(k_sweep),
        split=split,
        arms=arms,
    )
    result.hero_number = _hero_number(society_results, baseline)
    result.notes = _honesty_notes(society_results, baseline)
    return result


def _hero_number(society: List[SingleArmResult], baseline: Optional[SingleArmResult]) -> Dict[str, Any]:
    """The one headline figure: society calls-per-account vs baseline 1.0.

    Reports the society's cheapest K (smallest calls/account) recall alongside
    its calls-per-account, and the baseline's calls-per-account (1.0 by
    construction). Baseline recall is whatever the baseline measured (or
    ``not_run``).
    """
    if not society:
        return {}
    cheapest = min(society, key=lambda r: (r.llm_calls_per_account if r.llm_calls_per_account is not None else 1e9))
    base_cpa = baseline.llm_calls_per_account if (baseline and baseline.status == "ran") else 1.0
    society_cpa = cheapest.llm_calls_per_account or 0.0
    ratio = (base_cpa / society_cpa) if society_cpa > 0 else None
    return {
        "claim_shape": "society scores 100% of the book at the scoring tier with 0 LLM calls; bounded debate calls spent only on top-K contested accounts",
        "society_llm_calls_per_account_min": float(society_cpa),
        "society_llm_calls_per_account_k": cheapest.k,
        "baseline_llm_calls_per_account": float(base_cpa) if base_cpa is not None else None,
        "calls_reduction_vs_baseline": (float(ratio) if ratio is not None else None),
        "society_recall_at_call_tier": cheapest.recall_at_call_tier,
        "society_precision_at_call_tier": cheapest.precision_at_call_tier,
        "baseline_recall_at_call_tier": (baseline.recall_at_call_tier if baseline else None),
        "baseline_status": (baseline.status if baseline else "not_run"),
    }


def _honesty_notes(society: List[SingleArmResult], baseline: Optional[SingleArmResult]) -> List[str]:
    """Explicit honesty disclosures (the credibility features)."""
    notes = [
        "Ground truth is label_default (eventual charge-off) — the cross-sectional label. "
        "Cure rates and true band migration are NOT computable from one snapshot and are never reported.",
        "Society scoring tier is the real sklearn LogisticRegression (vintage-split, leakage-guarded); "
        "the debate tier uses a deterministic mock brain (model='mock-deterministic') grounded in real "
        "feature values — reproducible, not a coin flip.",
        "Recall/precision are flat across K: the scoring tier is independent of K. K only raises the "
        "debate budget (calls/account) and escalation load — so the cheapest society point is the smallest K.",
        "The single-agent baseline needs a live Qwen brain (DASHSCOPE_API_KEY + openai SDK). Without it "
        "the baseline reports not_run; its calls-per-account is 1.0 by construction (one call per account).",
        "Multi-agent is NOT worth it for cheap, low-stakes decisions — loan-risk qualifies because each "
        "decision is high-value and tool-grounded. Naming the boundary is a credibility feature.",
    ]
    if baseline and baseline.status != "ran":
        notes.append(f"Baseline status={baseline.status}: {baseline.reason}")
    return notes


# --------------------------------------------------------------------------- #
# CLI — regenerate the committed snapshot.
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="WA-017 efficiency benchmark")
    p.add_argument("--out", default=None, help="output JSON path (default: <pkgdir>/AGENT_SOCIETY_BENCH.json)")
    p.add_argument("--n", type=int, default=DEFAULT_N_ACCOUNTS, help="number of synthetic accounts")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED, help="RNG seed")
    p.add_argument("--k", type=int, nargs="+", default=list(DEFAULT_K_SWEEP), help="K sweep values")
    args = p.parse_args(argv)

    result = run_benchmark(n=args.n, seed=args.seed, k_sweep=tuple(args.k))
    out_path = Path(args.out) if args.out else Path(__file__).resolve().parent / "AGENT_SOCIETY_BENCH.json"
    payload = result.to_dict()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=False))
    # Stdout summary for the operator (the JSON is the artifact).
    _print_summary(result, out_path)
    return 0


def _print_summary(result: BenchResult, out_path: Path) -> None:
    h = result.hero_number
    print(f"[WA-017] benchmark written → {out_path}")
    print(f"  n_accounts={result.n_accounts} seed={result.seed} split={result.split.get('method')}")
    print(f"  society (cheapest K={h.get('society_llm_calls_per_account_k')}): "
          f"calls/account={h.get('society_llm_calls_per_account_min')} "
          f"recall={h.get('society_recall_at_call_tier')} "
          f"precision={h.get('society_precision_at_call_tier')}")
    print(f"  baseline: calls/account={h.get('baseline_llm_calls_per_account')} "
          f"status={h.get('baseline_status')}")
    if h.get("calls_reduction_vs_baseline") is not None:
        print(f"  hero: society uses ~1/{h['calls_reduction_vs_baseline']:.1f} the baseline's calls/account")
    for r in result.arms.society:
        print(f"  society[K={r.k}]: disputes={r.disputes_opened} escalations={r.escalations} "
              f"escalation_rate={r.escalation_rate} calls/account={r.llm_calls_per_account}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
