"""Data Analyst agent (WA-030) — Tier-2 reasoning layer over analytics.

The deterministic feature-engineering step (WA-009) is promoted into a Tier-2
Data Analyst agent: a ``qwen3.7-plus`` function-calling loop that decides *which*
DuckDB SQL explorations to run on the freshly-loaded book. The deterministic
feature recipe (``build_features``) STAYS as the core — the agent adds reasoning
on top, it does NOT replace it.

Flow
----
1. **Build the FeatureFrame** via :func:`waspada.features.collections.build_features`
   unconditionally, first. This guarantees the frozen contract is satisfied
   regardless of what the LLM does afterwards.
2. **Build a Lakehouse** over the RawLoans (and the FeatureFrame) so the tools
   can run read-only SQL explorations.
3. **Function-calling loop**: the brain (``qwen3.7-plus`` in prod,
   :class:`~waspada.agents.llm.MockLLM` offline) is shown the table shapes +
   the registered exploration tools and picks which query/correlation/
   distribution/aggregate to run next, hop by hop, until it signals ``done`` or
   the hop budget runs out. Every hop is recorded as a
   :class:`~waspada.agents.protocol.Step`.
4. **Publish two handles**: the unchanged ``feature_frame`` (drop-in replacement
   for ``AnalyticsAgent``) and a new ``analyst_aggregates`` dict that backs the
   MCP evidence base.

The exploration tools (``query``, ``correlation``, ``distribution``,
``build_feature``) are registered via the same ``register_tool`` pattern as the
Data Engineer's quality tools — stubbable in tests. They are read-only
(SELECT-only; chained statements rejected) and never mutate the FeatureFrame.

Resilience: an unparsable tool step or unavailable brain never crashes the
pipeline; the deterministic FeatureFrame is always emitted and
``analyst_aggregates`` carries whatever partial work was done.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import dataclasses

import pyarrow as pa
import pyarrow.compute as pc

from ..data.lakehouse import Lakehouse
from ..features.collections import assert_no_nulls, build_features
from ..schema import ApplicationFeatureFrame, FeatureFrame
from .base import Agent
from .llm import LLM, MockLLM
from .protocol import AgentContext, AgentResult, Status

__all__ = ["DataAnalystAgent", "DEFAULT_EXPLORE_BUDGET"]

# Hop budget for the function-calling loop. Tight enough to terminate if the
# brain loops, generous enough for a few genuine explorations.
DEFAULT_EXPLORE_BUDGET = 8

# Exploration tools the brain may invoke. Keys are the names the LLM emits in
# its ``{"tool": "<name>", "arg": "..."}`` reply.
_TOOL_NAMES = ("query", "correlation", "distribution", "build_feature")

# WA-084: native Qwen function-calling schemas (OpenAI ``tools`` shape). Same
# native surface as the Risk Auditor / Data Engineer. A brain that returns no
# native tool_calls (scripted MockLLM) transparently falls back to the legacy
# prompt-parsed loop, so offline/tests are unchanged.
_ANALYST_TOOLS: List[Dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "query",
        "description": "Run a read-only DuckDB SQL query over raw_loans / feature_frame.",
        "parameters": {"type": "object", "properties": {
            "sql": {"type": "string", "description": "The SELECT statement to run."}},
            "required": ["sql"]}}},
    {"type": "function", "function": {
        "name": "correlation",
        "description": "Pearson correlation between two numeric columns.",
        "parameters": {"type": "object", "properties": {
            "a": {"type": "string", "description": "First column."},
            "b": {"type": "string", "description": "Second column."}},
            "required": ["a", "b"]}}},
    {"type": "function", "function": {
        "name": "distribution",
        "description": "Quantiles + histogram buckets for one numeric column.",
        "parameters": {"type": "object", "properties": {
            "column": {"type": "string", "description": "The column to summarise."}},
            "required": ["column"]}}},
    {"type": "function", "function": {
        "name": "build_feature",
        "description": "Run a SQL exploration over feature_frame to inform the debate's evidence base.",
        "parameters": {"type": "object", "properties": {
            "sql": {"type": "string", "description": "The SELECT statement over feature_frame."}},
            "required": ["sql"]}}},
]


def _native_arg(name, args):
    """Map native tool_call arguments to the single ``arg`` string _invoke_tool expects."""
    if name == "correlation":
        return json.dumps({"a": str(args.get("a", "")), "b": str(args.get("b", ""))})
    return str(args.get("sql") or args.get("column") or args.get("arg") or "")


class DataAnalystAgent(Agent):
    """Analytics promoted to a Tier-2 reasoning agent.

    Brain: ``qwen3.7-plus`` with native function calling in production; a
    :class:`MockLLM` offline (the framework runs end-to-end on the mock).
    """

    name = "data_analyst"
    role = "build the FeatureFrame, then explore the book via DuckDB SQL"

    def __init__(
        self,
        llm: Optional[Any] = None,
        *,
        as_of: Optional[dt.date] = None,
        explore_budget: int = DEFAULT_EXPLORE_BUDGET,
    ) -> None:
        super().__init__(llm=llm if llm is not None else MockLLM())
        self.as_of = as_of or dt.date(2024, 12, 1)
        self.explore_budget = int(explore_budget)
        # Exploration tools default to DuckDB-backed implementations; a caller
        # overrides per-tool with register_tool (tests inject deterministic stubs).
        for name, fn in _default_exploration_tools().items():
            self.register_tool(name, fn)
        # The Lakehouse built from this run's data (set during run()). Exposed
        # for audit/tests so a caller can inspect what the agent saw.
        self.lakehouse: Optional[Lakehouse] = None

    # -------------------------------------------------------------------- run
    def run(self, context: AgentContext) -> AgentResult:
        # ---- 1. Consume predecessor RawLoans handle ----
        if not context.prior_results:
            self.step("build_features", status=Status.ERROR, notes="no predecessor")
            return AgentResult(status=Status.ERROR, agent=self.name, notes="no RawLoans input")
        raw_handle = context.prior_results[-1].artifact_ref
        raw: Optional[pa.Table] = context.data_handles.get(raw_handle) if raw_handle else None
        if raw is None:
            self.step("build_features", status=Status.ERROR, notes=f"handle {raw_handle!r} missing")
            return AgentResult(
                status=Status.ERROR, agent=self.name,
                notes=f"RawLoans handle {raw_handle!r} not found",
            )

        # ---- 2. Deterministic FeatureFrame core (never replaced) ----
        # WA-038: the feature recipe + contract are selected by lane. The frame
        # is ALWAYS the deterministic builder's output (the WA-030 rule holds
        # per lane); the LLM loop only explores it.
        if context.lane == "origination":
            from ..features.origination import build_features as _build_features
            frame_contract = ApplicationFeatureFrame
        else:
            _build_features = build_features
            frame_contract = FeatureFrame
        self.step("build_features", notes=f"lane={context.lane} as_of={self.as_of.isoformat()} rows={raw.num_rows}")
        try:
            frame = _build_features(raw, self.as_of)
            assert_no_nulls(frame, frame_contract)
        except Exception as exc:
            self.step("build_features", status=Status.ERROR, notes=str(exc))
            return AgentResult(status=Status.ERROR, agent=self.name, notes=f"features failed: {exc}")
        if context.lane == "origination":
            # Alias the id so the debate machinery (auditor lookups, dispute
            # records, adjudication write-back) runs verbatim on either lane.
            # Additive superset — the contract stays untouched.
            frame = frame.append_column("loan_id", frame.column("application_id"))

        # Surface a null-rate summary (same acceptance as AnalyticsAgent).
        null_total = sum(
            int(pc.sum(pc.is_null(frame.column(f.name))).as_py())
            for f in dataclasses.fields(frame_contract)
        )
        self.step(
            "null_rate_check", notes=f"feature-null total = {null_total} (all contract fields non-null)",
        )

        # ---- 3. Build the Lakehouse the tools query ----
        self.lakehouse = _arrow_lakehouse(raw, "raw_loans", frame, "feature_frame")

        # ---- 4. Function-calling loop ----
        aggregates = self._reasoning_loop(raw, frame)

        # ---- 5. Publish both handles ----
        context.data_handles["feature_frame"] = frame
        context.data_handles["analyst_aggregates"] = aggregates

        return AgentResult(
            status=Status.OK, agent=self.name, artifact_ref="feature_frame",
            notes=f"built FeatureFrame ({frame.num_rows} rows, nulls={null_total}); "
                  f"aggregates={list(aggregates.keys())}",
        )

    # ---------------------------------------------------------- reasoning loop
    def _reasoning_loop(self, raw: pa.Table, frame: pa.Table) -> Dict[str, Any]:
        """Dispatch: native Qwen function-calling when the brain supports it,
        else the legacy prompt-parsed loop.

        WA-084: the Data Analyst now uses the SAME native function-calling surface
        as the Risk Auditor (``chat(tools=_ANALYST_TOOLS)`` + ``tool_calls``) when
        the brain emits native tool calls. A brain returning no native tool_calls
        (scripted MockLLM) transparently falls through to
        :meth:`_legacy_reasoning_loop`, so existing behaviour is unchanged.
        """
        if getattr(self.llm, "supports_native_tools", False):
            try:
                aggregates = self._native_reasoning_loop(raw, frame)
            except Exception as exc:  # native failed before engaging -> legacy
                self.step("da_native_error", status=Status.ERROR,
                          notes=f"native loop error, falling back: {exc}")
                aggregates = None
            if aggregates is not None:
                return aggregates
        return self._legacy_reasoning_loop(raw, frame)

    def _native_reasoning_loop(self, raw: pa.Table, frame: pa.Table) -> Optional[Dict[str, Any]]:
        """Native Qwen function-calling loop (WA-084).

        Returns the aggregates dict, or ``None`` if the brain emitted no native
        ``tool_calls`` on the first turn (signal to the dispatcher: use legacy).
        """
        aggregates: Dict[str, Any] = {"queries_run": []}
        seed = self._loop_prompt(raw, frame, [], None)
        messages: List[Dict[str, Any]] = [{"role": "user", "content": seed}]
        engaged = False
        for hop in range(self.explore_budget):
            resp = self.llm.chat(seed, tools=_ANALYST_TOOLS, messages=messages)
            if not resp.has_tool_calls:
                if not engaged:
                    return None  # not a native-tool brain -> legacy loop
                self.step("da_native_done",
                          notes=f"brain signalled done after {hop} native hop(s)")
                break
            engaged = True
            messages.append({
                "role": "assistant", "content": resp.content or "",
                "tool_calls": [
                    {"id": tc.id or f"call_{i}", "type": "function",
                     "function": {"name": tc.name, "arguments": tc.arguments}}
                    for i, tc in enumerate(resp.tool_calls)],
            })
            for tc in resp.tool_calls:
                if tc.name not in _TOOL_NAMES:
                    messages.append({"role": "tool", "tool_call_id": tc.id or "call_0",
                                     "content": json.dumps({"error": f"unknown tool {tc.name!r}"})})
                    continue
                arg = _native_arg(tc.name, tc.parsed_arguments())
                reply = self._invoke_tool(tc.name, arg)
                self.step(f"da_tool:{tc.name}", notes=f"native arg={arg!r} -> {reply[:160]}")
                aggregates["queries_run"].append({"tool": tc.name, "arg": arg, "reply": reply})
                messages.append({"role": "tool", "tool_call_id": tc.id or "call_0", "content": reply})
        else:
            self.step("da_native_budget",
                      notes=f"hit explore_budget={self.explore_budget} without done (native)")
        return aggregates

    def _legacy_reasoning_loop(self, raw: pa.Table, frame: pa.Table) -> Dict[str, Any]:
        """Run the qwen3.7-plus function-calling loop over the book.

        Each hop: the brain is shown the table shapes + the last tool reply and
        picks the next exploration (or signals ``done``). The loop terminates on
        ``done``, on the hop budget, or on an unparsable turn.

        Returns a dict of analyst aggregates (the ``analyst_aggregates`` handle).
        """
        aggregates: Dict[str, Any] = {"queries_run": []}
        history: List[Dict[str, str]] = []
        last_reply: Optional[str] = None

        for hop in range(self.explore_budget):
            prompt = self._loop_prompt(raw, frame, history, last_reply)
            try:
                raw_reply = self.llm.complete(prompt)
            except Exception as exc:  # brain unreachable → safe degrade
                self.step("da_loop", status=Status.ERROR,
                          notes=f"llm error on hop {hop}: {exc}; returning partial aggregates")
                return aggregates

            tool, arg = _parse_tool_call(raw_reply)
            self.step("da_think", notes=f"hop {hop} reply={raw_reply[:120]!r}")
            if tool == "done":
                self.step("da_done", notes=f"brain signalled done after {hop} hops")
                break
            if tool is None or tool not in _TOOL_NAMES:
                # Unparsable → stop the loop; deterministic frame is already safe.
                self.step("da_unparsable", status=Status.ERROR,
                          notes=f"unparsable tool call on hop {hop}: {raw_reply[:80]!r}")
                break

            # Invoke the chosen tool and feed the reply back into the next hop.
            reply = self._invoke_tool(tool, arg)
            self.step(f"da_tool:{tool}", notes=f"arg={arg!r} -> {reply[:160]}")
            aggregates["queries_run"].append({"tool": tool, "arg": arg, "reply": reply})
            history.append({"tool": tool, "arg": arg or "", "reply": reply})
            last_reply = reply
        else:
            self.step("da_budget_exhausted",
                      notes=f"hit explore_budget={self.explore_budget} without a done signal")

        return aggregates

    def _invoke_tool(self, tool: str, arg: Optional[str]) -> str:
        """Call a registered exploration tool and return its reply as a string."""
        fn = self.tools.get(tool)
        if fn is None:
            return json.dumps({"error": f"tool {tool!r} not registered"})
        try:
            out = fn(self.lakehouse, arg or "")
        except Exception as exc:  # pragma: no cover - defensive
            return json.dumps({"error": str(exc)})
        if isinstance(out, (dict, list)):
            return json.dumps(out)
        return str(out)

    # --------------------------------------------------------------- prompts
    def _loop_prompt(
        self,
        raw: pa.Table,
        frame: pa.Table,
        history: List[Dict[str, str]],
        last_reply: Optional[str],
    ) -> str:
        """Build the function-calling prompt for one hop."""
        raw_cols = [f.name for f in raw.schema]
        frame_cols = [f.name for f in frame.schema]
        lines = [
            "You are the Data Analyst (Tier-2 analytics reasoning).",
            f"The RawLoans snapshot has {raw.num_rows} rows. Columns: {raw_cols}.",
            f"The FeatureFrame has {frame.num_rows} rows. Columns: {frame_cols}.",
            "The deterministic FeatureFrame has already been built. Your job is to "
            "pick the NEXT read-only exploration to run, based on what you've seen "
            "so far, OR signal that you're done.",
            "",
            "Available tools (reply with ONLY a JSON object, no prose):",
            '  {"tool": "query", "arg": "SELECT grade, AVG(dti) FROM raw_loans GROUP BY grade LIMIT 200"}',
            '  {"tool": "correlation", "arg": "{\\"a\\": \\"payment_ratio\\", \\"b\\": \\"dti\\"}"}',
            '  {"tool": "distribution", "arg": "payment_ratio"}',
            '  {"tool": "build_feature", "arg": "SELECT grade, AVG(outstanding_ratio) AS median_outstanding FROM feature_frame GROUP BY grade"}',
            '  {"tool": "done"}              — you have explored enough; stop the loop',
            "",
            "Rules:",
            "  - query/build_feature must be SELECT-only, no semicolons.",
            "  - Two tables: 'feature_frame' (the debate's evidence base — has the",
            "    derived features payment_ratio, outstanding_ratio, loan_age, ...)",
            "    and 'raw_loans' (the raw snapshot — outstanding_principal, total_paid).",
            "  - correlation/distribution read 'feature_frame' by default; target the",
            '    raw snapshot with "raw_loans.<col>" (distribution) or a "table":',
            '    "raw_loans" key (correlation).',
            "",
        ]
        if history:
            lines.append("Explorations so far:")
            for h in history:
                lines.append(f"  - {h['tool']}({h['arg']}): {h['reply']}")
        else:
            lines.append("No explorations run yet — pick the most informative first query.")
        if last_reply:
            lines.append("")
            lines.append(f"Last tool reply: {last_reply}")
        lines.append("")
        lines.append("Reply with ONLY: {\"tool\": \"...\", \"arg\": \"...\"}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Default exploration tools — backed by the Lakehouse (DuckDB SQL), read-only.
# --------------------------------------------------------------------------- #
def _default_exploration_tools() -> Dict[str, Callable[..., Any]]:
    """Return the default exploration-tool callables (DuckDB-backed)."""
    return {
        "query": _tool_query,
        "correlation": _tool_correlation,
        "distribution": _tool_distribution,
        "build_feature": _tool_build_feature,
    }


_MAX_ROWS = 200

# The two tables the Lakehouse registers. ``correlation`` / ``distribution``
# default to the FeatureFrame — it is the debate's evidence base (it carries the
# derived columns the debate actually cites: payment_ratio, outstanding_ratio,
# loan_age, delinquency_status). ``raw_loans`` stays reachable via an explicit
# table override so raw-only columns (outstanding_principal, total_paid) are not
# lost.
_VALID_TABLES = ("raw_loans", "feature_frame")
_DEFAULT_TABLE = "feature_frame"

# WA-045: the column allowlist — the union of every column in the RawLoans and
# FeatureFrame contracts (``waspada.schema``). Any identifier in an LLM-composed
# SELECT that isn't a known column name, SQL keyword, or built-in function is
# rejected before the query reaches DuckDB. This stops a prompt-injection from
# reading arbitrary column aliases (and keeps the LLM honest about the contract).
_ALLOWED_COLUMNS: frozenset[str] = frozenset(
    # RawLoans fields
    {
        "loan_id", "amount", "term", "rate", "grade", "annual_income", "dti",
        "issue_date", "purpose", "region", "outstanding_principal",
        "total_paid", "current_status",
        # FeatureFrame-derived fields
        "loan_age", "payment_ratio", "outstanding_ratio",
        "delinquency_status", "label_default", "as_of_date",
    }
)

# SQL keywords / aggregate / window functions that commonly appear in a
# well-formed analytic SELECT and must NOT be treated as column names.
_SQL_NON_COLUMN_KEYWORDS: frozenset[str] = frozenset(
    {
        # Core clauses
        "select", "from", "where", "group", "by", "order", "having", "limit",
        "offset", "as", "and", "or", "not", "in", "is", "null", "like",
        "between", "case", "when", "then", "else", "end", "distinct",
        "union", "all", "asc", "desc", "join", "left", "right", "inner",
        "outer", "on", "using", "with", "over", "partition",
        # Aggregate functions (DuckDB)
        "count", "sum", "avg", "min", "max", "median", "stddev", "variance",
        "corr", "covar", "array_agg", "list", "string_agg", "approx_quantile",
        "first", "last", "any_value", "bool_and", "bool_or", "mode",
        # Numeric / math functions
        "cast", "round", "floor", "ceil", "ceiling", "abs", "sqrt", "pow",
        "power", "exp", "ln", "log", "log10", "log2", "sign", "trunc",
        "coalesce", "greatest", "least", "width_bucket", "range",
        # String functions
        "length", "len", "upper", "lower", "trim", "concat", "substring",
        "substr", "replace", "regexp_replace",
        # Date functions
        "extract", "date_part", "date_trunc", "year", "month", "day",
        # Boolean
        "true", "false",
        # Table names (valid references, not columns)
        "raw_loans", "feature_frame",
    }
)


def _resolve_table(name: Any) -> str:
    """Resolve a caller-supplied table name to a registered table.

    Unknown / empty names fall back to the FeatureFrame (evidence base) rather
    than erroring — the tool still returns a useful default answer.
    """
    t = str(name or "").strip()
    return t if t in _VALID_TABLES else _DEFAULT_TABLE


def _split_table_col(ref: str) -> Tuple[str, str]:
    """Resolve a ``table.column`` or bare ``column`` reference to ``(table, column)``.

    A bare column defaults to the FeatureFrame; a ``raw_loans.col`` prefix
    targets the raw snapshot. Quotes are stripped defensively.
    """
    ref = str(ref or "").strip().replace('"', "")
    if "." in ref:
        head, tail = ref.split(".", 1)
        if head.strip() in _VALID_TABLES:
            return head.strip(), tail.strip()
    return _DEFAULT_TABLE, ref


def _safe_sql_check(sql: str) -> Optional[str]:
    """Return an error string if ``sql`` is not a safe read-only query.

    WA-045: in addition to the existing SELECT-only / no-chained-statements
    guard, this now enforces a **column allowlist**. Every identifier in the
    query that is not a SQL keyword, built-in function, or known table name
    must be a column from the RawLoans / FeatureFrame contract. This prevents
    a prompt-injection from composing a query that reads or aliases arbitrary
    column names the LLM should never reference.
    """
    stripped = sql.strip()
    if not stripped.lower().startswith("select"):
        return "only SELECT statements are allowed"
    if ";" in stripped:
        return "chained statements are not allowed"

    # WA-045: column allowlist — extract identifiers and reject unknown ones.
    col_err = _check_column_allowlist(stripped)
    if col_err:
        return col_err

    return None


def _check_column_allowlist(sql: str) -> Optional[str]:
    """Validate that every identifier in ``sql`` is an allowed column or keyword.

    Extracts word-like tokens from the SQL, skips SQL keywords / functions /
    table names / numeric literals, and rejects any remaining identifier that
    isn't in the FeatureFrame / RawLoans column contract. This is deliberately
    conservative — it may reject exotic but valid SQL, but the Data Analyst's
    tools should only ever query the known feature set.

    WA-045: column aliases introduced via ``AS <name>`` inside the query (e.g.
    ``AVG(dti) AS avg_dti``) and subquery aliases (``FROM (...) sub``) are
    recognised as locally-defined names so they don't trip the allowlist.
    """
    # Collect locally-defined aliases: ``AS <ident>`` (column aliases) and
    # ``) <ident>`` trailing a subquery (table aliases). These are names the
    # query itself invents, so referencing them is safe.
    local_aliases: set[str] = set()
    # AS aliases — ``AS foo`` or ``AS "foo"`` (case-insensitive).
    for m in re.finditer(r'\bAS\s+(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))', sql, re.IGNORECASE):
        local_aliases.add((m.group(1) or m.group(2)).strip().lower())
    # Subquery table aliases — ``) sub`` at the end of a derived table.
    # Match a closing paren followed by whitespace and a bare identifier.
    for m in re.finditer(r'\)\s+([A-Za-z_][A-Za-z0-9_]*)', sql):
        local_aliases.add(m.group(1).strip().lower())

    # Tokenize: match identifiers (including dotted refs like table.col and
    # quoted identifiers like "col"). Numbers and strings are ignored.
    # Pattern: word chars, dots, and quoted identifiers.
    tokens = re.findall(r'"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*)', sql)

    unknown: list[str] = []
    for quoted, bare in tokens:
        ident = (quoted or bare).strip().lower()
        if not ident:
            continue
        # Skip the part before a dot (table name) — it was already captured as
        # a separate bare token by the regex, so a dotted ref like
        # ``raw_loans.dti`` yields two tokens: "raw_loans" (table) + "dti".
        if ident in _SQL_NON_COLUMN_KEYWORDS:
            continue
        if ident in _ALLOWED_COLUMNS:
            continue
        if ident in local_aliases:
            continue
        # Numeric-looking tokens (e.g. quantile params) are not identifiers.
        if ident.isdigit() or ident.replace(".", "", 1).isdigit():
            continue
        unknown.append(ident)

    if unknown:
        return (
            f"blocked: column(s) {sorted(set(unknown))!r} are not in the "
            f"FeatureFrame/RawLoans contract; query rejected by egress control"
        )
    return None


def _tool_query(lh: Lakehouse, arg: str, *_a: Any) -> Dict[str, Any]:
    """Run a bounded SELECT query composed by the LLM."""
    sql = (arg or "").strip()
    err = _safe_sql_check(sql)
    if err:
        return {"error": err}
    try:
        # Enforce a row cap.
        if "limit" not in sql.lower():
            sql = f"{sql} LIMIT {_MAX_ROWS}"
        rel = lh.con.execute(sql)
        cols = [desc[0] for desc in rel.description]
        rows = rel.fetchall()[:_MAX_ROWS]
        records = [_jsonify_row(cols, row) for row in rows]
        return {"sql": sql, "count": len(records), "rows": records}
    except Exception as exc:
        return {"sql": sql, "error": str(exc)}


def _tool_correlation(lh: Lakehouse, arg: str, *_a: Any) -> Dict[str, Any]:
    """Pearson correlation between two numeric columns.

    Both columns are read from one table (default ``feature_frame``); pass an
    optional ``"table"`` key to target ``raw_loans`` instead.
    """
    try:
        obj = json.loads(arg or "{}")
    except (ValueError, TypeError):
        return {"error": "correlation needs JSON arg {'a': col, 'b': col}"}
    a = str(obj.get("a", "")).replace('"', "")
    b = str(obj.get("b", "")).replace('"', "")
    if not a or not b:
        return {"error": "correlation needs JSON arg {'a': col, 'b': col}"}
    table = _resolve_table(obj.get("table"))
    try:
        r = lh.scalar(
            f'SELECT CORR(CAST("{a}" AS DOUBLE), CAST("{b}" AS DOUBLE)) FROM {table}'
        )
        return {"table": table, "a": a, "b": b, "correlation": r}
    except Exception as exc:
        return {"table": table, "a": a, "b": b, "error": str(exc)}


def _tool_distribution(lh: Lakehouse, arg: str, *_a: Any) -> Dict[str, Any]:
    """Quantiles/min/max/mean/histogram buckets for one column.

    The column reference is ``column`` (read from the FeatureFrame, the debate
    evidence base) or ``table.column`` to target ``raw_loans`` explicitly.
    """
    ref = (arg or "").strip()
    if not ref:
        return {"error": "distribution needs a column name (arg)"}
    table, qcol = _split_table_col(ref)
    if not qcol:
        return {"error": "distribution needs a column name (arg)"}
    try:
        row = lh.con.execute(
            f'SELECT COUNT(*), MIN("{qcol}"), MAX("{qcol}"), '
            f'AVG(CAST("{qcol}" AS DOUBLE)), '
            f'APPROX_QUANTILE(CAST("{qcol}" AS DOUBLE), 0.25), '
            f'APPROX_QUANTILE(CAST("{qcol}" AS DOUBLE), 0.50), '
            f'APPROX_QUANTILE(CAST("{qcol}" AS DOUBLE), 0.75) '
            f'FROM {table}'
        ).fetchone()
        n, lo, hi, mean, q1, q2, q3 = (
            (row + (None,) * 7)[:7] if row else (0, None, None, None, None, None, None)
        )
        hist: List[Dict[str, Any]] = []
        try:
            hist_rows = lh.con.execute(
                f'SELECT bucket, COUNT(*) FROM ('
                f'  SELECT WIDTH_BUCKET(CAST("{qcol}" AS DOUBLE), '
                f'    (SELECT MIN(CAST("{qcol}" AS DOUBLE)) FROM {table}), '
                f'    (SELECT MAX(CAST("{qcol}" AS DOUBLE)) FROM {table}), 10) AS bucket '
                f'  FROM {table}) GROUP BY bucket ORDER BY bucket'
            ).fetchall()
            hist = [{"bucket": b, "count": c} for b, c in hist_rows]
        except Exception:
            hist = []
        return {
            "table": table, "column": qcol, "n": n, "min": lo, "max": hi, "mean": mean,
            "q1": q1, "median": q2, "q3": q3, "histogram": hist,
        }
    except Exception as exc:
        return {"table": table, "column": qcol, "error": str(exc)}


def _tool_build_feature(lh: Lakehouse, arg: str, *_a: Any) -> Dict[str, Any]:
    """Return a read-only aggregate the debate may cite."""
    sql = (arg or "").strip()
    err = _safe_sql_check(sql)
    if err:
        return {"error": err}
    try:
        rel = lh.con.execute(sql)
        cols = [desc[0] for desc in rel.description]
        rows = rel.fetchall()[:_MAX_ROWS]
        records = [_jsonify_row(cols, row) for row in rows]
        return {"feature": "aggregate", "sql": sql, "count": len(records), "result": records}
    except Exception as exc:
        return {"sql": sql, "error": str(exc)}


# --------------------------------------------------------------------------- #
# Arrow -> Lakehouse helper (offline / test path; no dlt, no network).
# --------------------------------------------------------------------------- #
def _jsonify_row(cols: List[str], row: Tuple[Any, ...]) -> Dict[str, Any]:
    """Convert a DuckDB result row into a JSON-serialisable dict."""
    out: Dict[str, Any] = {}
    for c, v in zip(cols, row):
        if isinstance(v, dt.date):
            out[c] = v.isoformat()
        else:
            out[c] = v
    return out


def _arrow_lakehouse(
    raw: pa.Table, raw_name: str,
    frame: pa.Table, frame_name: str,
) -> Lakehouse:
    """Build an in-memory DuckDB Lakehouse from pyarrow Tables."""
    import duckdb  # lazy

    con = duckdb.connect(":memory:", read_only=False)
    con.register(raw_name, raw)
    con.register(frame_name, frame)
    return Lakehouse(con, table=raw_name)


# --------------------------------------------------------------------------- #
# Tool-call parsing — defensive (LLM output is a string).
# --------------------------------------------------------------------------- #
_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")


def _parse_tool_call(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse one function-calling reply → ``(tool, arg)``.

    Tolerates surrounding prose / ```json fences by extracting the first
    ``{...}`` blob. Returns ``(None, None)`` on any parse failure (caller
    stops the loop; deterministic frame is already safe).
    """
    if not raw or not raw.strip():
        return None, None
    text = raw.strip()
    m = _JSON_OBJ_RE.search(text)
    blob = m.group(0) if m else text
    try:
        obj = json.loads(blob)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(obj, dict):
        return None, None
    tool = str(obj.get("tool", "")).strip().lower()
    if not tool:
        return None, None
    arg = obj.get("arg")
    arg_s = str(arg).strip() if arg is not None else None
    return tool, (arg_s or None)
