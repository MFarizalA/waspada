"""WASPADA Cloud Run API server.

Serves the pre-built dashboard statically + exposes a live pipeline endpoint:

  GET  /              → dashboard (dashboard/dist/index.html)
  GET  /api/health    → {"status": "ok"}
  POST /api/run       → runs the orchestrated agent pipeline on a synthetic
                        snapshot (offline, no BQ needed), returns DashboardPayload + report

The pipeline runs on a small synthetic RawLoans snapshot so the demo is fast
(~3-5s) and has no external dependencies. The dashboard fixture shows the
real BQ-generated payload (1M loans) for the static view.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# Ensure the waspada package is importable
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from waspada.agents.orchestrator import Orchestrator
from waspada.agents.base import ApprovalGate
from waspada.agents.protocol import AgentContext

app = FastAPI(title="WASPADA API", version="1.0.0")

# --- Serve the dashboard static files ---
_DASHBOARD_DIST = _REPO / "dashboard" / "dist"
if _DASHBOARD_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(_DASHBOARD_DIST / "assets")), name="assets")


@app.get("/")
async def dashboard():
    """Serve the dashboard index page."""
    index = _DASHBOARD_DIST / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return JSONResponse({"error": "Dashboard not built. Run: cd dashboard && npx vite build"}, status_code=404)


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "waspada"}


@app.post("/api/run")
async def run_pipeline():
    """Run the full agent pipeline on a synthetic snapshot.

    Returns the DashboardPayload + the plain-language analyst report.
    Runs offline (no BQ, no GPU, no network) in ~3-5 seconds.
    """
    try:
        # Import the synthetic table generator from the CLI module
        from waspada.agents.__main__ import _sample_raw_table, _stub_fetch_factory  # type: ignore
    except ImportError:
        # Fallback: inline the stub
        from waspada.agents.__main__ import _sample_raw_table
        from waspada.agents.ingest import IngestAgent

    import datetime as dt

    # Build the orchestrator with auto-approve (demo mode)
    orch = Orchestrator(
        as_of=dt.date(2024, 12, 1),
        top_n=20,
    )
    orch.gate = ApprovalGate(auto_approve=True)

    ctx = AgentContext(
        lane="collections",
        data_handles={},
        meta={"source": "cloud-run-demo"},
    )

    # Stub the ingest with a synthetic snapshot so no BQ is needed
    sample = _sample_raw_table(n=200)
    _stub = (lambda tbl: (lambda *, lane="collections", limit=None: tbl))(sample)

    from waspada.agents.ingest import IngestAgent
    _orig_build = orch._build_agents
    def _build_with_stub():
        agents = _orig_build()
        for a in agents:
            if isinstance(a, IngestAgent):
                a.register_tool("fetch", _stub)
        return agents
    orch._build_agents = _build_with_stub  # type: ignore

    # Run the pipeline
    orch.plan("collections")
    result = orch.run(ctx)

    if not result.ok:
        return JSONResponse(
            {"error": f"Pipeline failed: {result.notes}"},
            status_code=500,
        )

    # Extract the payload from the orchestrator's context
    final_ctx = getattr(orch, "_final_ctx", ctx)
    payload = final_ctx.data_handles.get(result.artifact_ref)

    if payload is None:
        return JSONResponse(
            {"error": "No payload produced"},
            status_code=500,
        )

    report = orch.report(payload)
    summary = final_ctx.data_handles.get("alert_summary", "")

    # Collect the step log for the audit trail
    steps = []
    for agent in orch._build_agents.__wrapped__() if hasattr(orch._build_agents, '__wrapped__') else []:
        pass  # agents are rebuilt; steps are on the orchestrator
    steps = [
        {"agent": s.agent, "action": s.action, "status": s.status, "notes": s.notes, "auto": s.auto}
        for s in orch.steps
    ]

    return {
        "payload": payload,
        "report": report,
        "alert_summary": summary,
        "steps": steps,
    }


@app.get("/api/payload")
async def get_payload():
    """Return the pre-baked BQ-generated payload (the real 1M-loan run)."""
    fixture = _REPO / "dashboard" / "fixtures" / "sample-payload.json"
    if fixture.exists():
        return json.loads(fixture.read_text(encoding="utf-8"))
    return JSONResponse({"error": "No payload available"}, status_code=404)


if __name__ == "__main__":
    uvicorn.run("api.main:app", host="0.0.0.0", port=8080, reload=False)
