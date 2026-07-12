"""WASPADA Cloud Run API server.

Serves the pre-built dashboard statically + exposes live pipeline endpoints:

  GET  /              → dashboard (dashboard/dist/index.html)
  GET  /api/health    → {"status": "ok"}
  POST /api/run       → runs the orchestrated agent pipeline on a synthetic
                        snapshot (offline, no BQ needed), returns DashboardPayload + report
  GET  /api/run/stream → SSE stream of the debate rounds / resolutions (WA-022)

The pipeline runs on a small synthetic RawLoans snapshot so the demo is fast
(~3-5s) and has no external dependencies. The dashboard fixture shows the
real BQ-generated payload (1M loans) for the static view.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# Ensure the waspada package is importable
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from waspada.agents.orchestrator import Orchestrator
from waspada.agents.base import ApprovalGate
from waspada.agents.llm import MockLLM, get_llm
from waspada.agents.protocol import AgentContext

# Auth (WA-028): JWT gate on protected routes + auth router.
# ``init_db()`` is idempotent — it ensures the users / reset_tokens tables
# exist on the configured store (ApsaraDB RDS PostgreSQL via DATABASE_URL,
# or the SQLite local-dev fallback) before we try to seed.
from api import db as db_mod
from api.auth import current_user, current_user_ws, router as auth_router, seed_default_user


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables if needed, then seed a demo analyst on startup.

    Idempotent: safe to run on every cold start against the same RDS instance.
    """
    db_mod.init_db()
    seed_default_user()
    yield


app = FastAPI(title="WASPADA API", version="1.0.0", lifespan=lifespan)
app.include_router(auth_router)

# --- Serve the dashboard static files ---
_DASHBOARD_DIST = _REPO / "dashboard" / "dist"
if _DASHBOARD_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(_DASHBOARD_DIST / "assets")), name="assets")

# --- Serve the committed fixture (dashboard fetches this as a static file,
# matching how Vite's dev server serves dashboard/public/ at the root) ---
_FIXTURES_DIR = _REPO / "dashboard" / "fixtures"
if _FIXTURES_DIR.exists():
    app.mount("/fixtures", StaticFiles(directory=str(_FIXTURES_DIR)), name="fixtures")


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


# --------------------------------------------------------------------------- #
# Shared demo-orchestrator builder (used by /api/run and /api/run/stream).
# --------------------------------------------------------------------------- #
def _build_demo_orchestrator(
    brain: str = "mock",
    *,
    on_round_complete: Optional[Callable[[Any, Any], None]] = None,
    on_dispute_resolved: Optional[Callable[[Any], None]] = None,
) -> Orchestrator:
    """Build an offline demo orchestrator with a stubbed fetch and auto-approve gate.

    ``brain`` selects the reasoning LLM (``mock`` default; ``qwen`` opt-in).
    The streaming hooks are passed straight through to ``Orchestrator``; when
    None the orchestrator behaves exactly as before.
    """
    from waspada.agents.__main__ import _sample_raw_table
    from waspada.agents.data_engineer import DataEngineerAgent

    llm = get_llm(brain) if brain and brain != "mock" else MockLLM()
    orch = Orchestrator(
        llm,
        as_of=dt.date(2024, 12, 1),
        top_n=20,
        on_round_complete=on_round_complete,
        on_dispute_resolved=on_dispute_resolved,
    )
    orch.gate = ApprovalGate(auto_approve=True)

    sample = _sample_raw_table(n=200)
    _stub = (lambda tbl: (lambda *, lane="collections", limit=None: tbl))(sample)

    _orig_build = orch._build_agents
    def _build_with_stub():
        agents = _orig_build()
        for a in agents:
            if isinstance(a, DataEngineerAgent):
                a.register_tool("fetch", _stub)
        return agents
    orch._build_agents = _build_with_stub  # type: ignore

    return orch


def _collect_pipeline_steps(orch: Orchestrator, ctx: AgentContext) -> Dict[str, Any]:
    """Package the payload, report, alert summary and step logs from a run."""
    final_ctx = getattr(orch, "_final_ctx", ctx)
    result = orch._final_result if hasattr(orch, "_final_result") else None
    payload = final_ctx.data_handles.get(result.artifact_ref) if result else None

    if payload is None:
        return {"error": "No payload produced"}

    report = orch.report(payload)
    summary = final_ctx.data_handles.get("alert_summary", "")

    pipeline_steps = []
    for agent in getattr(orch, "_pipeline_agents", []):
        for s in getattr(agent, "steps", []):
            pipeline_steps.append({
                "agent": s.agent, "action": s.action, "status": s.status,
                "notes": s.notes, "rationale": s.rationale, "auto": s.auto,
            })
    orch_steps = [
        {"agent": s.agent, "action": s.action, "status": s.status, "notes": s.notes, "auto": s.auto}
        for s in orch.steps
    ]

    return {
        "payload": payload,
        "report": report,
        "alert_summary": summary,
        "steps": orch_steps + pipeline_steps,
    }


