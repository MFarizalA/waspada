"""WASPADA Function Compute API server.

Serves the pre-built dashboard statically + exposes live pipeline endpoints:

  GET  /              → dashboard (dashboard/dist/index.html)
  GET  /api/health    → {"status": "ok"}
  POST /api/run       → runs the orchestrated agent pipeline on the real OSS
                        RawLoans snapshot, returns DashboardPayload + report
  GET  /api/run/stream → SSE stream of the debate rounds / resolutions (WA-022)

Data is always read from OSS (``waspada.data.oss.fetch_loans``). ``brain=mock``
selects the offline LLM brain for the agent-society debate; it does NOT
select the data source. There is no synthetic fallback in the API.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
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

from waspada.data import OSSClient
from waspada.agents.dispute_memory import get_memory_backend

# Auth (WA-028): JWT gate on protected routes + auth router.
# ``init_db()`` is idempotent — it ensures the users / reset_tokens tables
# exist on the configured store (ApsaraDB RDS MySQL via DATABASE_URL,
# or the SQLite local-dev fallback) before we try to seed.
from api import db as db_mod
from api.auth import (
    current_user,
    current_user_ws,
    router as auth_router,
    seed_default_user,
    validate_jwt_secret,
)

# Startup guard (WA-040): refuse to boot outside dev with a weak/missing JWT
# secret. Runs at import time so a misconfigured deploy crashes immediately —
# before binding a port or seeding users — instead of silently signing tokens.
validate_jwt_secret()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables if needed, seed a demo analyst, and probe OSS reachability.

    Idempotent: safe to run on every cold start against the same RDS instance.
    The JWT-secret guard re-runs here so FC cold starts (which may import the
    module from a cached artifact) still catch a missing secret before serving.

    OSS is probed at startup: if the bucket/object is unreachable we still boot
    so health checks pass, but we mark the data source unavailable and ``/api/run``
    returns a clear 503. NEVER silent synthetic fallback.
    """
    validate_jwt_secret()
    db_mod.init_db()
    seed_default_user()
    app.state.oss_available, app.state.oss_detail = _probe_oss()
    yield


def _probe_oss() -> tuple[bool, str]:
    """Return (reachable, detail). Detail is empty when reachable."""
    try:
        client = OSSClient()
        meta = client.object_meta()
        return True, f"OK (size={meta.get('size_bytes', '?')} bytes)"
    except Exception as exc:  # pragma: no cover - infra-dependent
        return False, f"data source unavailable: {exc}"


def _data_source_unavailable() -> JSONResponse:
    detail = getattr(app.state, "oss_detail", "data source unavailable")
    return JSONResponse({"detail": detail}, status_code=503)


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
    """Serve the dashboard index page so the browser RENDERS it.

    Uses HTMLResponse, not FileResponse: the deployed Starlette's FileResponse
    emits ``Content-Disposition: attachment``, which made the live URL *download*
    index.html instead of showing the dashboard. HTMLResponse never sets a
    Content-Disposition header, so the SPA always renders. (Supersedes the
    earlier ``content_disposition_type="inline"`` attempt, which depends on the
    Starlette version behaving; this doesn't.)
    """
    index = _DASHBOARD_DIST / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text(encoding="utf-8"))
    return JSONResponse({"error": "Dashboard not built. Run: cd dashboard && npx vite build"}, status_code=404)


@app.get("/api/health")
async def health():
    detail = getattr(app.state, "oss_detail", "")
    return {"status": "ok", "service": "waspada", "oss": detail}


# --------------------------------------------------------------------------- #
# Shared orchestrator builder (used by /api/run and /api/run/stream).
# --------------------------------------------------------------------------- #
def _build_orchestrator(
    brain: str = "mock",
    *,
    on_round_complete: Optional[Callable[[Any, Any], None]] = None,
    on_dispute_resolved: Optional[Callable[[Any], None]] = None,
) -> Orchestrator:
    """Build a real-data orchestrator with auto-approve gate.

    ``brain`` selects the reasoning LLM (``mock`` default; ``qwen`` opt-in).
    The streaming hooks are passed straight through to ``Orchestrator``; when
    None the orchestrator behaves exactly as before.

    The data-engineer agent's default ``fetch`` tool already resolves to
    ``waspada.data.oss.fetch_loans``; no tool injection is required.
    """
    import os

    from waspada.policy import load_policy

    llm = get_llm(brain) if brain and brain != "mock" else MockLLM()
    orch = Orchestrator(
        llm,
        as_of=dt.date(2024, 12, 1),
        top_n=20,
        memory_backend=get_memory_backend(),
        policy=load_policy(os.environ.get("WASPADA_POLICY_FILE")),
        on_round_complete=on_round_complete,
        on_dispute_resolved=on_dispute_resolved,
    )
    orch.gate = ApprovalGate(auto_approve=True)
    return orch


