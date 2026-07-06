"""Collections-lane feature + label engineering (WA-004).

The analytics core of the Collections lane. The source data is **cross-sectional**
(one row per loan — a snapshot of status + aggregate payment totals), so this
module builds a cross-sectional feature set and an *eventual* charge-off / default
label — NOT month-over-month roll rates or a 30-day roll label (those need a
panel the snapshot does not provide; see the WA-001 lesson cited in
:mod:`waspada.schema`).

This is the host / CPU reference implementation (pure pyarrow, GPU-free) so the
unit tests run in the worker container without a GPU. The GPU/cuDF execution path
lives in ``gpu/run_features.py`` (run via :func:`waspada.wsl.run_gpu`); it reuses
the status-mapping logic exported here so the two paths never disagree on what
counts as a default.

FeatureFrame contract (cited verbatim from waspada/schema.py)::

    @dataclasses.dataclass(frozen=True)
    class FeatureFrame:
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
"""
from __future__ import annotations

import datetime as dt
from typing import FrozenSet

import pyarrow as pa
import pyarrow.compute as pc

from ..schema import FeatureFrame, RawLoans, schema_from_dataclass, validate_table

__all__ = [
    "DEFAULT_STATUSES",
    "build_label",
    "build_features",
    "delinquency_bucket",
    "is_default",
    "assert_no_nulls",
]

# ---------------------------------------------------------------------------
# Status mapping — the single source of truth for "what is a default".
# Imported by gpu/run_features.py so the CPU and GPU paths can never disagree.
# Statuses are matched case-insensitively after strip(); the canonical
# LendingClub values are "Charged Off", "Default", "Current", "Fully Paid".
# ---------------------------------------------------------------------------
DEFAULT_STATUSES: FrozenSet[str] = frozenset({"charged off", "default"})


def _norm(status: str) -> str:
    """Normalize a status string for matching (strip + lowercase)."""
    return (status or "").strip().lower()


def is_default(status: str) -> bool:
    """True iff ``status`` is a terminal default (Charged Off / Default).

    This is the *eventual default* label — only terminal default states are
    True. Everything else (Current, Fully Paid, and in-flight delinquencies like
    Grace Period / Late that have not yet reached terminal default) is False.
    """
    return _norm(status) in DEFAULT_STATUSES


def delinquency_bucket(status: str) -> str:
    """Map a raw ``current_status`` to a delinquency bucket (a feature, not the label).

    Buckets are coarse on purpose (a feature, not a full status taxonomy):
      * ``"0"``       — performing (Current / Fully Paid)
      * ``"1-30"``    — in grace period
      * ``"16-30"``   — late, 16-30 days
      * ``"31-120"``  — late, 31-120 days
      * ``"Default"`` — terminal default (Charged Off / Default)
      * ``"other"``   — anything unrecognized (still non-null; never leaks NaN)
    """
    s = _norm(status)
    if s in DEFAULT_STATUSES:
        return "Default"
    if s in {"current", "fully paid"}:
        return "0"
    if s == "in grace period":
        return "1-30"
    if s == "late (16-30 days)":
        return "16-30"
    if s == "late (31-120 days)":
        return "31-120"
    return "other"


# ---------------------------------------------------------------------------
# build_label — eventual charge-off / default from final current_status.
# Returns an Arrow bool array (the Series[bool] equivalent, kept native so the
# Arrow pipeline never has to round-trip through pandas).
# ---------------------------------------------------------------------------
def build_label(raw: pa.Table) -> pa.Array:
    """Return ``label_default`` as a non-null ``pa.Array`` of bool.

    ``label_default = is_default(current_status)`` — eventual charge-off /
    default only (NOT a 30-day roll; the source is cross-sectional so a roll
    label is not computable). As-of date is irrelevant to this label because it
    reads only the *final* status, so it takes no ``as_of`` argument (outcome-
    derived fields never leak into training features because
    :func:`build_features` carries only snapshot + payment-total-derived
    behavioral features, never status-derived fields other than the bucket).
    """
    statuses = raw.column("current_status").to_pylist()
    flags = [is_default(s) for s in statuses]
    return pa.array(flags, type=pa.bool_())


