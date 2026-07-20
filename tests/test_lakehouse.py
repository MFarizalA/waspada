"""Tests for waspada.data.lakehouse — the DuckDB read surface.

Covers the WA-060 analytics-connection fallback and the WA-047 honest
``load_to_duckdb`` (Arrow / local-Parquet register; OSS is read elsewhere).
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pyarrow as pa
import pytest

from waspada.data.lakehouse import (
    Lakehouse,
    get_analytics_connection,
    load_to_duckdb,
)


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


class TestLoadToDuckdb:
    """WA-047: the honest read surface — Arrow / local Parquet, no dlt/OSS."""

    def test_arrow_table_is_registered_and_queryable(self):
        tbl = pa.table({"loan_id": ["L1", "L2"], "amount": [100.0, 250.0]})
        lh = load_to_duckdb(arrow=tbl, table="raw_loans")
        assert isinstance(lh, Lakehouse) and lh.table == "raw_loans"
        assert lh.scalar("SELECT COUNT(*) FROM raw_loans") == 2
        assert lh.scalar("SELECT SUM(amount) FROM raw_loans") == 350.0

    def test_local_parquet_is_read(self, tmp_path):
        import pyarrow.parquet as pq
        p = tmp_path / "loans.parquet"
        pq.write_table(pa.table({"loan_id": ["L1"], "amount": [42.0]}), p)
        lh = load_to_duckdb(local_parquet=str(p), table="raw_loans")
        assert lh.scalar("SELECT amount FROM raw_loans") == 42.0

    def test_no_source_raises_a_clear_error_not_a_silent_empty_read(self):
        with pytest.raises(RuntimeError, match="no source"):
            load_to_duckdb()

    def test_no_dlt_landmine_remains(self):
        """The removed dead path imported dlt and called a nonexistent
        ``dlt.readers.filesystem`` API. Guard the executable code (not the
        docstring, which names the removed API) against it creeping back."""
        import ast
        import inspect
        from waspada.data import lakehouse
        tree = ast.parse(inspect.getsource(lakehouse))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert "dlt" not in imported                       # no dlt import
        assert not hasattr(lakehouse, "_oss_s3_endpoint")  # dead helper removed
