"""Ingest agent (WA-009) — the data door.

Wraps :func:`waspada.data.oss.fetch_loans`. Runs freshness/schema checks and
returns a :class:`~waspada.agents.protocol.AgentResult` whose
``artifact_ref`` points to the RawLoans handle the analytics agent consumes.

Offline by design: the real OSS call is injected as a tool (``fetch``) so the
integration test stubs it. With no tool registered the agent falls back to
the real client, which raises a clear error when creds are absent (so a
misconfigured run fails loud, not silent).
"""
from __future__ import annotations

from typing import Any, Callable, Optional

import pyarrow as pa

from ..data.oss import fetch_loans as _real_fetch_loans
from ..schema import RawLoans, validate_table
from .base import Agent
from .protocol import AgentContext, AgentResult, Status

__all__ = ["IngestAgent"]


class IngestAgent(Agent):
    """Pull the RawLoans snapshot from Alibaba Cloud OSS, with freshness/schema checks."""

    name = "ingest"
    role = "ingest the RawLoans snapshot from Alibaba Cloud OSS"

    def __init__(self, llm: Optional[Any] = None, *, limit: Optional[int] = None) -> None:
        super().__init__(llm=llm)
        self.limit = limit

    def run(self, context: AgentContext) -> AgentResult:
        lane = context.lane
        # The fetch callable: an injected stub (tool) in tests, the real BQ
        # client in production. Falls back to the real client when unset.
        fetch: Callable[..., pa.Table] = self.tools.get("fetch", _real_fetch_loans)

        self.step("fetch_loans", notes=f"lane={lane} limit={self.limit}")
        try:
            raw = fetch(lane=lane, limit=self.limit) if self.limit else fetch(lane=lane)
        except Exception as exc:  # pragma: no cover - exercised via stubs in tests
            self.step("fetch_loans", status=Status.ERROR, notes=f"fetch failed: {exc}")
            return AgentResult(
                status=Status.ERROR, agent=self.name,
                notes=f"ingest fetch failed: {exc}",
            )

        # Schema check: the table must be RawLoans-shaped (validate raises loud).
        try:
            validate_table(raw, RawLoans, name="IngestAgent(raw)")
        except ValueError as exc:
            self.step("schema_check", status=Status.ERROR, notes=str(exc))
            return AgentResult(status=Status.ERROR, agent=self.name, notes=f"schema drift: {exc}")

        # Freshness check: non-empty snapshot (zero-row read is stale/broken).
        n_rows = raw.num_rows
        if n_rows == 0:
            self.step("freshness_check", status=Status.BLOCKED, notes="zero rows read")
            return AgentResult(
                status=Status.BLOCKED, agent=self.name,
                notes="ingest returned zero rows (stale/empty source)",
            )

        self.step("freshness_check", notes=f"{n_rows} rows; schema OK")
        # Publish the table on the shared store and point the artifact at it.
        handle = "raw_loans"
        context.data_handles[handle] = raw
        return AgentResult(
            status=Status.OK, agent=self.name, artifact_ref=handle,
            notes=f"ingested {n_rows} RawLoans rows (lane={lane})",
        )
