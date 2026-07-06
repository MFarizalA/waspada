"""Configuration & environment loading for WASPADA.

Reads from the environment, optionally seeded from a ``.env`` file via
python-dotenv. The contract (:func:`load_config`) returns a snapshot
:class:`Config` with empty-string defaults (never ``None``) so callers can do
simple truthiness checks, and raises ``ValueError`` for an invalid lane so a
typo fails loudly instead of silently picking a lane.

Vars (see ``.env.example``):
  * ``BQ_PROJECT``, ``BQ_DATASET``, ``BQ_TABLE`` — BigQuery location of the loans table.
  * ``WASPADA_LANE`` — decision lane: ``collections`` (default) or ``origination``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Tuple

from dotenv import load_dotenv

# The two decision lanes sharing one engine (see HACKATHON.md "Two lanes").
COLLECTIONS = "collections"
ORIGINATION = "origination"
LANES: Tuple[str, ...] = (COLLECTIONS, ORIGINATION)

# Seed env from .env once on import (no-op when .env is absent).
load_dotenv()


@dataclass(frozen=True)
class Config:
    """Snapshot of the active WASPADA configuration.

    BQ fields default to empty string (not None) so callers can use simple
    truthiness checks; ``lane`` defaults to ``collections``.
    """

    lane: str
    bq_project: str
    bq_dataset: str
    bq_table: str

    def require_bq(self) -> "Config":
        """Return self if BigQuery is fully configured, else raise RuntimeError."""
        if not (self.bq_project and self.bq_dataset and self.bq_table):
            raise RuntimeError(
                "BigQuery not configured: set BQ_PROJECT, BQ_DATASET, BQ_TABLE "
                "(see .env.example)."
            )
        return self


def _resolve_lane() -> str:
    """Resolve WASPADA_LANE: strip, lowercase, validate; default collections."""
    value = (os.environ.get("WASPADA_LANE") or COLLECTIONS).strip().lower()
    if value not in LANES:
        raise ValueError(
            f"WASPADA_LANE={value!r} is invalid; must be one of {LANES}"
        )
    return value


def load_config() -> Config:
    """Build a :class:`Config` from the current environment (pure, no caching).

    Reads the live env each call, so tests using ``monkeypatch.setenv`` see the
    new value immediately. Lane defaults to ``collections`` and is validated.
    """
    return Config(
        lane=_resolve_lane(),
        bq_project=os.environ.get("BQ_PROJECT", ""),
        bq_dataset=os.environ.get("BQ_DATASET", ""),
        bq_table=os.environ.get("BQ_TABLE", ""),
    )


# Module-level singleton — convenient for ``from waspada.config import config``.
# Tests that mutate env and call ``importlib.reload(config)`` see it refreshed.
config: Config = load_config()