@app.post("/api/run")
async def run_pipeline(brain: str = "mock", _user: dict = Depends(current_user)):
    """Run the full agent pipeline on a synthetic snapshot.

    Returns the DashboardPayload + the plain-language analyst report.
    Runs offline (no BQ, no GPU, no network) in ~3-5 seconds by default.

    ``brain`` selects the reasoning LLM for the risk-auditor negotiation
    step (``mock`` default = fast/free/deterministic; ``qwen`` = real Qwen
    calls via DashScope, opt-in only — this adds real network latency and
    should not be the default every visitor's first click triggers).
    """
    orch = _build_demo_orchestrator(brain)
    ctx = AgentContext(
        lane="collections",
        data_handles={},
        meta={"source": "cloud-run-demo"},
    )

    orch.plan("collections")
    result = orch.run(ctx)

    if not result.ok:
        return JSONResponse(
            {"error": f"Pipeline failed: {result.notes}"},
            status_code=500,
        )

    # Stash result on the orchestrator so the collector can read artifact_ref.
    orch._final_result = result  # type: ignore[attr-defined]
    out = _collect_pipeline_steps(orch, ctx)
    if "error" in out:
        return JSONResponse(out, status_code=500)
    return out


@app.get("/api/run/stream")
async def run_stream(brain: str = "mock", _user: dict = Depends(current_user_ws)):
    """Stream the agent debate live via Server-Sent Events.

    The orchestrator's ``on_round_complete`` / ``on_dispute_resolved`` hooks
    are wired to an ``asyncio.Queue``. The sync pipeline runs in a worker
    thread; each hook schedules a JSON event onto the queue from the event-loop
    thread. The async generator yields ``data: <json>\n\n`` frames matching the
    frozen shape in ``dashboard/src/lib/useLiveDebateStream.ts``.

    With ``brain=mock`` the Risk Auditor uses the default canned brain, so
    disputes are unlikely and the stream typically ends with a single ``done``.
    A visible debate requires ``brain=qwen`` or a scripted mock in tests.
    """
    q: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _round_event(d: Any, r: Any) -> Dict[str, Any]:
        return {
            "type": "round",
            "round_no": r.round_no,
            "speaker": r.speaker,
            "model": r.model,
            "claim": r.claim,
            "confidence": r.confidence,
            "evidence": list(r.evidence),
        }

    def _resolution_event(d: Any) -> Dict[str, Any]:
        ddict = d.to_dict()
        return {
            "type": "resolution",
            "loan_id": ddict["loan_id"],
            "resolution": ddict["resolution"],
            "resolved_by": ddict["resolved_by"],
            "rationale": ddict["rationale"],
        }

    def on_round_complete(d: Any, r: Any) -> None:
        loop.call_soon_threadsafe(q.put_nowait, _round_event(d, r))

    def on_dispute_resolved(d: Any) -> None:
        loop.call_soon_threadsafe(q.put_nowait, _resolution_event(d))

    orch = _build_demo_orchestrator(
        brain,
        on_round_complete=on_round_complete,
        on_dispute_resolved=on_dispute_resolved,
    )
    ctx = AgentContext(
        lane="collections",
        data_handles={},
        meta={"source": "sse-stream"},
    )

    async def runner() -> None:
        try:
            await asyncio.to_thread(orch.run, ctx)
        except Exception as exc:  # pragma: no cover - defensive; stream must close
            # Log locally but never crash the SSE connection; the frontend falls
            # back to the request/response path on disconnect/error.
            print(f"stream run error: {exc}")
        finally:
            await q.put({"type": "done"})

    async def event_gen():
        task = asyncio.create_task(runner())
        try:
            while True:
                ev = await q.get()
                yield f"data: {json.dumps(ev)}\n\n"
                if ev.get("type") == "done":
                    break
        finally:
            task.cancel()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/payload")
async def get_payload():
    """Return the pre-baked BQ-generated payload (the real 1M-loan run)."""
    fixture = _REPO / "dashboard" / "fixtures" / "sample-payload.json"
    if fixture.exists():
        return json.loads(fixture.read_text(encoding="utf-8"))
    return JSONResponse({"error": "No payload available"}, status_code=404)


if __name__ == "__main__":
    uvicorn.run("api.main:app", host="0.0.0.0", port=8080, reload=False)
