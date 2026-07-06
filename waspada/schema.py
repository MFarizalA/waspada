"""WASPADA frozen data contract.

Four contract types flow through the pipeline in one direction:

    RawLoans  ->  FeatureFrame  ->  ScoredAccounts  ->  DashboardPayload
    (ingest)     (features)        (scores)            (analyst view)

These are **frozen**: downstream tickets (WA-002..WA-013) cite these names and
fields verbatim. Do not rename fields or change their types; if a field seems
missing, raise it with the boss before widening the contract. Add new *optional*
fields only with explicit sign-off.

Domain note (from WA-001): the source data is **cross-sectional** -- one row per
loan, a snapshot of current loan_status + aggregate payment totals -- NOT a
monthly payment panel. There is therefore no per-account month-over-month roll
rate and no computable 30-day roll-to-NPL label. ``label_default`` is *eventual*
charge-off / default (derived from the final ``current_status``); roll rates are
a portfolio-level aggregate only (true roll rates need the Freddie Mac panel --
stretch, not in the MVP payload).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "RawLoans",
    "FeatureFrame",
    "Segment",
    "ScoredAccounts",
    "PortfolioHealth",
    "Alert",
    "DashboardPayload",
]

# All monetary/ratio fields are floats; IDs are strings (portable across
# parquet/JSON/BigQuery, immune to int64/float64 overflow in round trips).
# Dates are ISO-8601 strings ("YYYY-MM-DD") for clean JSON + parquet interop.


@dataclass(frozen=True)
class RawLoans:
    """Raw loan row -- one record per loan, as mapped from LendingClub (WA-003).

    Cross-sectional snapshot: a single observation of each loan's current
    status plus its lifetime aggregate payment totals. Not a payment panel.

    Fields:
        loan_id: Stable loan identifier (string).
        amount: Loan principal funded (currency units).
        term: Loan term in months (typically 36 or 60).
        rate: Annual interest rate, in percent (e.g. 13.56 == 13.56%).
        grade: Credit grade (e.g. "A".."G").
        annual_income: Borrower stated annual income.
        dti: Debt-to-income ratio (percent).
        issue_date: Date the loan was issued (ISO-8601 "YYYY-MM-DD").
        purpose: Loan purpose category (e.g. "credit_card", "debt_consolidation").
        region: Borrower geographic region.
        outstanding_principal: Principal still outstanding (snapshot).
        total_paid: Total paid to date (principal + interest, snapshot).
        current_status: Final/current loan status string (e.g. "Fully Paid",
            "Charged Off", "Current", "Late (31-120 days)"). Source of
            ``label_default`` downstream.
    """

    loan_id: str
    amount: float
    term: int
    rate: float
    grade: str
    annual_income: float
    dti: float
    issue_date: str
    purpose: str
    region: str
    outstanding_principal: float
    total_paid: float
    current_status: str


@dataclass(frozen=True)
class FeatureFrame:
    """Per-loan feature record (output of the cuDF analytics step, WA-004).

    Snapshot + behavioral features derivable from the cross-sectional row, plus
    the eventual-default label. No panel-dependent features (no roll rates,
    no DPD trend) -- those require the Freddie Mac panel (stretch).

    Fields:
        loan_id: Loan identifier (joins 1:1 with ``RawLoans.loan_id``).
        loan_age: Months since ``issue_date`` at the snapshot date.
        payment_ratio: ``total_paid / amount`` (lifetime recovery progress).
        outstanding_ratio: ``outstanding_principal / amount``.
        delinquency_status: Current delinquency bucket/status at snapshot,
            mapped from ``current_status`` (e.g. "Current", "Late (31-120)").
        dti: Debt-to-income ratio (percent).
        grade: Credit grade.
        term: Loan term in months.
        label_default: **Eventual** charge-off / default (True iff final
            ``current_status`` is a terminal-default state). NOT a 30-day roll.
        as_of_date: Snapshot date this feature row is computed for (ISO-8601).
    """

    loan_id: str
    loan_age: int
    payment_ratio: float
    outstanding_ratio: float
    delinquency_status: str
    dti: float
    grade: str
    term: int
    label_default: bool
    as_of_date: str


@dataclass(frozen=True)
class Segment:
    """Portfolio segment key -- the two axes the dashboard groups scores by."""

    product: str
    region: str


@dataclass(frozen=True)
class ScoredAccounts:
    """Per-loan risk score (output of the cuML risk-model step, WA-005/006).

    Fields:
        loan_id: Loan identifier.
        p_default: Predicted probability of *eventual* default (0.0..1.0).
        score_band: Discrete risk band derived from ``p_default``
            (e.g. "very_low".."very_high").
        segment: Portfolio segment (product + region) for grouping.
        recommended_action: Analyst-facing action for this account
            (collections lane: e.g. "prioritize", "monitor", "release").
    """

    loan_id: str
    p_default: float
    score_band: str
    segment: Segment
    recommended_action: str


@dataclass(frozen=True)
class PortfolioHealth:
    """Portfolio-level aggregate health metrics for the dashboard.

    Fields:
        npl_ratio: Non-performing loan ratio at snapshot (proportion, 0..1).
        vintage_default_rate: Default rate across the relevant vintage(s)
            (proportion, 0..1).
        status_mix: Mapping of status-label -> proportion (sums to ~1.0).
            Keys mirror the ``current_status`` / ``delinquency_status`` vocab.
    """

    npl_ratio: float
    vintage_default_rate: float
    status_mix: dict[str, float]


@dataclass(frozen=True)
class Alert:
    """A single portfolio alert surfaced to the analyst.

    Provisional shape -- the element type of ``DashboardPayload.alerts``. The
    WA-001 spec leaves alert fields open (``alerts: []`` in the MVP payload);
    this minimal {level, message, context} defers the full schema to the
    insight/alerts ticket. Add fields there, not here, without sign-off.

    Fields:
        level: Severity -- one of "info" | "warn" | "critical".
        message: Human-readable alert text.
        context: Optional structured detail (segment key, threshold, etc.).
    """

    level: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DashboardPayload:
    """The analyst-facing JSON payload (output of the insight step, WA-007).

    This is the frozen wire shape the dashboard (Kirana, WA-008+) consumes.
    True roll rates are deferred to the Freddie Mac panel stretch and are NOT
    in the MVP payload.

    Fields:
        work_list: Ranked collections work-list of scored accounts
            (highest priority first).
        portfolio_health: Aggregate portfolio health metrics.
        alerts: Portfolio early-warning alerts (may be empty in MVP).
    """

    work_list: list[ScoredAccounts]
    portfolio_health: PortfolioHealth
    alerts: list[Alert] = field(default_factory=list)
