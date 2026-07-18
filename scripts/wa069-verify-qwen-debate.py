"""WA-069 verification — real Qwen debate (manual dispute + Actuary + Arbiter).

Runs a small local pipeline (mock for data + risk model) against a synthetic
snapshot that produces a clear Very High account, then uses the real Qwen brain
for the Actuary rebuttal and the Arbiter ruling. This proves the 3-round debate
protocol works with a live LLM without waiting for a full portfolio run.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

import numpy as np
import pyarrow as pa

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from waspada.agents.analytics import AnalyticsAgent
from waspada.agents.arbiter import ArbiterAgent
from waspada.agents.data_engineer import DataEngineerAgent
from waspada.agents.ingest import IngestAgent
from waspada.agents.llm import MockLLM, QwenLLM
from waspada.agents.protocol import AgentContext, Dispute, DisputeRound
from waspada.agents.risk_auditor import RiskAuditorAgent
from waspada.agents.risk_model import RiskModelAgent
from waspada.schema import RawLoans, schema_from_dataclass


def _build_raw_table() -> pa.Table:
    """One very risky account and 49 normal ones so the model gets a clean signal."""
    rng = np.random.default_rng(42)
    rows = []
    for i in range(60):
        risky = (i < 20)  # 20 risky, 40 safe
        rate = 26.0 if risky else float(rng.uniform(5, 12))
        dti = 34.0 if risky else float(rng.uniform(3, 18))
        grade = "E" if risky else "B"
        op = 0.9 if risky else float(rng.uniform(0.1, 0.4))
        tp = 0.05 if risky else float(rng.uniform(0.5, 1.0))
        status = "Charged Off" if risky else "Current"
        rows.append({
            "loan_id": f"LN{i:04d}",
            "amount": float(rng.uniform(3000, 20000)),
            "term": 36,
            "rate": rate,
            "grade": grade,
            "annual_income": float(rng.uniform(30000, 100000)),
            "dti": dti,
            "issue_date": dt.date(2022, 1, 1),
            "purpose": "debt_consolidation" if risky else "credit_card",
            "region": "Banten",
            "outstanding_principal": float(rng.uniform(500, 3000)) * op,
            "total_paid": float(rng.uniform(500, 3000)) * tp,
            "current_status": status,
        })
    import dataclasses
    cols = {f.name: [] for f in dataclasses.fields(RawLoans)}
    for r in rows:
        for name in cols:
            cols[name].append(r[name])
    return pa.table(cols, schema=schema_from_dataclass(RawLoans))


def run() -> int:
    if not os.environ.get("DASHSCOPE_API_KEY"):
        print("[qwen debate] DASHSCOPE_API_KEY not set — skip")
        return 0

    qwen = QwenLLM(json_mode=True)

    print("[qwen debate] building table and scoring...")
    raw = _build_raw_table()
    ctx = AgentContext(lane="collections", data_handles={})
    fetch = (lambda tbl: (lambda *, lane="collections", limit=None: tbl))(raw)
    ingest = IngestAgent(MockLLM())
    ingest.register_tool("fetch", fetch)
    ctx = ctx.with_result(ingest.run(ctx))
    print("  ingest:", ctx.prior_results[-1].status, ctx.prior_results[-1].notes)
    ctx = ctx.with_result(AnalyticsAgent(MockLLM(), as_of=dt.date(2024, 12, 1)).run(ctx))
    print("  analytics:", ctx.prior_results[-1].status, ctx.prior_results[-1].notes)
    ctx = ctx.with_result(RiskModelAgent(MockLLM()).run(ctx))
    print("  risk_model:", ctx.prior_results[-1].status, ctx.prior_results[-1].notes)

    scored = ctx.data_handles["scored_accounts"]
    frame = ctx.data_handles["feature_frame"]
    loan_id = scored.column("loan_id")[0].as_py()
    model_band = scored.column("score_band")[0].as_py()
    p_default = scored.column("p_default")[0].as_py()
    print(f"[qwen debate] selected loan_id={loan_id} band={model_band} p_default={p_default:.4f}")

    # Manually open the dispute to guarantee a Round 1 challenge.
    dispute = Dispute(
        loan_id=loan_id,
        opened_by="risk_auditor",
        model_band=model_band,
        auditor_view="Low",
        rounds=[DisputeRound(
            round_no=1, speaker="risk_auditor", model="qwen",
            claim="payment_ratio is high relative to the default band",
            confidence=0.8, evidence=["payment_ratio=0.95"],
        )],
    )

    print("[qwen debate] Actuary rebuttal (Qwen)...")
    actuary = RiskModelAgent(qwen)
    r2 = actuary.defend_score(dispute, scored, frame)
    print(f"  Round 2: speaker={r2.speaker} confidence={r2.confidence}")
    print(f"  claim: {r2.claim}")
    print(f"  evidence: {r2.evidence}")
    dispute.rounds.append(r2)

    print("[qwen debate] Arbiter ruling (Qwen)...")
    arbiter = ArbiterAgent(qwen)
    ruling, rationale, confidence, r3 = arbiter.rule(dispute)
    print(f"  Ruling: {ruling} (confidence={confidence})")
    print(f"  Rationale: {rationale}")
    print(f"  Round 3: speaker={r3.speaker} claim={r3.claim}")
    dispute.rounds.append(r3)
    dispute.resolution = "upheld" if ruling == "uphold" else "overridden" if ruling == "override" else "escalated_approved"
    dispute.resolved_by = "arbiter" if ruling in ("uphold", "override") else "human"

    print(f"[qwen debate] Final resolution: {dispute.resolution} by {dispute.resolved_by}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