# ---------------------------------------------------------------------------
# build_features — snapshot + behavioral features shaped to the FeatureFrame
# contract. CPU/pyarrow reference path (the GPU path is gpu/run_features.py).
# ---------------------------------------------------------------------------
# A tiny epsilon so total_paid/amount never divides by zero. Loan amounts are in
# whole dollars and always > 0 in practice, so this only guards the degenerate
# row (amount == 0) by forcing its ratios to 0.0 via if_else below.
_AMOUNT_EPS = 1e-9


def _safe_ratio(numerator: pa.Array, amount: pa.Array) -> pa.Array:
    """``numerator / amount`` with the amount==0 rows forced to 0.0 (no inf/nan)."""
    safe = pc.max_element_wise(amount, pa.scalar(_AMOUNT_EPS, type=pa.float64()))
    ratio = pc.divide(numerator, safe)
    return pc.if_else(pc.equal(amount, pa.scalar(0.0, type=pa.float64())), 0.0, ratio)


def build_features(raw: pa.Table, as_of: dt.date) -> pa.Table:
    """Build a :class:`FeatureFrame`-shaped table from a ``RawLoans`` snapshot.

    Parameters
    ----------
    raw
        A ``RawLoans``-contract Arrow table (validated up front; drift fails loud).
    as_of
        The snapshot / scoring date. ``loan_age`` is months from ``issue_date``
        to ``as_of`` (clamped at 0). Configurable so a vintage split can score a
        loan as-of a past date without outcome leakage.

    Returns
    -------
    pa.Table
        Validated against :class:`FeatureFrame` — every contract field present,
        correctly typed, and **non-null** (asserted via :func:`assert_no_nulls`).
        Extra columns are not added; the output is exactly the contract.
    """
    validate_table(raw, RawLoans, name="build_features(raw)")

    # --- snapshot features (carried verbatim from RawLoans) ---
    carried = {
        name: raw.column(name)
        for name in (
            "loan_id", "amount", "term", "rate", "grade",
            "annual_income", "dti", "purpose", "region",
        )
    }

    # --- behavioral features (derived) ---
    # loan_age: whole months from issue_date to as_of, clamped at 0.
    issue = raw.column("issue_date")  # date32 per RawLoans contract
    issue_ym = pc.add(
        pc.multiply(pc.cast(pc.year(issue), pa.int64()), 12),
        pc.cast(pc.month(issue), pa.int64()),
    )
    as_of_ym = pa.scalar(as_of.year * 12 + as_of.month, type=pa.int64())
    loan_age = pc.max_element_wise(pc.subtract(as_of_ym, issue_ym), pa.scalar(0, type=pa.int64()))

    amount = raw.column("amount")
    payment_ratio = _safe_ratio(raw.column("total_paid"), amount)
    outstanding_ratio = _safe_ratio(raw.column("outstanding_principal"), amount)

    statuses = raw.column("current_status").to_pylist()
    delinq = pa.array([delinquency_bucket(s) for s in statuses], type=pa.string())

    label_default = build_label(raw)
    as_of_col = pa.array([as_of] * raw.num_rows, type=pa.date32())

    out = pa.table(
        {
            **carried,
            "loan_age": loan_age,
            "payment_ratio": payment_ratio,
            "outstanding_ratio": outstanding_ratio,
            "delinquency_status": delinq,
            "label_default": label_default,
            "as_of_date": as_of_col,
        },
        schema=schema_from_dataclass(FeatureFrame),
    )
    validate_table(out, FeatureFrame, name="build_features(out)")
    assert_no_nulls(out, FeatureFrame)
    return out


def assert_no_nulls(table: pa.Table, contract: type) -> None:
    """Raise ``ValueError`` if any contract field of ``table`` contains nulls.

    Contract fields are all ``nullable=False`` (see :func:`schema_from_dataclass`),
    so a null in any of them is a contract violation — fail loud rather than
    silently feeding NaN into a downstream model (WA-005).
    """
    import dataclasses

    names = [f.name for f in dataclasses.fields(contract)]
    offenders = [n for n in names if pc.sum(pc.is_null(table.column(n))).as_py() > 0]
    if offenders:
        raise ValueError(
            f"{contract.__name__} has nulls in required field(s): {offenders} "
            "(all contract fields are non-nullable)."
        )
