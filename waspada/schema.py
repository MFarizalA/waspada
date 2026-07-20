"""The frozen WASPADA data contract.

Four contract types, locked here so every downstream ticket cites the same names
and shapes verbatim:

  * :class:`RawLoans`         — raw row, one per loan (cross-sectional snapshot).
  * :class:`FeatureFrame`     — per-loan features + ``label_default``.
  * :class:`ScoredAccounts`   — per-loan ``p_default`` + band/segment/action.
  * :class:`DashboardPayload` — JSON handed to the frontend (insight layer).

Wire format
-----------
The three columnar contracts are :func:`dataclasses.dataclass` instances (the
ticket's "dataclasses / TypedDict"). The dashboard payload and its nested
records are :class:`~typing.TypedDict` (JSON for the frontend). Arrow tables
that flow between pipeline stages (WA-002 → 004 → 005 → 006) are *shaped* by
these dataclasses — :func:`schema_from_dataclass` derives a matching
``pyarrow.Schema`` so an Arrow table can be validated against the contract.

Provenance / label note (do not re-litigate without raising it)
---------------------------------------------------------------
The LendingClub source is **cross-sectional** — one row per loan with current
status + aggregate payment totals, NOT a monthly payment panel. Therefore:
  * ``label_default`` is **eventual charge-off / default** (from final status),
    NOT a 30-day roll-to-NPL (which needs a panel).
  * Portfolio roll rates are aggregate-only and deferred to the Freddie Mac
    panel stretch — they are not in the MVP payload.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Dict, List, Optional, Tuple, TypedDict, Type, get_args, get_origin, get_type_hints

import pyarrow as pa


# ---------------------------------------------------------------------------
# 1. RawLoans — raw row, one per loan (mapped from LendingClub in WA-003).
#    LC → contract map: id→loan_id, loan_amnt→amount, term→term (coerced to
#    months), int_rate→rate, grade/sub_grade→grade, annual_inc→annual_income,
#    dti→dti, issue_d→issue_date, purpose→purpose, addr_state→region,
#    out_prncp→outstanding_principal, total_pymnt→total_paid,
#    loan_status→current_status.
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class RawLoans:
    """One raw loan row (cross-sectional snapshot of status + payment totals)."""

    loan_id: str
    amount: float
    term: int                      # tenure in months (e.g. 36 / 60)
    rate: float                    # annual interest rate, percent
    grade: str
    annual_income: float
    dti: float                     # debt-to-income, percent
    issue_date: dt.date
    purpose: str
    region: str
    outstanding_principal: float
    total_paid: float
    current_status: str            # loan_status, final/snapshot


# ---------------------------------------------------------------------------
# 2. FeatureFrame — per-loan features + label (built in WA-004 via cuDF).
#    Snapshot features carried from raw; behavioral features derived.
#    `as_of_date` is configurable so outcome-derived fields never leak into
#    training features (WA-004 / WA-005 vintage split).
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class FeatureFrame:
    """Per-loan feature vector + the eventual-default label."""

    loan_id: str
    # --- snapshot features (carried from RawLoans) ---
    amount: float
    term: int
    rate: float
    grade: str
    annual_income: float
    dti: float
    purpose: str
    region: str
    # --- behavioral features (derived) ---
    loan_age: int                  # months from issue_date to as_of_date
    payment_ratio: float           # total_paid / amount
    outstanding_ratio: float       # outstanding_principal / amount
    delinquency_status: str        # current delinquency status / bucket
    # --- label + as-of ---
    label_default: bool            # eventual charge-off/default (NOT a 30-day roll)
    as_of_date: dt.date


# ---------------------------------------------------------------------------
# 3. ScoredAccounts — per-loan model output (built in WA-005, ranked WA-006).
#    `score_band` is a risk level (quintile-derived, WA-005). `segment` and
#    `recommended_action` are populated by WA-006; WA-005 may emit them empty.
# ---------------------------------------------------------------------------
# Risk-level vocabulary for ``score_band`` — ordered lowest→highest risk.
# The single source of truth: the model assigns these, ranking maps them to
# actions, the auditor projects them onto its ordinal, the dashboard renders
# them. (Previously the quintile labels Q1..Q5.)
RISK_LEVELS: Tuple[str, ...] = ("Very Low", "Low", "Medium", "High", "Very High")

# The Skeptic's independent read uses a coarser 3-point vocabulary. It is a
# projection of RISK_LEVELS onto its odd positions (1/3/5) — the same projection
# the dispute-admissibility gate already assumes
# (``risk_auditor._VIEW_ORDINAL``: low=1, medium=3, high=5). Inverting it here
# gives the two layers one shared, non-duplicated source of truth.
AUDITOR_VIEWS: Tuple[str, ...] = ("Low", "Medium", "High")
_VIEW_TO_LEVEL: Dict[str, str] = {
    "low": RISK_LEVELS[0], "medium": RISK_LEVELS[2], "high": RISK_LEVELS[4],
}


def risk_level_ordinal(level: str) -> Optional[int]:
    """1-based rank of ``level`` on the risk scale ("Very Low"=1 … "Very High"=5).

    ``None`` when ``level`` is not a RISK_LEVELS value. Callers compare two
    ordinals to learn the *direction* of a change — which is what decides whether
    a society override is an escalation (auto-apply) or a de-escalation (human
    sign-off). See ``Orchestrator._apply_adjudications``.
    """
    try:
        return RISK_LEVELS.index(str(level).strip().title()) + 1
    except ValueError:
        return None


def view_to_risk_level(view: str) -> Optional[str]:
    """Project a Skeptic view ("Low"|"Medium"|"High") onto RISK_LEVELS.

    Used as the fallback when the Arbiter rules against the model but does not
    name a band itself, and on the Actuary's concede path (conceding means the
    Skeptic's view stands). ``None`` on an unrecognized view.
    """
    return _VIEW_TO_LEVEL.get(str(view).strip().lower())


@dataclasses.dataclass(frozen=True)
class Segment:
    """A portfolio slice by product and region."""

    product: str
    region: str


@dataclasses.dataclass(frozen=True)
class ScoredAccounts:
    """Per-loan model output: P(default) + band/segment/action.

    ``p_default`` and ``score_band`` are the **model's** output and are never
    rewritten downstream — they stay the auditable statistical fact.

    WA-048 adds two **additive optional Arrow columns** (not dataclass fields, so
    the contract is unchanged — :func:`validate_table` allows supersets):

    * ``final_band`` — the risk level after the Agent Society's debate. Equals
      ``score_band`` unless a dispute went against the model *and* the revision
      was applied. This is what ``ranking.rank()`` derives
      ``recommended_action`` from, which is what makes the debate load-bearing
      rather than decorative.
    * ``override_reason`` — why the society moved the band (the arbiter's or the
      conceding model's rationale). Empty when the model's band stands.

    Same additive discipline as ``expected_loss``/``outstanding_principal``
    (WA-024) and ``agent_dialogue`` below: producers may omit them, consumers
    probe with ``_safe_get`` and degrade to the model's band.
    """

    loan_id: str
    p_default: float               # P(eventual default) ∈ [0, 1] — never rewritten
    score_band: str                # the MODEL's risk level (RISK_LEVELS) — never rewritten
    segment: Segment
    recommended_action: str        # "call" | "watch" | "auto-cure"


# ---------------------------------------------------------------------------
# Origination lane contract (WA-034) — ADDITIVE, beside the collections types.
# An applicant has no payment history, so the collections shapes don't fit.
#
# Label honesty note (do not fake): the LendingClub snapshot only contains
# FUNDED loans — there is no rejected-applications outcome data. The origination
# label is therefore "funded-then-defaulted" (reject-inference is impossible
# from this data). Never claim approve/reject ground truth.
# ---------------------------------------------------------------------------
# The origination action vocabulary (ScoredApplications.recommended_action).
ORIGINATION_ACTIONS: Tuple[str, ...] = ("approve", "refer", "reject")


@dataclasses.dataclass(frozen=True)
class RawApplications:
    """One raw loan APPLICATION row (application-time snapshot).

    Post-disbursement fields (outstanding_principal / total_paid /
    current_status) deliberately absent — they don't exist at application time.
    ``funded`` + ``funded_default`` are OUTCOME columns carried for training
    only (see the label honesty note above); they must never enter the feature
    matrix (LEAKAGE_EXCLUDED, WA-035/036).
    """

    application_id: str
    amount: float
    term: int                      # requested tenure in months
    requested_rate: float          # requested/offered rate — NOT a booked rate
    grade: str
    annual_income: float
    dti: float
    application_date: dt.date
    purpose: str
    region: str
    employment_length: int         # years employed (0 = <1yr / unknown)
    funded: bool                   # outcome: was the application funded?
    funded_default: bool           # outcome: funded AND later defaulted


@dataclasses.dataclass(frozen=True)
class ApplicationFeatureFrame:
    """Application-time feature vector + the funded-then-defaulted label."""

    application_id: str
    # --- application-time features (no payment history exists) ---
    amount: float
    term: int
    requested_rate: float
    grade: str
    annual_income: float
    dti: float
    loan_to_income: float          # amount / annual_income
    employment_length: int
    purpose: str
    region: str
    # --- cohort axis (application-time metadata, NEVER a model feature) ---
    application_date: dt.date      # the out-of-time split cohorts on this
    # --- label + as-of ---
    label_default: bool            # funded-then-defaulted (see honesty note)
    as_of_date: dt.date


@dataclasses.dataclass(frozen=True)
class ScoredApplications:
    """Per-application model output: P(default) + band/segment/action.

    Mirrors :class:`ScoredAccounts` — ``p_default`` / ``score_band`` are the
    model's output and never rewritten; RISK_LEVELS is reused verbatim. The
    action vocabulary is origination's: ``approve`` | ``refer`` | ``reject``.
    ``priced_rate`` (risk-based pricing) is an additive optional Arrow column,
    not a contract field.
    """

    application_id: str
    p_default: float               # P(funded-then-default) ∈ [0, 1]
    score_band: str                # RISK_LEVELS value — never rewritten
    segment: Segment
    recommended_action: str        # "approve" | "refer" | "reject"


class OriginationHealth(TypedDict):
    """Origination-book aggregates (origination lane).

    * ``approval_rate`` — fraction of applications decided ``approve``.
    * ``projected_default_rate`` — mean ``p_default`` of the approved book.
    * ``band_mix`` — proportion of applications per RISK_LEVELS band.
    * ``approved_volume`` — Σ ``amount`` over approved applications.
    """

    approval_rate: float
    projected_default_rate: float
    band_mix: Dict[str, float]
    approved_volume: float


# ---------------------------------------------------------------------------
# 4. DashboardPayload — JSON for the frontend (assembled in WA-006).
#    TypedDicts (not dataclasses): this is the JSON-serializable hand-off to
#    the dashboard (WA-011). True roll rates are deferred to the Freddie Mac
#    panel stretch and are NOT in the MVP payload.
# ---------------------------------------------------------------------------
class PortfolioHealth(TypedDict):
    """Portfolio-level cross-sectional aggregates (collections lane).

    * ``npl_ratio`` — fraction of accounts in delinquent/default status.
    * ``vintage_default_rate`` — default rate keyed by ``issue_date`` cohort.
    * ``status_mix`` — proportion of accounts per ``current_status`` value.

    WA-024 adds an ADDITIVE optional key ``total_expected_loss`` (the portfolio
    Σ of PD × LGD × EAD, in IDR) — present only when ``outstanding_principal``
    is available; older payloads without it stay valid. Named ``total_``* to
    distinguish it from each work-list row's per-account ``expected_loss`` and to
    match the frontend (``dashboard/src/types.ts``).
    """

    npl_ratio: float
    vintage_default_rate: Dict[str, float]
    status_mix: Dict[str, float]


class Alert(TypedDict):
    """A cohort/portfolio deterioration alert from WA-006."""

    metric: str                    # e.g. "npl_ratio", "vintage_default_rate"
    value: float
    threshold: float
    message: str
    segment: Optional[Dict[str, str]]   # None = portfolio-wide


class DashboardPayload(TypedDict):
    """The frozen frontend hand-off: ranked work-list + health + alerts.

    ``agent_dialogue`` is an ADDITIVE optional key (the Qwen-pivot debate
    protocol, HACKATHON.md): absent on older payloads, so validation does not
    require it. When present it is the serialized
    :class:`~waspada.agents.protocol.Dispute` list (one entry per contested
    account). Older payloads without it stay valid.
    """

    work_list: List[Dict[str, object]]     # ScoredAccounts rows as JSON records
    portfolio_health: PortfolioHealth
    alerts: List[Alert]
    agent_dialogue: Optional[List[Dict[str, object]]]


# ---------------------------------------------------------------------------
# Arrow shape helper — derive a pyarrow.Schema from a contract dataclass so
# an Arrow table flowing between pipeline stages can be validated against the
# frozen contract (used by WA-002/003/004/005/006 acceptance checks).
# ---------------------------------------------------------------------------
_PY_TO_ARROW: Dict[type, pa.DataType] = {
    str: pa.string(),
    int: pa.int64(),
    float: pa.float64(),
    bool: pa.bool_(),
    dt.date: pa.date32(),
    dt.datetime: pa.timestamp("us"),
}


def schema_from_dataclass(cls: type) -> pa.Schema:
    """Build a ``pyarrow.Schema`` matching a contract dataclass field-for-field.

    Nested dataclass fields become ``pa.struct`` fields. Raises ``TypeError``
    for unmappable field types rather than guessing — a frozen seam should not
    silently coerce.
    """
    if not dataclasses.is_dataclass(cls):
        raise TypeError(f"{cls!r} is not a dataclass")
    hints = get_type_hints(cls)
    fields: List[pa.Field] = []
    for f in dataclasses.fields(cls):
        pa_type = _to_arrow_type(hints[f.name])
        fields.append(pa.field(f.name, pa_type, nullable=False))
    return pa.schema(fields, metadata={b"contract": cls.__name__.encode(), b"frozen": b"WA-001"})


def _to_arrow_type(py_type: type) -> pa.DataType:
    origin = get_origin(py_type)
    if origin is Optional:  # Optional[X] -> unwrap
        inner = get_args(py_type)[0]
        return _to_arrow_type(inner)
    if dataclasses.is_dataclass(py_type):
        nested = pa.struct(
            [pa.field(f.name, _to_arrow_type(get_type_hints(py_type)[f.name])) for f in dataclasses.fields(py_type)]
        )
        return nested
    if py_type in _PY_TO_ARROW:
        return _PY_TO_ARROW[py_type]
    raise TypeError(f"No Arrow mapping for {py_type!r} (contract field); add it explicitly.")


def validate_table(table: pa.Table, cls: Type, *, name: str = "table") -> None:
    """Assert ``table`` has every field of dataclass ``cls`` with a compatible type.

    Extra columns are allowed (superset), matching the ingest acceptance
    ("columns are a superset of RawLoans"). Missing fields or type mismatches
    raise ``ValueError`` naming the diff.
    """
    expected = schema_from_dataclass(cls)
    exp_names = [f.name for f in expected]
    act_names = [f.name for f in table.schema]
    missing = [n for n in exp_names if n not in act_names]
    if missing:
        raise ValueError(f"{name} is missing required field(s): {missing}")
    mismatched = []
    for f in expected:
        actual = table.schema.field(f.name).type
        if not actual.equals(f.type):
            mismatched.append((f.name, f.type, actual))
    if mismatched:
        detail = "; ".join(f"{n}: expected {e}, got {a}" for n, e, a in mismatched)
        raise ValueError(f"{name} has type mismatch(es) — {detail}")
