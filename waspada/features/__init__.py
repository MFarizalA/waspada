"""Feature engineering for WASPADA (WA-004).

Currently ships the Collections-lane feature + label builder (CPU/pyarrow
reference path; the GPU/cuDF path is ``gpu/run_features.py`` run via
:func:`waspada.wsl.run_gpu`). Import-safe without cuDF — nothing here imports
the GPU stack; it is pure pyarrow.
"""
from __future__ import annotations

from .collections import (
    DEFAULT_STATUSES,
    assert_no_nulls,
    build_features,
    build_label,
    delinquency_bucket,
    is_default,
)

__all__ = [
    "DEFAULT_STATUSES",
    "assert_no_nulls",
    "build_features",
    "build_label",
    "delinquency_bucket",
    "is_default",
]
