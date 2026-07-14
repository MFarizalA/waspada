#!/usr/bin/env python
"""Live Qwen API smoke test for the WASPADA agent debate protocol.

This is a **standalone, network-dependent** script — it is NOT part of the
regular pytest suite and must never be collected by CI (it would hang offline).
Run it explicitly when you have DASHSCOPE_API_KEY available:

    python tests/smoke_live_qwen.py

What it does:
  1. Loads DASHSCOPE_API_KEY from .env (or os.environ).
  2. Instantiates the real QwenLLM (waspada.agents.llm) against DashScope.
  3. Exercises the three debate prompt types:
       a. Risk Auditor  → _parse_view_json   (expects auditor_view JSON)
       b. Actuary       → _parse_verdict_json (expects verdict JSON)
       c. Arbiter       → _parse_ruling_json  (expects ruling JSON)
  4. Tests model tiering via with_model() for flash / plus / max.
  5. Times each API call (latency data for demo planning).
  6. Verifies the existing JSON parsers handle the *real* API response shapes.
  7. Prints the raw API response for the first call (shape inspection).
  8. Reports pass/fail per sub-test and a final summary.

Production code is never modified — this is read-only from the agents package.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: load .env, ensure repo root is on sys.path for waspada.* imports
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv
    _DOTENV_PATH = _REPO_ROOT / ".env"
    if _DOTENV_PATH.exists():
        load_dotenv(_DOTENV_PATH)
except ImportError:
    pass  # fall back to os.environ only

# Now import the production code (read-only — we do not modify it).
from waspada.agents.llm import QwenLLM, QWEN_TIER_DEFAULTS, qwen_tier  # noqa: E402
from waspada.agents.risk_auditor import RiskAuditorAgent, _parse_view_json  # noqa: E402
from waspada.agents.risk_model import RiskModelAgent, _parse_verdict_json  # noqa: E402
from waspada.agents.arbiter import ArbiterAgent, _parse_ruling_json  # noqa: E402
from waspada.agents.protocol import Dispute, DisputeRound  # noqa: E402


# ---------------------------------------------------------------------------
# ANSI helpers (best-effort; works on most terminals, degrades on Windows cmd)
# ---------------------------------------------------------------------------
class C:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def _pass(msg: str) -> str:
    return f"{C.GREEN}✓ PASS{C.RESET} {msg}"


def _fail(msg: str) -> str:
    return f"{C.RED}✗ FAIL{C.RESET} {msg}"


def _info(msg: str) -> str:
    return f"{C.CYAN}ℹ{C.RESET}  {msg}"


def _warn(msg: str) -> str:
    return f"{C.YELLOW}⚠ WARN{C.RESET} {msg}"


# ---------------------------------------------------------------------------
# Prompt builders — mirror the real agent prompts but standalone (no pyarrow)
# ---------------------------------------------------------------------------
def auditor_challenge_prompt() -> str:
    """A Risk Auditor–style challenge prompt (Round 1)."""
    lines = [
        "You are the Skeptic (risk auditor) in a bounded risk debate.",
        "Account L-0042: the Actuary (classical ML model) scored it "
        "p_default=0.870, band=Very High.",
        "Account features: payment_ratio=0.12; outstanding_ratio=0.91; "
        "dti=0.38; rate=0.19; loan_age=6; delinquency_status=31-120; grade=E.",
        "Portfolio context: book_npl_ratio=0.180.",
        "Give your INDEPENDENT view of this account's risk. Reply with ONLY a "
        "JSON object, no prose, exactly this shape:",
        '{"auditor_view": "Low|Medium|High", "confidence": 0.0-1.0, '
        '"claim": "one-sentence rationale", "evidence": ["fact1", "fact2"]}',
    ]
    return "\n".join(lines)


def actuary_defend_prompt() -> str:
    """An Actuary–style rebuttal prompt (Round 2)."""
    lines = [
        "You are the Actuary (classical risk model) in a bounded risk debate.",
        "Account L-0042: you scored it band=Very High",
        "Your model's p_default=0.870.",
        'The Skeptic (risk_auditor) opened a dispute, viewing this as Low risk. '
        'Its challenge: "p_default is inflated by short loan age; delinquency '
        'is early-cycle and may cure."',
        "Account features: payment_ratio=0.12; outstanding_ratio=0.91; "
        "dti=0.38; rate=0.19; loan_age=6; delinquency_status=31-120; grade=E.",
        "Defend or concede your band. Reply with ONLY a JSON object, no prose, "
        "exactly this shape:",
        '{"verdict": "uphold|concede", "confidence": 0.0-1.0, '
        '"claim": "one-sentence rationale", "evidence": ["fact1", "fact2"]}',
    ]
    return "\n".join(lines)


def arbiter_ruling_prompt() -> str:
    """An Arbiter–style ruling prompt (Round 3)."""
    lines = [
        "You are the Arbiter in a bounded risk debate. Read both arguments "
        "and rule finally. Do not re-open the debate.",
        "Account L-0042: model band=Very High, auditor view=Low risk.",
        'Round 1 (Skeptic, model=qwen3.6-flash): "p_default is inflated by '
        'short loan age; delinquency is early-cycle and may cure." '
        "(confidence=0.72).",
        'Round 2 (Actuary, model=qwen3.7-plus): "UPHOLD: 31-120 delinquency '
        'with grade E and dti=0.38 is a strong default signal; loan age does '
        'not override the cumulative risk indicators." (confidence=0.81).',
        "Rule which side wins, or escalate if genuinely uncertain. Reply "
        "with ONLY a JSON object, no prose, exactly this shape:",
        '{"ruling": "uphold|override|escalate", "confidence": 0.0-1.0, '
        '"rationale": "one-sentence decision", "evidence": ["fact1"]}',
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sub-tests
# ---------------------------------------------------------------------------
class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.passed = False
        self.detail = ""
        self.latency: float | None = None
        self.raw_response: str | None = None
        self.parsed: object | None = None
        self.parse_error: str | None = None


def _timed_complete(llm: QwenLLM, prompt: str) -> tuple[str, float]:
    """Call llm.complete() and return (response_text, elapsed_seconds)."""
    t0 = time.perf_counter()
    resp = llm.complete(prompt)
    elapsed = time.perf_counter() - t0
    return resp, elapsed


def test_auditor_view(base_llm: QwenLLM, tier: str, print_raw: bool = False) -> TestResult:
    """Risk Auditor challenge → _parse_view_json."""
    tr = TestResult(f"Auditor view [{tier}]")
    brain = base_llm.with_model(qwen_tier(tier))
    prompt = auditor_challenge_prompt()
    try:
        raw, elapsed = _timed_complete(brain, prompt)
    except Exception as exc:
        tr.detail = f"API call failed: {exc}"
        tr.parse_error = traceback.format_exc()
        return tr
    tr.latency = elapsed
    tr.raw_response = raw
    if print_raw:
        print(f"\n{C.BOLD}--- RAW API RESPONSE (Auditor / {tier}) ---{C.RESET}")
        print(raw if raw else "(empty)")
        print(f"{C.BOLD}--- END RAW ---{C.RESET}\n")
    parsed = _parse_view_json(raw)
    tr.parsed = parsed
    if parsed is None:
        tr.detail = (
            f"_parse_view_json returned None for response "
            f"(len={len(raw)}): {raw[:200]!r}"
        )
        return tr
    view, confidence, claim, evidence = parsed
    tr.passed = True
    tr.detail = (
        f"view={view!r} confidence={confidence} claim={claim[:80]!r} "
        f"evidence={evidence[:3]}"
    )
    return tr


def test_actuary_verdict(base_llm: QwenLLM, tier: str) -> TestResult:
    """Actuary rebuttal → _parse_verdict_json."""
    tr = TestResult(f"Actuary verdict [{tier}]")
    brain = base_llm.with_model(qwen_tier(tier))
    prompt = actuary_defend_prompt()
    try:
        raw, elapsed = _timed_complete(brain, prompt)
    except Exception as exc:
        tr.detail = f"API call failed: {exc}"
        tr.parse_error = traceback.format_exc()
        return tr
    tr.latency = elapsed
    tr.raw_response = raw
    parsed = _parse_verdict_json(raw)
    tr.parsed = parsed
    if parsed is None:
        tr.detail = (
            f"_parse_verdict_json returned None for response "
            f"(len={len(raw)}): {raw[:200]!r}"
        )
        return tr
    verdict, confidence, claim, evidence = parsed
    tr.passed = True
    tr.detail = (
        f"verdict={verdict!r} confidence={confidence} claim={claim[:80]!r} "
        f"evidence={evidence[:3]}"
    )
    return tr


def test_arbiter_ruling(base_llm: QwenLLM, tier: str, print_raw: bool = False) -> TestResult:
    """Arbiter ruling → _parse_ruling_json."""
    tr = TestResult(f"Arbiter ruling [{tier}]")
    brain = base_llm.with_model(qwen_tier(tier))
    prompt = arbiter_ruling_prompt()
    try:
        raw, elapsed = _timed_complete(brain, prompt)
    except Exception as exc:
        tr.detail = f"API call failed: {exc}"
        tr.parse_error = traceback.format_exc()
        return tr
    tr.latency = elapsed
    tr.raw_response = raw
    if print_raw:
        print(f"\n{C.BOLD}--- RAW API RESPONSE (Arbiter / {tier}) ---{C.RESET}")
        print(raw if raw else "(empty)")
        print(f"{C.BOLD}--- END RAW ---{C.RESET}\n")
    parsed = _parse_ruling_json(raw)
    tr.parsed = parsed
    if parsed is None:
        tr.detail = (
            f"_parse_ruling_json returned None for response "
            f"(len={len(raw)}): {raw[:200]!r}"
        )
        return tr
    ruling, confidence, rationale, evidence = parsed
    tr.passed = True
    tr.detail = (
        f"ruling={ruling!r} confidence={confidence} rationale={rationale[:80]!r} "
        f"evidence={evidence[:3]}"
    )
    return tr


def test_tier_clones(base_llm: QwenLLM) -> TestResult:
    """Verify with_model() produces a usable clone for each tier."""
    tr = TestResult("Model tiering (with_model clones)")
    errors: list[str] = []
    for tier in ("flash", "plus", "max"):
        model_id = qwen_tier(tier)
        clone = base_llm.with_model(model_id)
        clone_name = getattr(clone, "model_name", None)
        if clone is base_llm:
            errors.append(f"{tier}: with_model returned self (not a clone)")
        elif clone_name != model_id:
            errors.append(f"{tier}: clone model_name={clone_name!r} expected={model_id!r}")
        elif not hasattr(clone, "_client") or clone._client is not base_llm._client:
            errors.append(f"{tier}: clone does not share the same _client")
    if errors:
        tr.detail = "; ".join(errors)
    else:
        tr.passed = True
        names = {t: getattr(base_llm.with_model(qwen_tier(t)), "model_name") for t in ("flash", "plus", "max")}
        tr.detail = f"clones ok → {names}"
    return tr


def test_tier_live_ping(base_llm: QwenLLM) -> list[TestResult]:
    """Make one trivial live call per tier to confirm each model resolves."""
    results: list[TestResult] = []
    ping_prompt = (
        'Reply with ONLY this JSON (no prose): '
        '{"status": "ok"}'
    )
    for tier in ("flash", "plus", "max"):
        tr = TestResult(f"Tier live ping [{tier}]")
        model_id = qwen_tier(tier)
        brain = base_llm.with_model(model_id)
        try:
            raw, elapsed = _timed_complete(brain, ping_prompt)
            tr.latency = elapsed
            tr.raw_response = raw
            if raw and raw.strip():
                tr.passed = True
                tr.detail = f"model={model_id} responded ({len(raw)} chars) in {elapsed:.2f}s"
            else:
                tr.detail = f"model={model_id} returned EMPTY response"
        except Exception as exc:
            tr.detail = f"model={model_id} call failed: {exc}"
            tr.parse_error = traceback.format_exc()
        results.append(tr)
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        print(_fail("DASHSCOPE_API_KEY not found in .env or environment."))
        print(_info("Set it in .env or export DASHSCOPE_API_KEY=sk-..."))
        return 2

    print(f"{C.BOLD}{'=' * 72}{C.RESET}")
    print(f"{C.BOLD}WASPADA — Live Qwen API Smoke Test{C.RESET}")
    print(f"{C.BOLD}{'=' * 72}{C.RESET}")
    print(_info(f"Key loaded: {api_key[:6]}...{api_key[-4:]} ({len(api_key)} chars)"))
    print(_info(f"Repo root: {_REPO_ROOT}"))

    # Resolve the tier model ids for logging
    tiers = {t: qwen_tier(t) for t in ("flash", "plus", "max")}
    print(_info(f"Tier model ids: {tiers}"))

    # Instantiate the real QwenLLM with json_mode (debate protocol)
    try:
        base_llm = QwenLLM(api_key=api_key, json_mode=True)
    except Exception as exc:
        print(_fail(f"QwenLLM construction failed: {exc}"))
        traceback.print_exc()
        return 3
    print(_info(f"Base LLM: model={base_llm.model_name} base_url configured, json_mode={base_llm.json_mode}"))

    all_results: list[TestResult] = []

    # 0. Tier clone verification (no network)
    print(f"\n{C.BOLD}[1] Model tiering — with_model() clones{C.RESET}")
    tr_tiers = test_tier_clones(base_llm)
    all_results.append(tr_tiers)
    print((_pass if tr_tiers.passed else _fail)(tr_tiers.detail))

    # 1. Tier live ping (one trivial call per tier)
    print(f"\n{C.BOLD}[2] Tier live ping — one trivial call per model{C.RESET}")
    ping_results = test_tier_live_ping(base_llm)
    for tr in ping_results:
        lat = f" ({tr.latency:.2f}s)" if tr.latency is not None else ""
        print((_pass if tr.passed else _fail)(tr.detail + lat))
    all_results.extend(ping_results)

    # 2. Auditor (Round 1) — flash tier
    print(f"\n{C.BOLD}[3] Risk Auditor challenge → _parse_view_json  (flash tier){C.RESET}")
    tr_aud = test_auditor_view(base_llm, "flash", print_raw=True)
    all_results.append(tr_aud)
    lat = f" [{tr_aud.latency:.2f}s]" if tr_aud.latency is not None else ""
    print((_pass if tr_aud.passed else _fail)(tr_aud.detail + lat))

    # 3. Actuary (Round 2) — plus tier
    print(f"\n{C.BOLD}[4] Actuary rebuttal → _parse_verdict_json  (plus tier){C.RESET}")
    tr_act = test_actuary_verdict(base_llm, "plus")
    all_results.append(tr_act)
    lat = f" [{tr_act.latency:.2f}s]" if tr_act.latency is not None else ""
    print((_pass if tr_act.passed else _fail)(tr_act.detail + lat))

    # 4. Arbiter (Round 3) — max tier
    print(f"\n{C.BOLD}[5] Arbiter ruling → _parse_ruling_json  (max tier){C.RESET}")
    tr_arb = test_arbiter_ruling(base_llm, "max", print_raw=True)
    all_results.append(tr_arb)
    lat = f" [{tr_arb.latency:.2f}s]" if tr_arb.latency is not None else ""
    print((_pass if tr_arb.passed else _fail)(tr_arb.detail + lat))

    # ---- Summary ----
    print(f"\n{C.BOLD}{'=' * 72}{C.RESET}")
    print(f"{C.BOLD}SUMMARY{C.RESET}")
    print(f"{C.BOLD}{'=' * 72}{C.RESET}")
    n_pass = sum(1 for r in all_results if r.passed)
    n_fail = sum(1 for r in all_results if not r.passed)
    for r in all_results:
        lat_str = f"{r.latency:>5.2f}s" if r.latency is not None else "  —  "
        status = f"{C.GREEN}PASS{C.RESET}" if r.passed else f"{C.RED}FAIL{C.RESET}"
        print(f"  {status}  {lat_str}  {r.name}")
    print(f"\n  Total: {n_pass} passed, {n_fail} failed out of {len(all_results)}")

    # Latency table for demo planning
    print(f"\n{C.BOLD}Latency per call (demo planning){C.RESET}")
    print(f"  {'Test':<40} {'Latency':>8}")
    print(f"  {'-' * 50}")
    for r in all_results:
        if r.latency is not None:
            print(f"  {r.name:<40} {r.latency:>7.2f}s")

    # If anything failed, dump raw responses + tracebacks for diagnosis
    failures = [r for r in all_results if not r.passed]
    if failures:
        print(f"\n{C.BOLD}FAILURE DIAGNOSTICS{C.RESET}")
        for r in failures:
            print(f"\n{C.RED}--- {r.name} ---{C.RESET}")
            print(f"  Detail: {r.detail}")
            if r.raw_response is not None:
                print(f"  Raw response (len={len(r.raw_response)}):")
                print(f"    {r.raw_response[:500]!r}")
            if r.parse_error:
                print(f"  Traceback:\n{r.parse_error}")

    print()
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
