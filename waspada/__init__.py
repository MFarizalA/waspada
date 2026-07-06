"""WASPADA -- Warning & Approval System for Portfolio And Default Analytics.

The shared Python spine every downstream ticket builds against. This package
holds:

- :mod:`waspada.schema` -- the **frozen** data-contract types
  (``RawLoans -> FeatureFrame -> ScoredAccounts -> DashboardPayload``).
- :mod:`waspada.config` -- env/lane configuration.
- :mod:`waspada.wsl` -- the WSL->RAPIDS GPU entrypoint helper.

See ``backlog/WA-001.md`` for the contract spec and ``HACKATHON.md`` for the
full project brief.
"""

from __future__ import annotations

__version__ = "0.1.0"

# Re-export the contract surface so ``from waspada import RawLoans`` works.
# (Schema/config import is cheap and side-effect free; wsl import is deferred
# because importing it is harmless but run_gpu is the only GPU entrypoint.)
from .schema import (  # noqa: E402,F401
    Alert,
    DashboardPayload,
    FeatureFrame,
    PortfolioHealth,
    RawLoans,
    ScoredAccounts,
    Segment,
)
# NB: only Config/load_config are re-exported here -- NOT the module-level
# ``config`` instance, because binding ``config`` into this namespace would
# shadow the ``waspada.config`` submodule (``from waspada import config`` would
# then return the instance, not the module). Downstream code that wants the
# pre-resolved instance uses ``from waspada.config import config``.
from .config import Config, load_config  # noqa: E402,F401

__all__ = [
    "__version__",
    "RawLoans",
    "FeatureFrame",
    "Segment",
    "ScoredAccounts",
    "PortfolioHealth",
    "Alert",
    "DashboardPayload",
    "Config",
    "load_config",
]