def _ship_audit(orch: Orchestrator, ctx: AgentContext) -> None:
    """Ship the run's step log to the audit stream (WA-023). Fail-safe:
    SLS when configured, else a local file; never raises into the request."""
    from waspada.audit.sls import get_audit_sink, ship_run_audit

    run_id = (getattr(ctx, "meta", None) or {}).get("run_id") or uuid.uuid4().hex[:12]
    ship_run_audit(orch, run_id, get_audit_sink(run_id))


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


def _brain_error(brain: str, exc: Exception) -> JSONResponse:
    """A clean 503 when the reasoning brain can't be built or reached.

    Selecting ``brain=qwen`` without a working ``DASHSCOPE_API_KEY`` (or with
    DashScope unreachable) must NOT surface as a bare 500 — return an actionable
    message the dashboard can display and fall back from.
    """
    return JSONResponse(
        {
            "error": (
                f"Reasoning brain '{brain}' is unavailable "
                f"({type(exc).__name__}: {exc}). "
                f"Set DASHSCOPE_API_KEY for Qwen, or use the mock brain."
            ),
            "brain": brain,
        },
        status_code=503,
    )


@app.post("/api/run")
async def run_pipeline(brain: str = "mock", _user: dict = Depends(current_user)):
    """Run the full agent pipeline on the real OSS RawLoans snapshot.

    Returns the DashboardPayload + the plain-language analyst report.
    Data is always read from OSS; ``brain`` selects only the reasoning LLM
    for the risk-auditor negotiation step (``mock`` default = fast/free/
    deterministic; ``qwen`` = real Qwen calls via DashScope, opt-in only).

    If OSS is unreachable at startup, the app boots but ``/api/run`` returns
    **503 with a clear detail** rather than falling back to synthetic data.
    If ``brain=qwen`` is selected but Qwen can't be built/reached (no
    ``DASHSCOPE_API_KEY``, bad key, network down), we return a **503 with a
    clear message** rather than a bare 500.
    """
    if not getattr(app.state, "oss_available", True) is True:
        return _data_source_unavailable()

    ctx = AgentContext(
        lane="collections",
        data_handles={},
        meta={"source": "oss-real", "run_id": uuid.uuid4().hex[:12]},
    )
    try:
        orch = _build_orchestrator(brain)
        orch.plan("collections")
        result = orch.run(ctx)
    except Exception as exc:  # brain unbuildable / unreachable → clean 503, not a 500
        return _brain_error(brain, exc)
    _ship_audit(orch, ctx)  # WA-023: audit stream (fail-safe)

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

    If OSS is unreachable at startup, the app boots but this endpoint returns
    **503 with a clear detail** — never a silent synthetic fallback.
    """
    if not getattr(app.state, "oss_available", True) is True:
        return _data_source_unavailable()

    q: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _round_event(d: Any, r: Any) -> Dict[str, Any]:
        return {
            "type": "round",
            # Which account this turn belongs to. Disputes stream interleaved, so
            # without it a client cannot attribute a round to its debate.
            "loan_id": d.loan_id,
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
            # WA-048: what the ruling actually DID to the account. ``applied`` is
            # False here for a de-escalation still awaiting its human gate — the
            # payload carries the settled value.
            "model_band": ddict["model_band"],
            "revised_band": ddict["revised_band"],
            "applied": ddict["applied"],
        }

    def on_round_complete(d: Any, r: Any) -> None:
        loop.call_soon_threadsafe(q.put_nowait, _round_event(d, r))

    def on_dispute_resolved(d: Any) -> None:
        loop.call_soon_threadsafe(q.put_nowait, _resolution_event(d))

    try:
        orch = _build_orchestrator(
            brain,
            on_round_complete=on_round_complete,
            on_dispute_resolved=on_dispute_resolved,
        )
    except Exception as exc:  # brain unbuildable → clean 503 (EventSource errors → UI falls back)
        return _brain_error(brain, exc)
    ctx = AgentContext(
        lane="collections",
        data_handles={},
        meta={"source": "sse-stream-oss", "run_id": uuid.uuid4().hex[:12]},
    )

    async def runner() -> None:
        try:
            await asyncio.to_thread(orch.run, ctx)
            await asyncio.to_thread(_ship_audit, orch, ctx)  # WA-023 (fail-safe)
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
    """Return the pre-baked payload (the real 1M-loan run)."""
    fixture = _REPO / "dashboard" / "fixtures" / "sample-payload.json"
    if fixture.exists():
        return json.loads(fixture.read_text(encoding="utf-8"))
    return JSONResponse({"error": "No payload available"}, status_code=404)


if __name__ == "__main__":
    uvicorn.run("api.main:app", host="0.0.0.0", port=8080, reload=False)
