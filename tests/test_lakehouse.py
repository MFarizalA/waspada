"""Tests for waspada.data.lakehouse — WA-060 DuckDB RDS analytics fallback."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from waspada.data.lakehouse import get_analytics_connection


class TestGetAnalyticsConnection:
    """Verify get_analytics_connection() degrades gracefully."""

    def test_returns_local_duckdb_when_endpoint_empty(self, monkeypatch):
        """With DUCKDB_RDS_ENDPOINT unset, we get a local in-memory DuckDB."""
        monkeypatch.delenv("DUCKDB_RDS_ENDPOINT", raising=False)
        monkeypatch.delenv("DUCKDB_RDS_PORT", raising=False)

        con = get_analytics_connection()
        # Should be a duckdb.DuckDBPyConnection — no network calls.
        assert hasattr(con, "execute")
        result = con.execute("SELECT 1 AS one").fetchone()
        assert result[0] == 1
        con.close()

    def test_returns_local_duckdb_when_endpoint_blank(self, monkeypatch):
        """Blank/whitespace-only endpoint also falls back to local DuckDB."""
        monkeypatch.setenv("DUCKDB_RDS_ENDPOINT", "   ")
        monkeypatch.delenv("DUCKDB_RDS_PORT", raising=False)

        con = get_analytics_connection()
        assert hasattr(con, "execute")
        result = con.execute("SELECT 42 AS answer").fetchone()
        assert result[0] == 42
        con.close()

    def test_uses_rds_port_when_set(self, monkeypatch):
        """When endpoint is set, port is read from env (smoke test only)."""
        pytest.importorskip("pymysql", reason="pymysql not installed in test env")

        monkeypatch.setenv("DUCKDB_RDS_ENDPOINT", "rm-fake.mysql.rds.aliyuncs.com")
        monkeypatch.setenv("DUCKDB_RDS_PORT", "3307")
        monkeypatch.setenv("RDS_PASSWORD", "fake-pass")

        # We don't actually connect — just verify pymysql.connect would be
        # called with the right arguments. Patch at the import location.
        with patch("pymysql.connect") as mock_connect:
            mock_connect.return_value = object()
            con = get_analytics_connection()
            mock_connect.assert_called_once_with(
                host="rm-fake.mysql.rds.aliyuncs.com",
                port=3307,
                user="waspada",
                password="fake-pass",
                database="waspada",
            )
            assert con is mock_connect.return_value
