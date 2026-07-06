"""WASPADA — shared risk-decision package.

Package spine for every downstream ticket: config/env loading, the WSL→GPU
entrypoint helper, and the **frozen data-contract schemas** (raw → features →
scores → dashboard payload). Contract names live in :mod:`waspada.schema` and
are re-exported here so downstream tickets cite them verbatim.
"""
from __future__ import annotations

from . import config, schema, wsl  # noqa: F401
from .config import Config, load_config
from .schema import (
    Alert,
    DashboardPayload,
    FeatureFrame,
    PortfolioHealth,
    RawLoans,
    ScoredAccounts,
    Segment,
    schema_from_dataclass,
    validate_table,
)
from .wsl import run_gpu

__version__ = "0.1.0"

__all__ = [
    "config",
    "schema",
    "wsl",
    "Config",
    "load_config",
    # frozen contract types (see waspada/schema.py)
    "RawLoans",
    "FeatureFrame",
    "ScoredAccounts",
    "Segment",
    "DashboardPayload",
    "PortfolioHealth",
    "Alert",
    "schema_from_dataclass",
    "validate_table",
    "run_gpu",
    "__version__",
]
