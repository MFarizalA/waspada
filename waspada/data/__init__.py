"""Data layer for WASPADA — the shared engine's data door.

WA-002 ships :class:`~waspada.data.bq.BigQueryClient` and
:func:`~waspada.data.bq.fetch_loans` for reading raw loans from BigQuery as
Arrow. The package is import-safe without the google-cloud SDK installed: only
the actual query path imports the SDK.
"""
from __future__ import annotations

from .bq import BigQueryClient, fetch_loans

__all__ = ["BigQueryClient", "fetch_loans"]
