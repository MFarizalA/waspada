"""WA-069 verification — MCP stdio evidence parity.

Runs a local mock pipeline to build ScoredAccounts + FeatureFrame, writes them
to parquet, spawns the real MCP server subprocess, and checks that the
StdioClient tool returns match the in-process AnalyticsStore baseline.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile

import pyarrow.parquet as pq

from waspada.agents.__main__ import _sample_raw_table
from waspada.agents.base import ApprovalGate
from waspada.agents.data_engineer import DataEngineerAgent
from waspada.agents.llm import MockLLM
from waspada.agents.orchestrator import Orchestrator
from waspada.agents.protocol import AgentContext, Status
from waspada.mcp.client import StdioClient
from waspada.mcp.store import AnalyticsStore


def run() -> int:
    print("[mcp verify] running local mock pipeline...")
    raw = _sample_raw_table(n=200)
    orch = Orchestrator(MockLLM(), as_of=dt.date(2024, 12, 1), top_n=20)
    orch.gate = ApprovalGate(auto_approve=True)

    stub = (lambda tbl: (lambda *, lane="collections", limit=None: tbl))(raw)
    orig_build = orch._build_agents

    def build_with_stub():
        agents = orig_build()
        for a in agents:
            if isinstance(a, DataEngineerAgent):
                a.register_tool("fetch", stub)
        return agents

    orch._build_agents = build_with_stub
    ctx = AgentContext(lane="collections", data_handles={})
    result = orch.run(ctx)
    if result.status not in (Status.OK, Status.DISPUTED):
        print(f"[mcp verify] pipeline failed: {result.notes}")
        return 1

    final = getattr(orch, "_final_ctx", ctx)
    scored = final.data_handles["scored_accounts"]
    features = final.data_handles["feature_frame"]
    analyst = final.data_handles.get("analyst_aggregates")

    loan_id = str(features.column("loan_id")[0].as_py())

    with tempfile.TemporaryDirectory() as td:
        scored_path = os.path.join(td, "scored.parquet")
        features_path = os.path.join(td, "features.parquet")
        pq.write_table(scored, scored_path)
        pq.write_table(features, features_path)

        print("[mcp verify] in-process baseline...")
        store = AnalyticsStore(scored, features, analyst)
        baseline_stats = store.portfolio_stats()
        baseline_row = store.lookup_account(loan_id)

        print("[mcp verify] MCP stdio client...")
        with StdioClient(scored_path=scored_path, features_path=features_path) as client:
            client_stats = client.portfolio_stats()
            client_row = client.lookup_account(loan_id)

    print(f"[mcp verify] baseline stats: {baseline_stats}")
    print(f"[mcp verify] client   stats: {client_stats}")
    print(f"[mcp verify] baseline row: {baseline_row}")
    print(f"[mcp verify] client   row: {client_row}")

    # The StdioClient loads only scored+features parquet, so analyst_aggregates
    # are not present. Compare the core evidence numbers it does serve.
    baseline_core = {k: v for k, v in baseline_stats.items() if k != "analyst_aggregates"}
    if client_stats != baseline_core:
        print("[mcp verify] FAIL: portfolio_stats mismatch")
        return 1
    if client_row != baseline_row:
        print("[mcp verify] FAIL: lookup_account mismatch")
        return 1

    print("[mcp verify] OK: MCP stdio evidence parity verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
