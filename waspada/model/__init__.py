"""Risk-model package (WA-005). CPU path lives in :mod:`waspada.model.risk`.

The GPU/cuML estimator is on hold (owner); when it lands it keeps the same
``train``/``predict`` signatures so the pipeline agents (WA-009) need no
changes — a drop-in swap.
"""
from __future__ import annotations

from .risk import (
    CATEGORICAL_FEATURES,
    FEATURE_COLUMNS,
    LEAKAGE_EXCLUDED,
    NUMERIC_FEATURES,
    load_model,
    predict,
    save_model,
    train,
)

__all__ = [
    "FEATURE_COLUMNS",
    "NUMERIC_FEATURES",
    "CATEGORICAL_FEATURES",
    "LEAKAGE_EXCLUDED",
    "train",
    "predict",
    "save_model",
    "load_model",
]
