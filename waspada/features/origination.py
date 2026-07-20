"""Origination feature recipe (WA-035) — application-time features + label.

Parallel to :mod:`waspada.features.collections`, but for the Origination lane:
an APPLICANT has no payment history, so every feature here is knowable at
application time. The frame is **deterministic** — the Data Analyst LLM may
explore it, but it never computes it (the WA-030 rule).

Leakage guard (the acceptance centerpiece)
------------------------------------------
Nothing outcome-derived may enter the feature matrix. ``funded`` and
``funded_default`` are OUTCOME columns (they exist only so the label can be
built for training) — they are consumed by :func:`build_label` and then
**dropped**; they are not ApplicationFeatureFrame fields and are additionally
listed in the model layer's per-lane ``LEAKAGE_EXCLUDED`` (WA-036).

Label honesty note (mirrors schema.py WA-034)
---------------------------------------------
The LendingClub snapshot contains only FUNDED loans; there is no
rejected-applications outcome data. ``label_default`` here is
**funded-then-defaulted** — reject-inference is impossible from this data, and
we never claim approve/reject ground truth.
"""
from __future__ import annotations

import datetime as dt

import pyarrow as pa
import pyarrow.compute as pc

from ..schema import ApplicationFeatureFrame, RawApplications, schema_from_dataclass, validate_table
from .collections import assert_no_nulls

__all__ = ["build_features", "build_label"]


def build_label(raw: pa.Table) -> pa.Array:
    """The origination label: **funded-then-defaulted** (bool per application).

    ``funded_default`` is only meaningful when ``funded`` is true; the label is
    their conjunction so an unfunded application can never be a positive. This
    is outcome data — it exists for training only and never enters the features.
    """
    return pc.and_(raw.column("funded"), raw.column("funded_default"))


def build_features(raw: pa.Table, as_of: dt.date) -> pa.Table:
    """``RawApplications`` → a validated :class:`ApplicationFeatureFrame` table.

    All features are application-time (no payment history exists):

    * carried: ``amount``, ``term``, ``requested_rate``, ``grade``,
      ``annual_income``, ``dti``, ``employment_length``, ``purpose``, ``region``
    * derived: ``loan_to_income = amount / annual_income`` (0 when income is 0 —
      the same guarded-ratio discipline as collections' ``_safe_ratio``)
    * label: funded-then-defaulted (:func:`build_label`)

    Output is contract-validated and non-null (:func:`assert_no_nulls`).
    """
    validate_table(raw, RawApplications, name="build_features(raw)")

    amount = pc.cast(raw.column("amount"), pa.float64())
    income = pc.cast(raw.column("annual_income"), pa.float64())
    # Guarded ratio: income of 0 → 0.0 rather than inf/NaN (fail-safe, non-null).
    safe_income = pc.if_else(pc.equal(income, 0.0), pa.scalar(1.0, pa.float64()), income)
    lti = pc.if_else(
        pc.equal(income, 0.0),
        pa.scalar(0.0, pa.float64()),
        pc.divide(amount, safe_income),
    )

    out = pa.table(
        {
            "application_id": raw.column("application_id"),
            "amount": amount,
            "term": pc.cast(raw.column("term"), pa.int64()),
            "requested_rate": pc.cast(raw.column("requested_rate"), pa.float64()),
            "grade": raw.column("grade"),
            "annual_income": income,
            "dti": pc.cast(raw.column("dti"), pa.float64()),
            "loan_to_income": lti,
            "employment_length": pc.cast(raw.column("employment_length"), pa.int64()),
            "purpose": raw.column("purpose"),
            "region": raw.column("region"),
            "application_date": raw.column("application_date"),
            "label_default": build_label(raw),
            "as_of_date": pa.array([as_of] * raw.num_rows, type=pa.date32()),
        },
        schema=schema_from_dataclass(ApplicationFeatureFrame),
    )
    validate_table(out, ApplicationFeatureFrame, name="build_features(out)")
    assert_no_nulls(out, ApplicationFeatureFrame)
    return out
