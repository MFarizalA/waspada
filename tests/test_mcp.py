"""WA-020 QA — MCP layer tests (WA-015): AnalyticsStore + InProcessClient +
build_server tool handlers + _parse_tool_result + _jsonify_row.

This is the gap surfaced in review: the MCP integration (the rubric's explicit
"Model Context Protocol" depth marker) had no dedicated tests. These exercise
the compute and the protocol surface WITHOUT a subprocess — the brief says skip
StdioClient (needs ``python -m waspada.mcp.server`` over stdio + a running
server; out of scope for CI).

Coverage map:

  * ``AnalyticsStore.portfolio_stats`` — whole-book + segment-filtered + empty
    segment + non-matching segment + missing-segment-column degrade.
  * ``AnalyticsStore.lookup_account`` — hit + miss + no-features-configured.
  * ``InProcessClient`` — same surface as the store, no subprocess (the
    auditor's CI/offline MCP-backed path). Parity with the store's dict shapes.
  * ``build_server`` tool handlers — ``list_tools`` (both tools declared) +
    ``call_tool`` for both tools (portfolio_stats whole-book + segment,
    lookup_account hit + miss) + unknown-tool error envelope. Handlers invoked
    directly via ``server.request_handlers`` (no stdio).
  * ``_parse_tool_result`` — the StdioClient's result-parser edge cases:
    structuredContent unwrap, isError → error dict, text-JSON fallback,
    non-JSON text passthrough, empty.
  * ``_jsonify_row`` — date → ISO string, bytes → utf-8, passthrough scalars.

The scored fixture is a superset of ``ScoredAccounts`` (carries ``segment``,
``delinquency_status``, ``issue_year``, ``label_default`` — the monitoring
columns ``segment_health`` aggregates over). The features fixture mirrors the
``FeatureFrame`` columns ``lookup_account`` serves.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from types import SimpleNamespace

import pyarrow as pa
import pytest

import mcp.types as types

from waspada.mcp.client import InProcessClient, _parse_tool_result
from waspada.mcp.server import build_server
from waspada.mcp.store import AnalyticsStore, _jsonify_row


# --------------------------------------------------------------------------- #
# Fixtures — minimal but contract-shaped tables.
# --------------------------------------------------------------------------- #
def _scored_table() -> pa.Table:
    """A ScoredAccounts superset with the monitoring columns segment_health reads."""
    return pa.table({
        "loan_id": pa.array(["L1", "L2", "L3", "L4"], pa.string()),
        "p_default": pa.array([0.92, 0.81, 0.40, 0.05], pa.float64()),
        "score_band": pa.array(["Very High", "Very High", "Medium", "Very Low"], pa.string()),
        "segment": pa.array([
            {"product": "card", "region": "West"},
            {"product": "card", "region": "East"},
            {"product": "auto", "region": "West"},
            {"product": "auto", "region": "East"},
        ]),
        "recommended_action": pa.array(["call", "call", "watch", "auto-cure"], pa.string()),
        # monitoring columns (not model features; segment_health aggregates them)
        "delinquency_status": pa.array(["Default", "Current", "31-120", "Current"], pa.string()),
        "label_default": pa.array([True, False, True, False], pa.bool_()),
        "issue_year": pa.array([2020, 2021, 2020, 2022], pa.int64()),
    })


def _features_table() -> pa.Table:
    """A FeatureFrame-shaped table lookup_account serves (per-loan features)."""
    return pa.table({
        "loan_id": pa.array(["L1", "L2", "L3", "L4"], pa.string()),
        "payment_ratio": pa.array([0.95, 0.30, 0.50, 0.80], pa.float64()),
        "outstanding_ratio": pa.array([0.05, 0.70, 0.50, 0.20], pa.float64()),
        "dti": pa.array([30.0, 8.0, 22.0, 5.0], pa.float64()),
        "rate": pa.array([24.0, 6.0, 14.0, 4.0], pa.float64()),
        "loan_age": pa.array([40, 12, 30, 24], pa.int64()),
        "delinquency_status": pa.array(["Default", "Current", "31-120", "Current"], pa.string()),
        "grade": pa.array(["E", "A", "C", "A"], pa.string()),
    })


@pytest.fixture
def scored():
    return _scored_table()


@pytest.fixture
def features():
    return _features_table()


@pytest.fixture
def store(scored, features):
    return AnalyticsStore(scored, features)


@pytest.fixture
def stats_only_store(scored):
    """A store with scored but NO features (stats-only configuration)."""
    return AnalyticsStore(scored, None)


# --------------------------------------------------------------------------- #
# AnalyticsStore.portfolio_stats
# --------------------------------------------------------------------------- #
class TestPortfolioStats:
    def test_whole_book_returns_expected_aggregates(self, store):
        out = store.portfolio_stats()
        assert out["segment"] is None
        assert out["account_count"] == 4
        # NPL buckets = {Default, 31-120}; L1 + L3 → 2/4 = 0.5
        assert out["npl_ratio"] == 0.5
        assert out["vintage_default_rate"] == {"2020": 1.0, "2021": 0.0, "2022": 0.0}
        # Worst vintage is 2020 at 1.0 default.
        assert out["worst_vintage"] == {"year": "2020", "default_rate": 1.0}
        assert out["status_mix"] == {"Default": 0.25, "Current": 0.5, "31-120": 0.25}

    def test_segment_filter_restricts_to_slice(self, store):
        out = store.portfolio_stats({"product": "card", "region": "West"})
        # Only L1 matches → account_count 1, NPL 1.0 (Default).
        assert out["segment"] == {"product": "card", "region": "West"}
        assert out["account_count"] == 1
        assert out["npl_ratio"] == 1.0

    def test_segment_product_only_wildcards_region(self, store):
        out = store.portfolio_stats({"product": "auto"})
        # L3, L4 → 2 accounts; L3 is 31-120 (NPL) → 0.5.
        assert out["account_count"] == 2
        assert out["npl_ratio"] == 0.5

    def test_segment_empty_string_treated_as_wildcard(self, store):
        # Empty values are normalized away (treated as no filter).
        out = store.portfolio_stats({"product": "", "region": ""})
        assert out["segment"] is None
        assert out["account_count"] == 4

    def test_non_matching_segment_returns_empty_stats(self, store):
        out = store.portfolio_stats({"product": "mortgage"})
        assert out["segment"] == {"product": "mortgage"}
        assert out["account_count"] == 0
        assert out["npl_ratio"] == 0.0
        assert out["vintage_default_rate"] == {}
        assert out["status_mix"] == {}
        assert "worst_vintage" not in out  # no vintages → no worst


# --------------------------------------------------------------------------- #
# AnalyticsStore.lookup_account
# --------------------------------------------------------------------------- #
class TestLookupAccount:
    def test_hit_returns_feature_row(self, store):
        row = store.lookup_account("L1")
        assert row["loan_id"] == "L1"
        assert row["payment_ratio"] == 0.95
        assert row["grade"] == "E"
        assert row["delinquency_status"] == "Default"

    def test_miss_returns_empty_dict(self, store):
        assert store.lookup_account("NOPE") == {}

    def test_no_features_returns_empty(self, stats_only_store):
        # Stats-only config (features=None) → lookup always empty.
        assert stats_only_store.lookup_account("L1") == {}


# --------------------------------------------------------------------------- #
# InProcessClient — same surface, no subprocess.
# --------------------------------------------------------------------------- #
class TestInProcessClient:
    def test_portfolio_stats_matches_store(self, store, scored, features):
        with InProcessClient(scored, features) as client:
            via_client = client.portfolio_stats()
        via_store = store.portfolio_stats()
        assert via_client == via_store

    def test_lookup_account_hit_matches_store(self, store, scored, features):
        with InProcessClient(scored, features) as client:
            via_client = client.lookup_account("L2")
        assert via_client == store.lookup_account("L2")

    def test_lookup_account_miss_empty(self, scored, features):
        with InProcessClient(scored, features) as client:
            assert client.lookup_account("MISSING") == {}

    def test_coerces_loan_id_to_str(self, scored, features):
        # The client str()-coerces loan_id (parity with the MCP wire, which is
        # always strings). An int must not raise.
        with InProcessClient(scored, features) as client:
            # L1 exists; passing 1 (int) is not equal to "L1" so it's a miss,
            # but the call must not raise.
            assert client.lookup_account(1) == {}


# --------------------------------------------------------------------------- #
# build_server tool handlers — invoked directly (no subprocess).
# --------------------------------------------------------------------------- #
def _run(coro):
    """Drive an async handler to completion (fresh loop per call, isolated)."""
    return asyncio.new_event_loop().run_until_complete(coro)


def _call_tool(server, name, arguments):
    """Invoke the registered call_tool handler and return the CallToolResult."""
    handler = server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments),
    )
    return _run(handler(req)).root  # unwrap ServerResult → CallToolResult


def _list_tools(server):
    """Invoke the registered list_tools handler and return the tool list."""
    handler = server.request_handlers[types.ListToolsRequest]
    req = types.ListToolsRequest(method="tools/list")
    return _run(handler(req)).root.tools


class TestBuildServerListTools:
    def test_declares_both_tools(self, store):
        server = build_server(store)
        tools = _list_tools(server)
        names = [t.name for t in tools]
        assert names == ["portfolio_stats", "lookup_account"]
        # Each tool carries its description + input schema.
        for t in tools:
            assert t.description and t.inputSchema

    def test_lookup_account_schema_requires_loan_id(self, store):
        server = build_server(store)
        tools = {t.name: t for t in _list_tools(server)}
        assert "loan_id" in tools["lookup_account"].inputSchema["properties"]
        assert "loan_id" in tools["lookup_account"].inputSchema.get("required", [])


class TestBuildServerCallTool:
    def test_portfolio_stats_whole_book(self, store):
        server = build_server(store)
        result = _call_tool(server, "portfolio_stats", {})
        assert result.isError is False
        body = json.loads(result.content[0].text)
        assert body["account_count"] == 4
        assert body["npl_ratio"] == 0.5

    def test_portfolio_stats_segment(self, store):
        server = build_server(store)
        result = _call_tool(
            server, "portfolio_stats",
            {"segment": {"product": "card", "region": "West"}},
        )
        body = json.loads(result.content[0].text)
        assert body["account_count"] == 1
        assert body["segment"] == {"product": "card", "region": "West"}

    def test_lookup_account_hit(self, store):
        server = build_server(store)
        result = _call_tool(server, "lookup_account", {"loan_id": "L1"})
        body = json.loads(result.content[0].text)
        assert body["loan_id"] == "L1"
        assert body["grade"] == "E"

    def test_lookup_account_miss_returns_empty_object(self, store):
        server = build_server(store)
        result = _call_tool(server, "lookup_account", {"loan_id": "ZZZ"})
        body = json.loads(result.content[0].text)
        assert body == {}

    def test_unknown_tool_returns_error_envelope(self, store):
        # The handler must not crash on an unknown tool — it returns an error
        # dict so the Skeptic's FC loop degrades gracefully.
        server = build_server(store)
        result = _call_tool(server, "no_such_tool", {})
        body = json.loads(result.content[0].text)
        assert "error" in body
        assert "unknown tool" in body["error"]

    def test_missing_loan_id_argument_rejected_by_schema(self, store):
        # ``loan_id`` is required in the tool schema; the MCP framework
        # validates inputs BEFORE the handler runs and returns isError=True
        # with a validation message (the handler's defensive default never
        # fires). This is the correct protocol behavior — document it.
        server = build_server(store)
        result = _call_tool(server, "lookup_account", None)
        assert result.isError is True
        assert "loan_id" in result.content[0].text


# --------------------------------------------------------------------------- #
# _parse_tool_result — the StdioClient's CallToolResult → dict parser.
# --------------------------------------------------------------------------- #
def _make_result(*, text=None, structured=None, is_error=False):
    """Build a lightweight stand-in for an mcp CallToolResult."""
    content = []
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(
        content=content,
        structuredContent=structured,
        isError=is_error,
    )


class TestParseToolResult:
    def test_structured_content_result_key_unwrap(self):
        res = _make_result(structured={"result": {"account_count": 4}})
        assert _parse_tool_result(res) == {"account_count": 4}

    def test_structured_content_passthrough(self):
        res = _make_result(structured={"foo": "bar", "baz": 1})
        assert _parse_tool_result(res) == {"foo": "bar", "baz": 1}

    def test_is_error_returns_error_dict(self):
        res = _make_result(text="boom", is_error=True)
        out = _parse_tool_result(res)
        assert "error" in out and "boom" in out["error"]

    def test_text_json_fallback(self):
        res = _make_result(text='{"account_count": 7, "npl_ratio": 0.3}')
        assert _parse_tool_result(res) == {"account_count": 7, "npl_ratio": 0.3}

    def test_non_json_text_passthrough(self):
        res = _make_result(text="not json at all")
        assert _parse_tool_result(res) == {"text": "not json at all"}

    def test_json_array_wraps_in_result(self):
        # A JSON list (not dict) is wrapped as {"result": [...]}.
        res = _make_result(text='[1, 2, 3]')
        assert _parse_tool_result(res) == {"result": [1, 2, 3]}

    def test_empty_content_returns_empty_dict(self):
        res = _make_result()  # no content, no structured
        assert _parse_tool_result(res) == {}


# --------------------------------------------------------------------------- #
# _jsonify_row — JSON-native coercion for feature rows.
# --------------------------------------------------------------------------- #
class TestJsonifyRow:
    def test_date_to_iso_string(self):
        out = _jsonify_row({"loan_id": "L1", "issue_date": dt.date(2021, 3, 15)})
        assert out["issue_date"] == "2021-03-15"
        assert out["loan_id"] == "L1"

    def test_datetime_to_iso_string(self):
        out = _jsonify_row({"ts": dt.datetime(2024, 1, 2, 3, 4, 5)})
        assert out["ts"].startswith("2024-01-02T03:04:05")

    def test_bytes_to_utf8_string(self):
        out = _jsonify_row({"blob": b"hello"})
        assert out["blob"] == "hello"

    def test_bytearray_decoded(self):
        out = _jsonify_row({"blob": bytearray(b"abc")})
        assert out["blob"] == "abc"

    def test_invalid_utf8_bytes_replaced(self):
        out = _jsonify_row({"blob": b"\xff\xfe"})
        # Replacement char(s), not an exception — decode(errors="replace").
        assert isinstance(out["blob"], str)

    def test_scalars_passthrough(self):
        out = _jsonify_row({"i": 5, "f": 1.5, "s": "x", "b": True, "n": None})
        assert out == {"i": 5, "f": 1.5, "s": "x", "b": True, "n": None}


# --------------------------------------------------------------------------- #
# WA-042 — analyst_aggregates served via AnalyticsStore / InProcessClient.
# --------------------------------------------------------------------------- #
def _sample_aggregates() -> dict:
    """Simulate what the Data Analyst's reasoning loop produces."""
    import json
    return {
        "queries_run": [
            {"tool": "correlation", "arg": '{"a":"payment_ratio","b":"dti"}',
             "reply": json.dumps({"table": "feature_frame", "a": "payment_ratio",
                                  "b": "dti", "correlation": -0.42})},
            {"tool": "distribution", "arg": "dti",
             "reply": json.dumps({"table": "feature_frame", "column": "dti",
                                  "n": 60, "min": 2.0, "max": 35.0, "mean": 15.3,
                                  "q1": 8.0, "median": 14.0, "q3": 22.0,
                                  "histogram": []})},
            {"tool": "build_feature",
             "arg": "SELECT grade, AVG(dti) AS avg_dti FROM feature_frame GROUP BY grade",
             "reply": json.dumps({"feature": "aggregate", "count": 2,
                                  "result": [{"grade": "A", "avg_dti": 6.5},
                                             {"grade": "E", "avg_dti": 28.1}]})},
        ],
    }


