"""Data layer for WASPADA — the shared engine's data door.

Ships :class:`~waspada.data.oss.OSSClient` and
:func:`~waspada.data.oss.fetch_loans` for reading the raw loans snapshot from
Alibaba Cloud OSS as Arrow. The package is import-safe without the ``oss2``
SDK installed: only the actual fetch path imports the SDK.
"""
from __future__ import annotations

from .oss import OSSClient, fetch_loans

__all__ = ["OSSClient", "fetch_loans"]
