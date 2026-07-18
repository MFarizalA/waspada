"""RiskPolicy — the human-configurable decision matrix (WA-032).

Every risk-policy value (band→action mapping, alert thresholds, the NPL bucket
set) used to be a hard-coded Python constant in :mod:`waspada.insight.ranking`.
This package lifts them into a **committed JSON file an analyst/admin can edit**
without touching code:

    RiskPolicy        — a frozen dataclass holding the matrix + thresholds.
    RiskPolicy.default() — the current constants, VERBATIM (behaviour unchanged
                       when no file is loaded — the regression anchor).
    load_policy(path) — read + validate JSON (``None`` → the committed
                       ``default_policy.json``, the file a human edits).

Validation is fail-loud, matching the lane check: an out-of-vocabulary action, a
band outside :data:`~waspada.schema.RISK_LEVELS`, or a threshold outside [0, 1]
raises ``ValueError`` naming the offending value — a typo never silently ships a
policy the dashboard can't render.

The band_edges / lgd slots are intentionally reserved for a later pass (WA-051
made absolute edges real, WA-024 made LGD real); they are documented here but not
yet wired, to keep this change to the owner-frozen scope.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Dict, FrozenSet, Optional

from ..schema import RISK_LEVELS

__all__ = ["RiskPolicy", "load_policy", "DEFAULT_POLICY_PATH", "VALID_ACTIONS"]

# The frozen action vocabulary (ScoredAccounts.recommended_action). The dashboard
# badges hard-depend on these literals, so load_policy keeps matrix output inside
# this set.
VALID_ACTIONS: FrozenSet[str] = frozenset({"call", "watch", "auto-cure"})

# The committed policy file — the one a human edits. Lives beside this module so
# it ships with the package.
DEFAULT_POLICY_PATH: Path = Path(__file__).resolve().parent / "default_policy.json"


@dataclasses.dataclass(frozen=True)
class RiskPolicy:
    """The decision matrix + alert thresholds, as data rather than code.

    * ``band_to_action`` — risk level → collections action. Keys are a subset of
      :data:`RISK_LEVELS`; values a subset of :data:`VALID_ACTIONS`. A band absent
      from the map falls back to the row's existing action (then ``watch``).
    * ``npl_threshold`` / ``vintage_threshold`` — the cohort-deterioration alert
      cutoffs, each in [0, 1].
    * ``npl_buckets`` — the ``delinquency_status`` values that count as
      non-performing for the NPL ratio.
    """

    band_to_action: Dict[str, str]
    npl_threshold: float
    vintage_threshold: float
    npl_buckets: FrozenSet[str]

    @classmethod
    def default(cls) -> "RiskPolicy":
        """The current hard-coded constants, verbatim.

        Sourced directly from :mod:`waspada.insight.ranking` so the default can
        never drift from the code it replaces (imported lazily to avoid any
        import-order coupling). This is the byte-for-byte regression anchor: a run
        with this policy equals a run with no policy at all.
        """
        from ..insight.ranking import (
            ACTION_BY_BAND,
            DEFAULT_NPL_THRESHOLD,
            DEFAULT_VINTAGE_THRESHOLD,
            _NPL_BUCKETS,
        )

        return cls(
            band_to_action=dict(ACTION_BY_BAND),
            npl_threshold=float(DEFAULT_NPL_THRESHOLD),
            vintage_threshold=float(DEFAULT_VINTAGE_THRESHOLD),
            npl_buckets=frozenset(_NPL_BUCKETS),
        )


def _validate(
    band_to_action: Dict[str, str],
    npl_threshold: float,
    vintage_threshold: float,
) -> None:
    """Fail loud on any out-of-vocabulary or out-of-range policy value."""
    bad_bands = [b for b in band_to_action if b not in RISK_LEVELS]
    if bad_bands:
        raise ValueError(
            f"RiskPolicy: unknown band(s) {bad_bands}; must be one of {list(RISK_LEVELS)}"
        )
    bad_actions = sorted({a for a in band_to_action.values() if a not in VALID_ACTIONS})
    if bad_actions:
        raise ValueError(
            f"RiskPolicy: invalid action(s) {bad_actions}; must be one of {sorted(VALID_ACTIONS)}"
        )
    for name, val in (("npl_threshold", npl_threshold),
                      ("vintage_threshold", vintage_threshold)):
        if not (0.0 <= float(val) <= 1.0):
            raise ValueError(f"RiskPolicy: {name}={val} out of range [0, 1]")


def load_policy(path: Optional[str] = None) -> RiskPolicy:
    """Read + validate a policy JSON file into a :class:`RiskPolicy`.

    ``path`` — an explicit file path, or ``None`` for the committed
    :data:`DEFAULT_POLICY_PATH`. An explicitly-named file that does not exist is
    an error (fail loud); a missing default file degrades to
    :meth:`RiskPolicy.default` so an unconfigured checkout still runs.

    Expected JSON shape::

        {
          "band_to_action": {"Very High": "call", ...},
          "npl_threshold": 0.20,
          "vintage_threshold": 0.15,
          "npl_buckets": ["Default", "31-120", "16-30"]
        }
    """
    if path:
        p = Path(path)
        if not p.exists():
            raise ValueError(f"RiskPolicy: policy file not found: {p}")
    else:
        p = DEFAULT_POLICY_PATH
        if not p.exists():  # unconfigured checkout — fall back to code defaults
            return RiskPolicy.default()

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"RiskPolicy: could not read {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"RiskPolicy: {p} must contain a JSON object")

    defaults = RiskPolicy.default()
    band_to_action = data.get("band_to_action", defaults.band_to_action)
    if not isinstance(band_to_action, dict) or not band_to_action:
        raise ValueError(f"RiskPolicy: band_to_action in {p} must be a non-empty object")
    band_to_action = {str(k): str(v) for k, v in band_to_action.items()}
    npl_threshold = float(data.get("npl_threshold", defaults.npl_threshold))
    vintage_threshold = float(data.get("vintage_threshold", defaults.vintage_threshold))
    raw_buckets = data.get("npl_buckets")
    npl_buckets = (frozenset(str(b) for b in raw_buckets)
                   if isinstance(raw_buckets, list) and raw_buckets
                   else defaults.npl_buckets)

    _validate(band_to_action, npl_threshold, vintage_threshold)
    return RiskPolicy(
        band_to_action=band_to_action,
        npl_threshold=npl_threshold,
        vintage_threshold=vintage_threshold,
        npl_buckets=npl_buckets,
    )
