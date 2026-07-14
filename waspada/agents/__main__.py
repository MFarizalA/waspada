"""CLI entry for the WASPADA agent layer (WA-010).

    python -m waspada.agents --lane collections

Runs the full orchestrated pipeline (ingest→analytics→risk-model→insight),
prints the analyst report, and writes the dashboard payload JSON to
``data/dashboard-payload.json``.

Offline by default: when OSS creds are absent the CLI loads a bundled
sample snapshot (``dashboard/fixtures/sample-payload.json`` is the *output*
shape; for the *input* we synthesize a small RawLoans table so the whole
pipeline runs end-to-end without network). When creds are present, the ingest
agent uses the real Alibaba Cloud OSS client.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Optional

import pyarrow as pa

from ..config import COLLECTIONS, LANES
from ..schema import RawLoans, schema_from_dataclass
from .llm import get_llm
from .orchestrator import Orchestrator
from .protocol import AgentContext, Status


def _sample_raw_table(n: int = 200, seed: int = 11) -> pa.Table:
    """A small synthetic RawLoans table for the offline CLI / demo run.

    Two risk classes across multiple vintages so the model trains and the
    pipeline produces a non-trivial work-list. Used when BQ creds are absent.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    issue_years = [2019, 2020, 2021, 2022, 2023]
    rows = []
    for i in range(n):
        iy = int(issue_years[i % len(issue_years)])
        im = int(rng.integers(1, 13))
        risky = rng.random() < 0.5
        if risky:
            rate = float(rng.uniform(18, 28)); dti = float(rng.uniform(22, 35))
            grade = "E"; op = float(rng.uniform(0.5, 0.9)); tp = float(rng.uniform(0.0, 0.3))
            status = "Charged Off"
        else:
            rate = float(rng.uniform(4, 10)); dti = float(rng.uniform(2, 12))
            grade = "A"; op = float(rng.uniform(0.0, 0.3)); tp = float(rng.uniform(0.6, 1.0))
            status = "Current"
        rows.append(dict(
            loan_id=f"LN{i:08d}",
            amount=float(rng.uniform(2000, 25000)),
            term=int(rng.choice([36, 60])),
            rate=rate, grade=grade,
            annual_income=float(rng.uniform(30000, 120000)),
            dti=dti,
            issue_date=dt.date(iy, im, 1),
            purpose=str(rng.choice(["credit_card", "debt_consolidation", "car", "medical"])),
            region=str(rng.choice(["DKI Jakarta", "Jawa Barat", "Jawa Timur", "Banten"])),
            outstanding_principal=float(rng.uniform(100, 5000)) * op,
            total_paid=float(rng.uniform(100, 5000)) * tp,
            current_status=status,
        ))
    import dataclasses
    cols = {f.name: [] for f in dataclasses.fields(RawLoans)}
    for r in rows:
        for name in cols:
            cols[name].append(r[name])
    return pa.table(cols, schema=schema_from_dataclass(RawLoans))


def _oss_configured() -> bool:
    return bool(
        os.environ.get("OSS_BUCKET")
        and os.environ.get("OSS_ENDPOINT")
        and os.environ.get("OSS_KEY")
        and os.environ.get("OSS_ACCESS_KEY_ID")
        and os.environ.get("OSS_ACCESS_KEY_SECRET")
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="waspada.agents", description="Run the WASPADA agent pipeline.")
    parser.add_argument("--lane", default=COLLECTIONS, choices=LANES, help="decision lane")
    parser.add_argument("--as-of", default="2024-12-01", help="snapshot date (YYYY-MM-DD)")
    parser.add_argument("--top-n", type=int, default=50, help="work-list size cap")
    parser.add_argument(
        "--out", default=str(Path("data/dashboard-payload.json")),
        help="where to write the dashboard payload JSON",
    )
    parser.add_argument(
        "--auto-approve", action="store_true",
        help="auto-approve the work-list gate (non-prod smoke run)",
    )
    args = parser.parse_args(argv)

    as_of = dt.date.fromisoformat(args.as_of)
    # WASPADA_LLM_PROVIDER selects the reasoning brain (mock/qwen);
    # defaults to the offline mock so the CLI never reaches for the network
    # unless a caller explicitly opts in via the env var.
    orch = Orchestrator(get_llm(), as_of=as_of, top_n=args.top_n)
    # Auto-approve for the CLI smoke run unless a real gate channel is wired.
    if args.auto_approve or os.environ.get("WASPADA_AUTO_APPROVE", "").strip() in ("1", "true", "yes"):
        from .base import ApprovalGate
        orch.gate = ApprovalGate(auto_approve=True)

    run_id = uuid.uuid4().hex[:12]
    ctx = AgentContext(lane=args.lane, data_handles={}, meta={"as_of": args.as_of, "cli": True, "run_id": run_id})

    # Offline path: stub the data-engineer fetch with a synthetic snapshot when
    # OSS isn't configured, so the CLI runs end-to-end without network.
    if not _oss_configured():
        from .data_engineer import DataEngineerAgent  # local import to avoid cycle
        sample = _sample_raw_table()
        # The orchestrator builds its own agents; register the stub on the
        # data-engineer agent via a tool-injection hook before run().
        # (DataEngineerAgent reads tools["fetch"] for the raw snapshot.)
        _stub_fetch = (lambda tbl: (lambda *, lane="collections", limit=None: tbl))(sample)

        # Patch the build hook so the instance the orchestrator builds picks up
        # the stub. Smallest-correct way to keep the CLI offline.
        _orig_build = orch._build_agents
        def _build_with_stub():
            agents = _orig_build()
            for a in agents:
                if isinstance(a, DataEngineerAgent):
                    a.register_tool("fetch", _stub_fetch)
            return agents
        orch._build_agents = _build_with_stub  # type: ignore[method-assign]
        print(f"[waspada] OSS not configured — using synthetic {sample.num_rows}-row snapshot (offline demo).", file=sys.stderr)

    orch.plan(args.lane)
    result = orch.run(ctx)

    # OK and DISPUTED are both *completions* — a payload was produced. DISPUTED
    # means the Skeptic opened live disputes (the payload carries them in
    # ``agent_dialogue``); only ERROR/BLOCKED are real failures.
    if result.status not in (Status.OK, Status.DISPUTED):
        print(f"[waspada] pipeline did not complete: {result.notes}", file=sys.stderr)
        return 2
    if result.status == Status.DISPUTED:
        n = len(getattr(orch, "_final_ctx", ctx).data_handles.get("risk_disputes") or [])
        print(f"[waspada] run completed with {n} open dispute(s) routed to the gate.", file=sys.stderr)

    # Pull the payload from the final context the orchestrator stashed.
    payload = getattr(orch, "_final_ctx", ctx).data_handles.get(result.artifact_ref)
    if payload is None:
        print("[waspada] no payload produced.", file=sys.stderr)
        return 3

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)

    print(orch.report(payload))
    print(f"[waspada] dashboard payload written to {out_path}")

    # Ship the run's step log to the audit stream (WA-023). Fail-safe: SLS when
    # configured, else a local data/audit/<run_id>.jsonl file; never blocks.
    from ..audit.sls import get_audit_sink, ship_run_audit
    sink = get_audit_sink(run_id)
    n_audit = ship_run_audit(orch, run_id, sink)
    print(f"[waspada] audit: shipped {n_audit} record(s) via {getattr(sink, 'backend', '?')} "
          f"(run_id={run_id})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
