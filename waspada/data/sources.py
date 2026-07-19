"""Pluggable data-source layer (WA-089) — any source → the frozen RawLoans contract.

Everything downstream of ``RawLoans`` (features → score → the society's debate) is
**source-agnostic**: it only ever sees a ``RawLoans``-conformant Arrow table. This module
is the ONE place that knows about a concrete source and maps it to that contract, so the
pipeline runs unchanged whether the data is synthetic, a public dataset, or (later) a
production database.

Sources, selected by ``WASPADA_DATA_SOURCE``:

  * ``synthetic``    — generate ``RawLoans`` directly. **No real data → no license, no PII,
                       freely publishable** (the legal-clean escape hatch). Deterministic by seed.
  * ``lending_club`` — the public LendingClub accepted-loans CSV (CC0) → ``RawLoans``,
                       via the single canonical WA-078 map (``scripts/load_lending_club.py``).

``bondora`` / ``sql`` land in follow-ups — see ``backlog/data-pipeline-architecture.md`` §3.

Design note: the *code* default is ``synthetic`` (offline-safe — no creds, no CSV, always
works); *production* sets ``WASPADA_DATA_SOURCE=lending_club`` (+ ``LC_SOURCE_PATH``). This
keeps offline/CI/tests green with zero configuration while the deployed pipeline reads real data.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import pyarrow as pa

from ..schema import RawLoans, validate_table

__all__ = [
    "RawLoansSource", "SyntheticSource", "LendingClubSource", "get_source", "SOURCES",
]

_RAW_COLUMNS = [f.name for f in dataclasses.fields(RawLoans)]


class RawLoansSource(ABC):
    """A data source that yields a ``RawLoans``-conformant Arrow table.

    Every concrete source maps its origin schema to ``RawLoans`` and validates before
    returning, so the frozen-contract gate holds no matter where the data came from —
    that gate is exactly what makes the downstream pipeline source-agnostic.
    """

    name: str = "source"

    @abstractmethod
    def fetch(self, *, limit: Optional[int] = None) -> pa.Table:
        """Return a ``RawLoans``-conformant Arrow table (optionally the first ``limit`` rows)."""
        raise NotImplementedError

    def _validated(self, table: pa.Table, limit: Optional[int]) -> pa.Table:
        """Select the contract columns (declaration order), slice, and validate."""
        table = table.select(_RAW_COLUMNS)
        if limit is not None:
            table = table.slice(0, int(limit))
        validate_table(table, RawLoans, name=f"{self.name}Source")
        return table


class SyntheticSource(RawLoansSource):
    """Generate ``RawLoans`` directly — no real data, no license, freely publishable.

    The legal-clean default: reproducible by ``seed`` so runs are deterministic. Delegates
    to the framework's synthetic generator so the shape always matches the contract.
    """

    name = "synthetic"

    def __init__(self, n: int = 1000, seed: int = 11) -> None:
        self.n = int(n)
        self.seed = int(seed)

    def fetch(self, *, limit: Optional[int] = None) -> pa.Table:
        from ..agents.__main__ import _sample_raw_table  # lazy: avoid import-time coupling
        n = int(limit) if limit else self.n
        return self._validated(_sample_raw_table(n=n, seed=self.seed), None)


class LendingClubSource(RawLoansSource):
    """Public LendingClub accepted-loans CSV (CC0) → ``RawLoans``.

    Delegates the column mapping to the WA-078 loader (``scripts/load_lending_club.py``) — the
    single canonical LC→RawLoans map — loaded on demand so there's no drift between the ad-hoc
    upload script and this source layer.
    """

    name = "lending_club"

    def __init__(self, csv_path: Optional[str] = None, sample: Optional[int] = None,
                 seed: int = 42) -> None:
        self.csv_path = csv_path or os.environ.get("LC_SOURCE_PATH", "")
        self.sample = sample
        self.seed = int(seed)

    @staticmethod
    def _loader():
        root = Path(__file__).resolve().parents[2]
        path = root / "scripts" / "load_lending_club.py"
        spec = importlib.util.spec_from_file_location("wa078_loader", str(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

    def fetch(self, *, limit: Optional[int] = None) -> pa.Table:
        if not self.csv_path or not os.path.exists(self.csv_path):
            raise FileNotFoundError(
                f"LendingClubSource needs a CSV: set LC_SOURCE_PATH or pass csv_path "
                f"(got {self.csv_path!r}). See backlog/WA-078.md."
            )
        loader = self._loader()
        sample = limit if limit is not None else self.sample
        rows = loader.stream_sample(self.csv_path, sample, self.seed)
        if not rows:
            raise ValueError(
                "LendingClubSource produced no valid rows — is this the LendingClub "
                "accepted-loans CSV? (check for a pre-header line.)"
            )
        return self._validated(loader.rows_to_table(rows), None)


SOURCES = {
    "synthetic": SyntheticSource,
    "lending_club": LendingClubSource,
}


def get_source(name: Optional[str] = None, **kwargs) -> RawLoansSource:
    """Resolve a data source. Precedence: ``name`` arg → ``WASPADA_DATA_SOURCE`` env → ``synthetic``.

    ``synthetic`` is the offline-safe default (no creds, no CSV, legal-clean); production sets
    ``WASPADA_DATA_SOURCE=lending_club`` (with ``LC_SOURCE_PATH``). Extra kwargs pass to the
    source constructor (``n``/``seed`` for synthetic; ``csv_path``/``sample``/``seed`` for LC).
    """
    key = (name or os.environ.get("WASPADA_DATA_SOURCE") or "synthetic").strip().lower()
    if key not in SOURCES:
        raise ValueError(
            f"unknown data source {key!r}; set WASPADA_DATA_SOURCE to one of {sorted(SOURCES)}"
        )
    return SOURCES[key](**kwargs)