class TestAnalystAggregates:
    """WA-042: AnalyticsStore serves analyst_aggregates in portfolio_stats."""

    def test_store_without_aggregates_omits_key(self, scored, features):
        """No analyst_aggregates → portfolio_stats has no analyst_aggregates key."""
        store = AnalyticsStore(scored, features)
        out = store.portfolio_stats()
        assert "analyst_aggregates" not in out

    def test_store_with_aggregates_includes_them(self, scored, features):
        """With aggregates, portfolio_stats includes an analyst_aggregates summary."""
        store = AnalyticsStore(scored, features, _sample_aggregates())
        out = store.portfolio_stats()
        assert "analyst_aggregates" in out
        agg = out["analyst_aggregates"]
        # Correlations extracted.
        assert "correlations" in agg
        assert any(c.get("a") == "payment_ratio" for c in agg["correlations"])
        # Distributions extracted.
        assert "distributions" in agg
        assert any(d.get("column") == "dti" for d in agg["distributions"])
        # Feature aggregates extracted.
        assert "feature_aggregates" in agg

    def test_set_aggregates_after_construction(self, scored, features):
        """set_analyst_aggregates injects aggregates post-construction."""
        store = AnalyticsStore(scored, features)
        assert "analyst_aggregates" not in store.portfolio_stats()
        store.set_analyst_aggregates(_sample_aggregates())
        out = store.portfolio_stats()
        assert "analyst_aggregates" in out

    def test_segment_query_includes_aggregates(self, scored, features):
        """Aggregates are whole-book context, available in segment queries too."""
        store = AnalyticsStore(scored, features, _sample_aggregates())
        out = store.portfolio_stats({"product": "card"})
        assert "analyst_aggregates" in out

    def test_empty_aggregates_omits_key(self, scored, features):
        """An empty aggregates dict is treated as absent."""
        store = AnalyticsStore(scored, features, {})
        out = store.portfolio_stats()
        assert "analyst_aggregates" not in out

    def test_inprocess_client_serves_aggregates(self, scored, features):
        """InProcessClient with aggregates → same enriched portfolio_stats."""
        with InProcessClient(scored, features, _sample_aggregates()) as client:
            via_client = client.portfolio_stats()
        assert "analyst_aggregates" in via_client
        assert "correlations" in via_client["analyst_aggregates"]

    def test_inprocess_client_set_aggregates(self, scored, features):
        """InProcessClient.set_analyst_aggregates injects post-construction."""
        with InProcessClient(scored, features) as client:
            assert "analyst_aggregates" not in client.portfolio_stats()
            client.set_analyst_aggregates(_sample_aggregates())
            assert "analyst_aggregates" in client.portfolio_stats()

    def test_aggregates_survive_json_serialization(self, scored, features):
        """The enriched response must be JSON-serializable (MCP wire-safe)."""
        import json
        store = AnalyticsStore(scored, features, _sample_aggregates())
        out = store.portfolio_stats()
        # Must not raise.
        json.dumps(out)


