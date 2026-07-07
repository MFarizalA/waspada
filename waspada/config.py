"""Configuration & environment loading for WASPADA.

Reads from the environment, optionally seeded from a ``.env`` file via
python-dotenv. The contract (:func:`load_config`) returns a snapshot
:class:`Config` with empty-string defaults (never ``None``) so callers can do
simple truthiness checks, and raises ``ValueError`` for an invalid lane so a
typo fails loudly instead of silently picking a lane.

Vars (see ``.env.example``):
  * ``OSS_BUCKET``, ``OSS_ENDPOINT``, ``OSS_KEY`` — Alibaba Cloud OSS location
    of the committed loan-portfolio Parquet object.
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

    OSS fields default to empty string (not None) so callers can use simple
    truthiness checks; ``lane`` defaults to ``collections``.
    """

    lane: str
    oss_bucket: str
    oss_endpoint: str
    oss_key: str

    def require_oss(self) -> "Config":
        """Return self if OSS is fully configured, else raise RuntimeError."""
        if not (self.oss_bucket and self.oss_endpoint and self.oss_key):
            raise RuntimeError(
                "OSS not configured: set OSS_BUCKET, OSS_ENDPOINT, OSS_KEY "
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
        oss_bucket=os.environ.get("OSS_BUCKET", ""),
        oss_endpoint=os.environ.get("OSS_ENDPOINT", ""),
        oss_key=os.environ.get("OSS_KEY", ""),
    )


# Module-level singleton — convenient for ``from waspada.config import config``.
# Tests that mutate env and call ``importlib.reload(config)`` see it refreshed.
config: Config = load_config()
