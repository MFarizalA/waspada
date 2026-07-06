"""Risk-Model agent (WA-009) — scoring.

Wraps :mod:`waspada.model.risk` (train + predict). Reads the FeatureFrame the
analytics agent published, fits the model (vintage split, leakage-safe), scores
every account, and publishes the :class:`~waspada.schema.ScoredAccounts` table.
Flags the highest-risk band in its notes (the WA-009 acceptance: "risk-model
agent flags score bands").
"""
from __future__ import annotations

from typing import Any, Optional

import pyarrow as pa

from ..model.risk import predict as _predict, train as _train
from ..schema import ScoredAccounts, validate_table
from .base import Agent
from .protocol import AgentContext, AgentResult, Status

__all__ = ["RiskModelAgent"]


class RiskModelAgent(Agent):
    """Train + score the risk model on the analytics FeatureFrame."""

    name = "risk_model"
    role = "score P(default) per account and attach risk bands"

    def __init__(self, llm: Optional[Any] = None) -> None:
        super().__init__(llm=llm)

    def run(self, context: AgentContext) -> AgentResult:
        if not context.prior_results:
            self.step("train", status=Status.ERROR, notes="no predecessor")
            return AgentResult(status=Status.ERROR, agent=self.name, notes="no FeatureFrame input")
        frame_handle = context.prior_results[-1].artifact_ref
        frame: Optional[pa.Table] = context.data_handles.get(frame_handle) if frame_handle else None
        if frame is None:
            self.step("train", status=Status.ERROR, notes=f"handle {frame_handle!r} missing")
            return AgentResult(
                status=Status.ERROR, agent=self.name,
                notes=f"FeatureFrame handle {frame_handle!r} not found",
            )

        self.step("train", notes=f"rows={frame.num_rows} (vintage split)")
        try:
            model = _train(frame)
        except Exception as exc:
            self.step("train", status=Status.ERROR, notes=str(exc))
            return AgentResult(status=Status.ERROR, agent=self.name, notes=f"train failed: {exc}")

        auc = model.get("metrics", {}).get("auc")
        self.step(
            "train_done",
            notes=f"split={model['split']['method']} auc={auc}" if auc else f"split={model['split']['method']}",
        )

        try:
            scored = _predict(model, frame)
            validate_table(scored, ScoredAccounts, name="RiskModelAgent(scored)")
        except Exception as exc:
            self.step("predict", status=Status.ERROR, notes=str(exc))
            return AgentResult(status=Status.ERROR, agent=self.name, notes=f"predict failed: {exc}")

        # Flag the highest-risk band (Q5) count — the "flags score bands" check.
        bands = scored.column("score_band").to_pylist()
        n_q5 = sum(1 for b in bands if b == "Q5")
        self.step("score_bands", notes=f"Q5(highest)={n_q5} of {len(bands)}")

        handle = "scored_accounts"
        context.data_handles[handle] = scored
        return AgentResult(
            status=Status.OK, agent=self.name, artifact_ref=handle,
            notes=f"scored {scored.num_rows} accounts; Q5={n_q5}",
        )
