"""Analytics agent (WA-009) — feature engineering.

Wraps :func:`waspada.features.collections.build_features`. Reads the RawLoans
artifact the ingest agent published, builds the cross-sectional
:class:`~waspada.schema.FeatureFrame`, and publishes it for the risk-model
agent. Surfaces a null-rate check (the WA-009 acceptance: "analytics agent
surfaces feature-null rates").
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

import pyarrow as pa
import pyarrow.compute as pc

from ..features.collections import assert_no_nulls, build_features
from ..schema import FeatureFrame
from .base import Agent
from .protocol import AgentContext, AgentResult, Status

__all__ = ["AnalyticsAgent"]


class AnalyticsAgent(Agent):
    """Build the FeatureFrame from the ingested RawLoans snapshot."""

    name = "analytics"
    role = "build the cross-sectional FeatureFrame"

    def __init__(self, llm: Optional[Any] = None, *, as_of: Optional[dt.date] = None) -> None:
        super().__init__(llm=llm)
        self.as_of = as_of or dt.date(2024, 12, 1)

    def run(self, context: AgentContext) -> AgentResult:
        # Consume the ingest agent's artifact handle.
        if not context.prior_results:
            self.step("build_features", status=Status.ERROR, notes="no predecessor")
            return AgentResult(status=Status.ERROR, agent=self.name, notes="no RawLoans input")
        raw_handle = context.prior_results[-1].artifact_ref
        raw: Optional[pa.Table] = context.data_handles.get(raw_handle) if raw_handle else None
        if raw is None:
            self.step("build_features", status=Status.ERROR, notes=f"handle {raw_handle!r} missing")
            return AgentResult(
                status=Status.ERROR, agent=self.name,
                notes=f"RawLoans handle {raw_handle!r} not found",
            )

        self.step("build_features", notes=f"as_of={self.as_of.isoformat()} rows={raw.num_rows}")
        try:
            frame = build_features(raw, self.as_of)
            assert_no_nulls(frame, FeatureFrame)  # raises on any null contract field
        except Exception as exc:
            self.step("build_features", status=Status.ERROR, notes=str(exc))
            return AgentResult(status=Status.ERROR, agent=self.name, notes=f"features failed: {exc}")

        # Surface a null-rate summary (acceptance: "surfaces feature-null rates").
        # Contract fields are non-nullable, so the per-field null rate is 0; we
        # report it explicitly as the QA-friendly guarantee.
        null_total = sum(
            int(pc.sum(pc.is_null(frame.column(f.name))).as_py())
            for f in __import__("dataclasses").fields(FeatureFrame)
        )
        self.step(
            "null_rate_check", notes=f"feature-null total = {null_total} (all contract fields non-null)",
        )

        handle = "feature_frame"
        context.data_handles[handle] = frame
        return AgentResult(
            status=Status.OK, agent=self.name, artifact_ref=handle,
            notes=f"built FeatureFrame ({frame.num_rows} rows, nulls={null_total})",
        )
