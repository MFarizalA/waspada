"""SLS audit-log stream (WA-023) — thin wrapper on ``aliyun-log-python-sdk``.

A pipeline run records a structured step log on the orchestrator
(``orch.steps``, per-agent ``agent.steps``, ``orch.handoffs``, and the
``Dispute`` records on the final context). This module ships those records to
**Alibaba Simple Log Service** as SQL-queryable structured logs, with the
audit fields the rubric asks for: ``run_id, agent, action, model, tokens,
latency, resolution`` (plus ``status / at / loan_id / notes`` for context).

Design
------
* **Fail-safe, non-negotiable.** SLS being down / unreachable / unconfigured, or
  the SDK not installed, must NEVER break a collections run. Every sink swallows
  its own errors, and :func:`ship_run_audit` swallows everything.
* **Local fallback.** When SLS isn't configured (the offline / CI default), or a
  ship attempt fails, records are written as JSON lines to a local file
  (``data/audit/<run_id>.jsonl``) — a real, inspectable audit artifact.
* **Lazy SDK import.** ``aliyun.log`` is imported only inside the SLS ship path,
  so this module imports cleanly without the SDK (e.g. in CI).
* **No token/latency fabrication.** The current agents don't measure per-call
  tokens or latency, so those fields are emitted as ``None`` rather than faked.
  The field is present (so an SLS query can select it) — the value is honest.

Config (env; the AccessKey pair is shared with OSS, per the ticket)
-------------------------------------------------------------------
``SLS_ENDPOINT``, ``SLS_PROJECT``, ``SLS_LOGSTORE`` +
``OSS_ACCESS_KEY_ID`` / ``OSS_ACCESS_KEY_SECRET``. All five present →
:class:`SLSAuditSink`; otherwise :class:`LocalAuditSink`.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

__all__ = [
    "AuditSink",
    "LocalAuditSink",
    "SLSAuditSink",
    "build_records",
    "get_audit_sink",
    "ship_run_audit",
    "sls_configured",
]

# Default local-fallback directory (gitignored via /data/).
_AUDIT_DIR = os.path.join("data", "audit")

# The audit-record field order the SLS spec asks for; extra context fields are
# appended per record. Kept as a template so every record has the same columns
# (an SLS query can rely on the schema even when a value is None).
_SPEC_FIELDS = ("run_id", "agent", "action", "status", "model", "tokens", "latency", "resolution")


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def sls_configured() -> bool:
    """True only when every var needed to reach SLS is set."""
    return all(
        _env(k)
        for k in ("SLS_ENDPOINT", "SLS_PROJECT", "SLS_LOGSTORE",
                  "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET")
    )


@runtime_checkable
class AuditSink(Protocol):
    """A destination for audit records. Implementations never raise on emit."""

    def emit_many(self, records: List[Dict[str, Any]]) -> None: ...
    def close(self) -> None: ...


# --------------------------------------------------------------------------- #
# Local fallback sink — always available, always safe.
# --------------------------------------------------------------------------- #
class LocalAuditSink:
    """Append audit records as JSON lines to ``data/audit/<run_id>.jsonl``.

    The floor of the fail-safe: this is what an SLS outage degrades to, and the
    default when SLS isn't configured. Any I/O error is swallowed — audit must
    never break the pipeline.
    """

    backend = "local"

    def __init__(self, run_id: str, path: Optional[str] = None) -> None:
        self.run_id = run_id
        self.path = path or os.path.join(_AUDIT_DIR, f"{run_id}.jsonl")

    def emit_many(self, records: List[Dict[str, Any]]) -> None:
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                for r in records:
                    fh.write(json.dumps(r, default=str) + "\n")
        except Exception:  # audit must never break the pipeline
            pass

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# SLS sink — ships to Alibaba Simple Log Service, falls back to local on failure.
# --------------------------------------------------------------------------- #
class SLSAuditSink:
    """Ship records to Alibaba Simple Log Service (``aliyun-log-python-sdk``).

    Fail-safe by construction: the SDK import, client build, and ``put_logs``
    call are all inside one ``try`` — any failure (SDK missing, bad creds, SLS
    unreachable) routes the records to a :class:`LocalAuditSink` instead and
    never raises.
    """

    backend = "sls"

    def __init__(
        self,
        run_id: str,
        *,
        endpoint: Optional[str] = None,
        project: Optional[str] = None,
        logstore: Optional[str] = None,
        access_key_id: Optional[str] = None,
        access_key_secret: Optional[str] = None,
        topic: str = "waspada",
    ) -> None:
        self.run_id = run_id
        self.endpoint = endpoint or _env("SLS_ENDPOINT")
        self.project = project or _env("SLS_PROJECT")
        self.logstore = logstore or _env("SLS_LOGSTORE")
        self.access_key_id = access_key_id or _env("OSS_ACCESS_KEY_ID")
        self.access_key_secret = access_key_secret or _env("OSS_ACCESS_KEY_SECRET")
        self.topic = topic
        self._fallback = LocalAuditSink(run_id)
        self._client: Any = None

    def _client_lazy(self) -> Any:
        if self._client is None:
            from aliyun.log import LogClient  # lazy: only needed on the SLS path
            self._client = LogClient(self.endpoint, self.access_key_id, self.access_key_secret)
        return self._client

    def emit_many(self, records: List[Dict[str, Any]]) -> None:
        try:
            from aliyun.log import LogItem, PutLogsRequest  # lazy
            client = self._client_lazy()
            items = []
            for r in records:
                item = LogItem()
                item.set_contents([(k, "" if v is None else str(v)) for k, v in r.items()])
                items.append(item)
            req = PutLogsRequest(self.project, self.logstore, self.topic, "", items)
            client.put_logs(req)
        except Exception:
            # SLS down / SDK missing / bad creds → local file, never raise.
            self._fallback.emit_many(records)

    def close(self) -> None:
        self._fallback.close()


def get_audit_sink(run_id: str) -> AuditSink:
    """Pick the SLS sink when fully configured, else the local fallback."""
    return SLSAuditSink(run_id) if sls_configured() else LocalAuditSink(run_id)


# --------------------------------------------------------------------------- #
# Record building — read the orchestrator's existing step log into flat records.
# --------------------------------------------------------------------------- #
def _blank(run_id: str) -> Dict[str, Any]:
    rec = {k: None for k in _SPEC_FIELDS}
    rec["run_id"] = run_id
    return rec


def build_records(orch: Any, run_id: str) -> List[Dict[str, Any]]:
    """Flatten an orchestrator's run log into audit records.

    Sources: ``orch.steps`` (orchestration-level), each pipeline agent's own
    ``.steps`` (the negotiation detail), ``orch.handoffs`` (frm→to hops), and
    the ``Dispute`` records on ``orch._final_ctx`` (rounds + terminal
    resolution). Missing attributes are tolerated (``getattr`` guards) so a
    partially-run or halted orchestrator still yields whatever it recorded.
    """
    records: List[Dict[str, Any]] = []

    def _step_record(s: Any) -> Dict[str, Any]:
        rec = _blank(run_id)
        rec.update(
            agent=getattr(s, "agent", None),
            action=getattr(s, "action", None),
            status=getattr(s, "status", None),
            at=getattr(s, "at", None),
            notes=getattr(s, "notes", None),
            auto=getattr(s, "auto", None),
        )
        return rec

    # Orchestration-level steps.
    for s in getattr(orch, "steps", []) or []:
        records.append(_step_record(s))

    # Per-agent steps (the audit detail the API already surfaces).
    for agent in getattr(orch, "_pipeline_agents", []) or []:
        for s in getattr(agent, "steps", []) or []:
            records.append(_step_record(s))

    # Handoffs (frm→to hops).
    for h in getattr(orch, "handoffs", []) or []:
        rec = _blank(run_id)
        result = getattr(h, "result", None)
        rec.update(
            agent=getattr(h, "frm", None),
            action="handoff",
            to=getattr(h, "to", None),
            status=getattr(result, "status", None),
            notes=getattr(h, "rationale", None),
        )
        records.append(rec)

    # Disputes: one record per round (carries the model), plus a terminal
    # resolution record.
    for d in _disputes_from(orch):
        for rd in getattr(d, "rounds", []) or []:
            rec = _blank(run_id)
            rec.update(
                agent=getattr(rd, "speaker", None),
                action=f"dispute_round_{getattr(rd, 'round_no', '')}",
                status="disputed",
                model=getattr(rd, "model", None),
                resolution=getattr(d, "resolution", None) or None,
                loan_id=getattr(d, "loan_id", None),
                confidence=getattr(rd, "confidence", None),
                notes=(getattr(rd, "claim", "") or "")[:200],
            )
            records.append(rec)
        rec = _blank(run_id)
        rec.update(
            agent=getattr(d, "resolved_by", None) or "orchestrator",
            action="dispute_resolved",
            status="disputed",
            resolution=getattr(d, "resolution", None) or None,
            loan_id=getattr(d, "loan_id", None),
            notes=(getattr(d, "rationale", "") or "")[:200],
        )
        records.append(rec)

    return records


def _disputes_from(orch: Any) -> List[Any]:
    ctx = getattr(orch, "_final_ctx", None)
    if ctx is None:
        return []
    handles = getattr(ctx, "data_handles", None) or {}
    return handles.get("risk_disputes") or []


def ship_run_audit(orch: Any, run_id: str, sink: Optional[AuditSink] = None) -> int:
    """Build + ship a run's audit records. Returns the count shipped.

    Fail-safe end to end: any error (record building, the sink, close) is
    swallowed and reported as ``0`` shipped — the caller (CLI / API) never sees
    an audit failure surface as a run failure.
    """
    sink = sink or get_audit_sink(run_id)
    try:
        records = build_records(orch, run_id)
        sink.emit_many(records)
        return len(records)
    except Exception:
        return 0
    finally:
        try:
            sink.close()
        except Exception:
            pass
