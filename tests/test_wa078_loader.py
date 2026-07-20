"""WA-078 — Lending Club → RawLoans mapping acceptance.

The multi-GB CSV isn't in the repo, but the transform that decides whether the
uploaded object will pass ``fetch_loans`` IS pure and testable. These lock the
column mapping, the type coercions (``%``-rate, ' 36 months', 'Dec-2018'), the
state→region projection, the drop-invalid rule, and — the real contract — that
the built table passes the SAME ``validate_table(RawLoans)`` the reader runs.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os

import pyarrow as pa
import pytest

# Import the script module by path (scripts/ isn't a package).
_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "scripts", "load_lending_club.py")
_spec = importlib.util.spec_from_file_location("load_lending_club", _SCRIPT)
lc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lc)  # type: ignore

from waspada.schema import RawLoans, validate_table


def _lc_row(**over) -> dict:
    """A realistic Lending Club accepted-loans row; override fields per test."""
    row = {
        "id": "12345678", "loan_amnt": "10000", "term": " 36 months",
        "int_rate": "13.56%", "grade": "C", "annual_inc": "65000",
        "dti": "18.24", "issue_d": "Dec-2018", "purpose": "debt_consolidation",
        "addr_state": "CA", "out_prncp": "4200.50", "total_pymnt": "6100.75",
        "loan_status": "Current",
    }
    row.update(over)
    return row


def test_map_row_happy_path():
    r = lc.map_row(_lc_row())
    assert r == {
        "loan_id": "12345678", "amount": 10000.0, "term": 36, "rate": 13.56,
        "grade": "C", "annual_income": 65000.0, "dti": 18.24,
        "issue_date": dt.date(2018, 12, 1), "purpose": "debt_consolidation",
        "region": "West", "outstanding_principal": 4200.50,
        "total_paid": 6100.75, "current_status": "Current",
    }


def test_term_rate_date_coercions():
    r = lc.map_row(_lc_row(term="60 months", int_rate=" 7.35 ", issue_d="Jan-2016"))
    assert r["term"] == 60 and r["rate"] == 7.35 and r["issue_date"] == dt.date(2016, 1, 1)


@pytest.mark.parametrize("state,region", [
    ("NY", "Northeast"), ("IL", "Midwest"), ("TX", "South"), ("WA", "West"),
    ("ZZ", "Other"), ("", "Other"),
])
def test_state_to_region(state, region):
    assert lc.map_row(_lc_row(addr_state=state))["region"] == region


@pytest.mark.parametrize("bad", [
    {"id": ""}, {"id": "https://lendingclub.com"},      # header/footer junk
    {"loan_amnt": ""}, {"dti": "n/a"}, {"issue_d": ""},  # unparseable required field
    {"grade": ""}, {"loan_status": ""},                  # missing required string
])
def test_invalid_rows_dropped(bad):
    assert lc.map_row(_lc_row(**bad)) is None


def test_rows_to_table_passes_validate_table():
    """The real contract: the built table is exactly what fetch_loans validates."""
    rows = [lc.map_row(_lc_row(id=str(1000 + i), addr_state=s))
            for i, s in enumerate(["CA", "NY", "TX", "IL"])]
    table = lc.rows_to_table([r for r in rows if r])
    assert table.num_rows == 4
    # Must not raise — same validation the OSS reader applies.
    validate_table(table, RawLoans, name="wa078-test")
    assert list(table.column_names) == [f.name for f in __import__("dataclasses").fields(RawLoans)]
    assert pa.types.is_date(table.schema.field("issue_date").type)
