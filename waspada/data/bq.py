"""The shared engine's data door: a reusable BigQuery client (WA-002).

One client serves both decision lanes. It reads creds from the environment
(via :mod:`waspada.config`), runs a query, and returns Arrow through the
BigQueryStorage fast path. :meth:`BigQueryClient.fetch_loans` returns a
``RawLoans``-shaped Arrow table (the WA-001 frozen contract), validated by
:func:`waspada.schema.validate_table`.

The BQ ``loans`` table columns are an exact 1:1 match for the :class:`RawLoans`
dataclass field names (verified against the live sandbox), so no column aliasing
is needed. The lane selects the table location; the contract is the same for
every lane (origination gets a second table later, same shape).
"""
from __future__ import annotations

import dataclasses
import os
from typing import Any, Dict, Optional

import pyarrow as pa

from ..config import COLLECTIONS, ORIGINATION, Config, load_config
from ..schema import RawLoans, validate_table

__all__ = ["BigQueryClient", "fetch_loans"]

# Columns we SELECT for the RawLoans contract — the exact field names, in the
# dataclass declaration order. Keeping this explicit (rather than `SELECT *`)
# means a stray column added upstream never silently reshapes the contract, and
# keeps the SELECT order stable for downstream readers.
_RAW_LOANS_COLUMNS: tuple[str, ...] = tuple(f.name for f in dataclasses.fields(RawLoans))


def _creds_configured() -> bool:
    """True iff the three BQ env vars + the credentials file path are all set.

    We check the credentials *path* is set (not that the file exists) so a
    misconfigured path still fails loudly at query time with the SDK's own
    error, rather than a confusing "file not found" from the gate.
    """
    return bool(
        os.environ.get("BQ_PROJECT")
        and os.environ.get("BQ_DATASET")
        and os.environ.get("BQ_TABLE")
        and os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    )


class BigQueryClient:
    """Thin wrapper over ``google.cloud.bigquery`` returning Arrow tables.

    Built from the ``BQ_PROJECT`` env var. :meth:`query` runs SQL and exports
    the result to Arrow through BigQueryStorage (the fast Arrow path; the SDK's
    ``to_arrow`` uses the storage API by default for results). :meth:`table_meta`
    reports row count, schema, and freshness — the freshness check the Ingest
    agent (WA-009) reuses to gate stale reads.
    """

    def __init__(self, config: Optional[Config] = None, *, _client: Any = None) -> None:
        if not _creds_configured():
            raise RuntimeError(
                "BQ credentials not configured: set BQ_PROJECT, BQ_DATASET, "
                "BQ_TABLE, and GOOGLE_APPLICATION_CREDENTIALS (see .env.example)."
            )
        self._cfg = config or load_config()
        # Lazily import so the module is importable without the SDK installed
        # (tests that only check the no-creds gate never touch google-cloud).
        if _client is not None:
            self._client = _client
        else:
            from google.cloud import bigquery

            self._client = bigquery.Client(project=self._cfg.bq_project)

    # ------------------------------------------------------------------ core
    def query(self, sql: str) -> pa.Table:
        """Run ``sql`` and return the result as a :class:`pyarrow.Table`.

        ``QueryJob.to_arrow`` uses the BigQueryStorage API for fast Arrow export
        when the dependency is present (it is, per requirements.txt).
        """
        job = self._client.query(sql)
        result = job.result()  # blocks until done; raises on job error
        return result.to_arrow(create_bqstorage_client=True)

    def table_meta(self, dataset_table: str) -> Dict[str, Any]:
        """Return ``{n_rows, schema, freshness}`` for ``project.dataset.table``.

        ``dataset_table`` may be ``dataset.table`` (uses the client's project)
        or a fully-qualified ``project.dataset.table``. ``freshness`` is the
        table's ``last_modified_time`` as an ISO-8601 UTC string; ``schema`` is a
        ``[{name, type, mode}]`` list mirroring the BQ field definitions.
        """
        from google.cloud import bigquery

        parts = dataset_table.split(".")
        if len(parts) == 3:
            project, dataset_id, table_id = parts
        elif len(parts) == 2:
            project, dataset_id, table_id = (
                self._cfg.bq_project,
                parts[0],
                parts[1],
            )
        else:
            raise ValueError(
                "dataset_table must be 'dataset.table' or "
                "'project.dataset.table'"
            )
        table_ref = bigquery.TableReference(
            bigquery.DatasetReference(project, dataset_id), table_id
        )
        meta = self._client.get_table(table_ref)
        return {
            "n_rows": int(meta.num_rows or 0),
            "schema": [
                {"name": f.name, "type": f.field_type, "mode": f.mode}
                for f in meta.schema
            ],
            "freshness": meta.modified.isoformat() if meta.modified else None,
        }

    # --------------------------------------------------------------- helpers
    @property
    def config(self) -> Config:
        return self._cfg

    def _fully_qualified_table(self) -> str:
        """The loans table for the configured lane as ``project.dataset.table``."""
        return f"{self._cfg.bq_project}.{self._cfg.bq_dataset}.{self._cfg.bq_table}"

    def fetch_loans(self, lane: str = COLLECTIONS, *, limit: Optional[int] = None) -> pa.Table:
        """Return a ``RawLoans``-shaped Arrow table for ``lane``.

        Selects exactly the :class:`RawLoans` columns (in declaration order) so
        the result validates cleanly against the frozen contract. ``limit``
        caps the row count (the smoke test passes a small LIMIT). The result is
        validated with :func:`waspada.schema.validate_table`; a schema drift
        upstream fails loudly here instead of silently downstream.
        """
        if lane not in (COLLECTIONS, ORIGINATION):
            raise ValueError(
                f"lane={lane!r} is invalid; must be 'collections' or 'origination'"
            )
        cols = ", ".join(_RAW_LOANS_COLUMNS)
        sql = f"SELECT {cols} FROM `{self._fully_qualified_table()}`"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        table = self.query(sql)
        validate_table(table, RawLoans, name=f"fetch_loans({lane})")
        return table


def fetch_loans(lane: str = COLLECTIONS, *, limit: Optional[int] = None) -> pa.Table:
    """Module-level convenience: a fresh :class:`BigQueryClient` then ``fetch_loans``."""
    return BigQueryClient().fetch_loans(lane=lane, limit=limit)
