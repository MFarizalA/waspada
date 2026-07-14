"""WA-045: Agent worker egress controls — prevent sensitive data exfiltration.

Tests the defense-in-depth egress guardrails:

  1. **LLM base_url allowlist** — ``QwenLLM`` rejects a
     ``base_url`` that doesn't match its known-good provider endpoint at
     construction time, before any data can leave the process.
  2. **DuckDB SQL column allowlist** — ``_safe_sql_check`` rejects queries
     that reference columns outside the RawLoans / FeatureFrame contract.
  3. **Normal operation unaffected** — valid base_urls and valid queries
     continue to work exactly as before.
"""
from __future__ import annotations

import json
import os

import pytest

from waspada.agents.llm import QwenLLM


# --------------------------------------------------------------------------- #
# 1. LLM base_url allowlist
# --------------------------------------------------------------------------- #
class TestQwenBaseUrlAllowlist:
    """QwenLLM must only connect to DashScope (dashscope.aliyuncs.com)."""

    def test_blocked_base_url_raises_value_error(self, monkeypatch):
        """A non-DashScope base_url raises ValueError at construction."""
        monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-123")
        with pytest.raises(ValueError, match="dashscope.aliyuncs.com"):
            QwenLLM(base_url="https://evil.exfil.example.com/v1")

    def test_blocked_base_url_via_env_raises_value_error(self, monkeypatch):
        """Env override pointing to a bad host is also blocked."""
        monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-123")
        monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://attacker-controlled.net/v1")
        with pytest.raises(ValueError, match="dashscope.aliyuncs.com"):
            QwenLLM()

    def test_valid_dashscope_intl_base_url_accepted(self, monkeypatch):
        """The default DashScope intl endpoint is accepted (no ValueError)."""
        monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-123")
        monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)
        # Should not raise — construction succeeds. (The OpenAI client is
        # created lazily but no network call happens at construction.)
        llm = QwenLLM()
        assert llm.name == "qwen"

    def test_explicit_valid_base_url_accepted(self, monkeypatch):
        """Explicitly passing the default endpoint also works."""
        monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-123")
        llm = QwenLLM(
            base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        )
        assert llm.model_name  # construction completed

    def test_cn_dashscope_variant_accepted(self, monkeypatch):
        """The CN variant of DashScope is also a valid endpoint."""
        monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-123")
        llm = QwenLLM(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        assert llm.name == "qwen"


# --------------------------------------------------------------------------- #
# 2. DuckDB SQL column allowlist
# --------------------------------------------------------------------------- #
from waspada.agents.data_analyst import _safe_sql_check, _check_column_allowlist


class TestSqlColumnAllowlist:
    """_safe_sql_check must reject queries referencing unknown columns."""

    def test_unknown_column_blocked(self):
        """A query selecting a column not in the contract is blocked."""
        err = _safe_sql_check(
            "SELECT secret_api_key FROM raw_loans"
        )
        assert err is not None
        assert "secret_api_key" in err
        assert "egress" in err.lower()

    def test_multiple_unknown_columns_blocked(self):
        """Multiple unknown columns are all reported."""
        err = _safe_sql_check(
            "SELECT password, ssn, internal_key FROM feature_frame"
        )
        assert err is not None
        assert "password" in err
        assert "ssn" in err

    def test_valid_feature_frame_columns_accepted(self):
        """Queries using only FeatureFrame contract columns pass."""
        assert _safe_sql_check(
            "SELECT grade, AVG(payment_ratio) FROM feature_frame GROUP BY grade"
        ) is None

    def test_valid_raw_loans_columns_accepted(self):
        """Queries using only RawLoans contract columns pass."""
        assert _safe_sql_check(
            "SELECT grade, COUNT(*) FROM raw_loans GROUP BY grade LIMIT 10"
        ) is None

    def test_star_query_accepted(self):
        """SELECT * passes — the wildcard is not an unknown column."""
        assert _safe_sql_check("SELECT * FROM raw_loans") is None

    def test_dotted_column_reference_accepted(self):
        """table.column references are correctly parsed."""
        assert _safe_sql_check(
            "SELECT raw_loans.dti, feature_frame.payment_ratio "
            "FROM raw_loans JOIN feature_frame ON raw_loans.loan_id = feature_frame.loan_id"
        ) is None

    def test_aggregate_functions_accepted(self):
        """Common DuckDB aggregates don't trigger the column guard."""
        assert _safe_sql_check(
            "SELECT grade, COUNT(*), AVG(dti), MIN(rate), MAX(amount) "
            "FROM raw_loans GROUP BY grade"
        ) is None

    def test_subquery_with_alias_accepted(self):
        """Aliases (AS) and subqueries with contract columns pass."""
        assert _safe_sql_check(
            "SELECT grade, avg_dti FROM ("
            "  SELECT grade, AVG(dti) AS avg_dti FROM feature_frame GROUP BY grade"
            ") sub"
        ) is None

    def test_non_select_still_blocked(self):
        """The original SELECT-only guard still works."""
        assert _safe_sql_check("DROP TABLE raw_loans") is not None
        assert "only SELECT" in _safe_sql_check("DROP TABLE raw_loans")

    def test_chained_statement_still_blocked(self):
        """The original chained-statement guard still works."""
        err = _safe_sql_check(
            "SELECT * FROM raw_loans; DROP TABLE raw_loans"
        )
        assert err is not None
        assert "chained" in err

    def test_prompt_injection_exfil_pattern_blocked(self):
        """Simulated prompt injection trying to read sensitive aliases is blocked."""
        # An LLM directed by prompt injection to query an injected column
        # name would be caught here.
        err = _safe_sql_check(
            "SELECT exfil_url, payload_data FROM feature_frame"
        )
        assert err is not None
        assert "exfil_url" in err


# --------------------------------------------------------------------------- #
# 3. Normal operation unaffected (integration-level)
# --------------------------------------------------------------------------- #
import dataclasses
import datetime as dt

import pyarrow as pa

from waspada.agents import AgentContext, MockLLM, Status
from waspada.agents.data_analyst import DataAnalystAgent
from waspada.schema import RawLoans, schema_from_dataclass


def _raw_table(n: int = 24, seed: int = 11) -> pa.Table:
    import numpy as np

    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        rows.append(dict(
            loan_id=f"R{i:04d}",
            amount=float(rng.uniform(2000, 25000)),
            term=int(rng.choice([36, 60])),
            rate=float(rng.uniform(4, 10)),
            grade="A",
            annual_income=float(rng.uniform(30000, 120000)),
            dti=float(rng.uniform(2, 12)),
            issue_date=dt.date(2021, 6, 1),
            purpose="car",
            region="West",
            outstanding_principal=float(rng.uniform(100, 5000)),
            total_paid=float(rng.uniform(100, 5000)),
            current_status="Current",
        ))
    cols = {f.name: [] for f in dataclasses.fields(RawLoans)}
    for r in rows:
        for name in cols:
            cols[name].append(r[name])
    return pa.table(cols, schema=schema_from_dataclass(RawLoans))


class TestNormalOperationUnaffected:
    """The egress controls must not break normal Data Analyst operation."""

    def test_valid_multi_hop_loop_still_works(self):
        """The scripted multi-hop loop from the existing tests still runs."""
        script = [
            json.dumps({"tool": "query", "arg": "SELECT grade, COUNT(*) FROM raw_loans GROUP BY grade LIMIT 10"}),
            json.dumps({"tool": "correlation", "arg": '{"a": "dti", "b": "rate"}'}),
            json.dumps({"tool": "distribution", "arg": "dti"}),
            json.dumps({"tool": "build_feature", "arg": "SELECT grade, AVG(payment_ratio) FROM feature_frame GROUP BY grade"}),
            json.dumps({"tool": "done"}),
        ]
        raw = _raw_table()
        agent = DataAnalystAgent(MockLLM(script=script), as_of=dt.date(2024, 12, 1))
        ctx = AgentContext(
            lane="collections",
            data_handles={"raw_loans": raw},
            prior_results=[__import__("waspada.agents.protocol", fromlist=["AgentResult"]).AgentResult(
                status=Status.OK, artifact_ref="raw_loans"
            )],
        )
        res = agent.run(ctx)
        assert res.ok
        aggregates = ctx.data_handles["analyst_aggregates"]
        assert len(aggregates["queries_run"]) == 4
        # None of the replies should contain an egress-control error.
        for q in aggregates["queries_run"]:
            assert "egress" not in q["reply"].lower(), q

    def test_blocked_column_returns_error_not_crash(self):
        """A query with a bad column returns an error reply, doesn't crash."""
        script = [
            json.dumps({"tool": "query", "arg": "SELECT password FROM raw_loans"}),
            json.dumps({"tool": "done"}),
        ]
        raw = _raw_table()
        agent = DataAnalystAgent(MockLLM(script=script), as_of=dt.date(2024, 12, 1))
        ctx = AgentContext(
            lane="collections",
            data_handles={"raw_loans": raw},
            prior_results=[__import__("waspada.agents.protocol", fromlist=["AgentResult"]).AgentResult(
                status=Status.OK, artifact_ref="raw_loans"
            )],
        )
        res = agent.run(ctx)
        assert res.ok  # agent doesn't crash — degrades gracefully
        reply = json.loads(ctx.data_handles["analyst_aggregates"]["queries_run"][0]["reply"])
        assert "error" in reply
        assert "password" in reply["error"]