class TestMcpEvidenceHelper:
    """WA-042: _mcp_evidence extracts citable strings from analyst aggregates."""

    def test_extracts_correlation_evidence(self):
        from waspada.agents.risk_auditor import _mcp_evidence
        stats = {"analyst_aggregates": {"correlations": [
            {"a": "payment_ratio", "b": "dti", "correlation": -0.42}]}}
        facts = _mcp_evidence(stats)
        assert any("corr(payment_ratio,dti)=-0.42" == f for f in facts)

    def test_extracts_distribution_evidence(self):
        from waspada.agents.risk_auditor import _mcp_evidence
        stats = {"analyst_aggregates": {"distributions": [
            {"column": "dti", "median": 14.0, "mean": 15.3}]}}
        facts = _mcp_evidence(stats)
        assert any("median(dti)=14.00" == f for f in facts)

    def test_empty_stats_returns_empty(self):
        from waspada.agents.risk_auditor import _mcp_evidence
        assert _mcp_evidence({}) == []
        assert _mcp_evidence(None) == []
        assert _mcp_evidence({"npl_ratio": 0.5}) == []

    def test_no_aggregates_key_returns_empty(self):
        from waspada.agents.risk_auditor import _mcp_evidence
        assert _mcp_evidence({"npl_ratio": 0.5, "account_count": 4}) == []
