"""Environment + lane configuration for WASPADA.

Loads vars from ``.env`` (gitignored; see ``.env.example``) on import and
exposes the resolved settings + the active lane switch. The two lanes
(collections | origination) share one engine and differ only in which
features/label/recommendation phrasing applies -- so the lane is a runtime
switch, not a separate codebase.

Env vars (all optional at import time -- defaults are safe for local dev and
tests; production values come from ``.env`` or real env):
    BQ_PROJECT   -- GCP project holding the BigQuery dataset.
    BQ_DATASET   -- BigQuery dataset name.
    BQ_TABLE     -- BigQuery source table (e.g. the LendingClub table).
    WASPADA_LANE -- "collections" (default) | "origination".
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

try:
    # python-dotenv is a declared dependency (requirements.txt). Importing must
    # not hard-fail when the file is absent or the package isn't installed yet
    # (e.g. in a minimal test env): load_dotenv is best-effort here.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dependency-missing path
    pass

Lane = Literal["collections", "origination"]

_DEFAULTS: dict[str, str] = {
    "BQ_PROJECT": "",
    "BQ_DATASET": "",
    "BQ_TABLE": "",
    "WASPADA_LANE": "collections",
}

_VALID_LANES = {"collections", "origination"}


@dataclass(frozen=True)
class Config:
    """Resolved configuration snapshot.

    Fields are empty-string-safe so tests can ``import waspada`` with no ``.env``
    and no GCP credentials present. Downstream code that actually talks to
    BigQuery (WA-002) validates non-empty values at call time, not at import.
    """

    bq_project: str
    bq_dataset: str
    bq_table: str
    lane: Lane


def _get(key: str) -> str:
    val = os.environ.get(key)
    return val if val is not None and val != "" else _DEFAULTS[key]


def _resolve_lane(raw: str) -> Lane:
    lane = raw.strip().lower()
    if lane not in _VALID_LANES:
        raise ValueError(
            f"WASPADA_LANE must be one of {sorted(_VALID_LANES)}, got: {raw!r}"
        )
    return lane  # type: ignore[return-value]


def load_config() -> Config:
    """Build a :class:`Config` from the current environment.

    Reads env vars (plus any ``.env`` loaded at import). Raises ``ValueError``
    only if ``WASPADA_LANE`` is set to an unknown value; empty/missing BigQuery
    values are allowed (validated lazily by WA-002).
    """
    return Config(
        bq_project=_get("BQ_PROJECT"),
        bq_dataset=_get("BQ_DATASET"),
        bq_table=_get("BQ_TABLE"),
        lane=_resolve_lane(_get("WASPADA_LANE")),
    )


# Module-level resolved config: importable as ``from waspada.config import config``.
# Tests that need to vary env should call ``load_config()`` after setting env.
config: Config = load_config()
