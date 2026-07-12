"""WASPADA audit-log stream (WA-023).

Ships every ``Step`` / ``Handoff`` / ``DisputeRound`` of a pipeline run to
Alibaba Simple Log Service (the fourth Alibaba Cloud service — the "show me the
audit trail" answer for a regulated lender), with a **fail-safe local fallback**
so an SLS outage can never break a collections run.

See :mod:`waspada.audit.sls`.
"""
from __future__ import annotations

from .sls import (
    AuditSink,
    LocalAuditSink,
    SLSAuditSink,
    build_records,
    get_audit_sink,
    ship_run_audit,
    sls_configured,
)

__all__ = [
    "AuditSink",
    "LocalAuditSink",
    "SLSAuditSink",
    "build_records",
    "get_audit_sink",
    "ship_run_audit",
    "sls_configured",
]
