"""WA-089 — pluggable data-source layer acceptance.

Every source must yield a RawLoans-conformant Arrow table (the frozen-contract gate is
what makes the downstream pipeline source-agnostic). The selector resolves by arg → env →
default. Synthetic is the offline-safe, legal-clean default; LendingClub maps the public CSV.
"""
from __future__ import annotations

import csv
import dataclasses
import os

import pyarrow as pa
import pytest

from waspada.data.sources import (
    LendingClubSource,
    RawLoansSource,
    SyntheticSource,
    get_source,
)
from waspada.schema import RawLoans, validate_table

_RAW_COLS = [f.name for f in dataclasses.fields(RawLoans)]


# --------------------------------------------------------------------------- #
# Synthetic source — the offline-safe, legal-clean default.
# --------------------------------------------------------------------------- #
def test_synthetic_source_yields_valid_rawloans():
    t = SyntheticSource(n=500, seed=7).fetch()
    assert t.num_rows == 500
    assert list(t.column_names) == _RAW_COLS
    validate_table(t, RawLoans, name="test")  # must not raise


def test_synthetic_source_is_deterministic_by_seed():
    a = SyntheticSource(n=200, seed=42).fetch()
    b = SyntheticSource(n=200, seed=42).fetch()
    assert a.equals(b)  # same seed -> byte-identical (reproducible runs)
    c = SyntheticSource(n=200, seed=99).fetch()
    assert not a.equals(c)  # different seed -> different data


def test_synthetic_source_limit():
    assert SyntheticSource(n=1000, seed=1).fetch(limit=50).num_rows == 50


# --------------------------------------------------------------------------- #
# Selector — arg → WASPADA_DATA_SOURCE env → default(synthetic).
# --------------------------------------------------------------------------- #
def test_get_source_default_is_synthetic(monkeypatch):
    monkeypatch.delenv("WASPADA_DATA_SOURCE", raising=False)
    assert isinstance(get_source(), SyntheticSource)


def test_get_source_reads_env(monkeypatch):
    monkeypatch.setenv("WASPADA_DATA_SOURCE", "lending_club")
    assert isinstance(get_source(), LendingClubSource)


def test_get_source_arg_overrides_env(monkeypatch):
    monkeypatch.setenv("WASPADA_DATA_SOURCE", "lending_club")
    assert isinstance(get_source("synthetic"), SyntheticSource)


def test_get_source_unknown_raises():
    with pytest.raises(ValueError, match="unknown data source"):
        get_source("nope")


def test_get_source_passes_kwargs():
    src = get_source("synthetic", n=42, seed=3)
    assert src.n == 42 and src.seed == 3
    assert src.fetch().num_rows == 42


# --------------------------------------------------------------------------- #
# LendingClub source — public CSV → RawLoans (canonical WA-078 map).
# --------------------------------------------------------------------------- #
def _mini_lc_csv(path: str, rows: int = 30) -> None:
    cols = ["id", "loan_amnt", "term", "int_rate", "grade", "annual_inc", "dti",
            "issue_d", "purpose", "addr_state", "out_prncp", "total_pymnt", "loan_status"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for i in range(rows):
            w.writerow([str(1000 + i), "12000", " 36 months", "11.5%", "B", "70000",
                        "15.0", "Mar-2017", "car", ["CA", "NY", "TX", "IL"][i % 4],
                        "3000.0", "5000.0", "Fully Paid"])
        w.writerow(["Total amount funded: 1"] + [""] * (len(cols) - 1))  # junk footer, must drop


def test_lending_club_source_maps_csv_to_rawloans(tmp_path):
    csv_path = str(tmp_path / "mini_lc.csv")
    _mini_lc_csv(csv_path, rows=30)
    t = LendingClubSource(csv_path=csv_path).fetch()
    assert t.num_rows == 30  # junk footer dropped
    assert list(t.column_names) == _RAW_COLS
    validate_table(t, RawLoans, name="lc-test")
    assert set(t.column("region").to_pylist()) <= {"West", "Northeast", "South", "Midwest", "Other"}


def test_lending_club_source_missing_csv_raises():
    with pytest.raises(FileNotFoundError, match="needs a CSV"):
        LendingClubSource(csv_path="/no/such/file.csv").fetch()


# --------------------------------------------------------------------------- #
# The invariant: sources are interchangeable — identical RawLoans schema.
# --------------------------------------------------------------------------- #
def test_sources_are_schema_identical(tmp_path):
    """Downstream is source-agnostic: every source emits the same RawLoans schema."""
    syn = SyntheticSource(n=10).fetch()
    csv_path = str(tmp_path / "lc.csv"); _mini_lc_csv(csv_path, rows=10)
    lc = LendingClubSource(csv_path=csv_path).fetch()
    assert syn.schema.equals(lc.schema)          # same contract, whatever the source
    assert list(syn.column_names) == _RAW_COLS == list(lc.column_names)
    assert isinstance(SyntheticSource(), RawLoansSource)
    assert isinstance(LendingClubSource(), RawLoansSource)
