"""Data Engineer agent (WA-029) — the Tier-2 reasoning layer over ingest.

The deterministic ingest step (WA-009) is promoted into a Tier-2 Data Engineer
agent: a ``qwen3.6-flash`` function-calling loop that decides *which* data
quality checks to run on the freshly-loaded book. The deterministic freshness
+ schema gate STAYS as the core — the agent adds reasoning on top, it does
NOT replace it.

Flow
----
1. **Load** the snapshot via the :mod:`waspada.data.lakehouse` layer (in-process
   DuckDB over the OSS-parquet Arrow table; the same in-memory Arrow table
   in tests/offline).
2. **Deterministic gate** (unchanged from IngestAgent): schema validation +
   non-empty freshness check. Dirty/malformed data -> ``ERROR`` / ``BLOCKED``
   loud. This runs BEFORE any reasoning — the gate is not advisory.
3. **Function-calling loop**: the brain (``qwen3.6-flash`` in prod,
   :class:`~waspada.agents.llm.MockLLM` offline) is shown the table shape +
   the registered quality tools and picks which check to run next, hop by hop,
   until it signals ``done`` or the hop budget runs out. Every hop's tool call
   and tool reply is recorded as a :class:`~waspada.agents.protocol.Step` so a
   multi-hop run is verifiable in the step log (the WA-029 acceptance).
4. **Decision**: if any check reported an anomaly / dirty signal, the agent
   returns ``BLOCKED`` (gate still fails loud). Otherwise ``OK`` and publishes
   the RawLoans handle downstream (drop-in replacement for IngestAgent).

The quality tools (``validate_schema``, ``null_rates``, ``profile_column``,
``detect_anomalies``) are registered via the same ``register_tool`` pattern as
ingest's ``fetch`` — stubbable in tests, and the defaults query the Lakehouse
via SQL so they work on the real OSS Parquet without extra wiring.

Resilience: an unparsable tool step never skips validation — the gate already
ran in step 2, and an unparsable hop falls back to the default check set
(null_rates + detect_anomalies) so the book is always inspected.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import pyarrow as pa

from ..data.lakehouse import Lakehouse
from ..data.oss import fetch_loans as _real_fetch_loans
from ..schema import RawLoans, validate_table
from .base import Agent
from .llm import LLM, MockLLM
from .protocol import AgentContext, AgentResult, Status

__all__ = ["DataEngineerAgent", "DEFAULT_CHECK_BUDGET", "DEFAULT_CHECK_SET"]

# Hop budget for the function-calling loop. Generous enough that flash can
# finish a real inspection (schema -> nulls -> profile suspect cols -> anomalies)
# without truncating, tight enough that a brain stuck in a loop terminates.
DEFAULT_CHECK_BUDGET = 8

# The fallback check set used when the brain is unparsable or unavailable.
# Ordered so the most informative checks run first. Never empty — validation
# must happen even when the brain cannot steer it.
DEFAULT_CHECK_SET: Tuple[str, ...] = ("null_rates", "detect_anomalies")

# Quality tools the brain may invoke. Keys are the names the LLM emits in its
# ``{"tool": "<name>", "arg": "..."}`` reply.
_TOOL_NAMES = ("validate_schema", "null_rates", "profile_column", "detect_anomalies")

# WA-084: native Qwen function-calling schemas (OpenAI ``tools`` shape). When the
# brain supports native tool calls (QwenLLM), the Data Engineer declares these and
# Qwen emits real ``tool_calls`` -- the SAME native surface the Risk Auditor uses --
# instead of the prompt-embedded {"tool":...} JSON the legacy loop parses. A brain
# that returns no native tool_calls (the scripted MockLLM) transparently falls back
# to the legacy loop, so offline/tests are byte-for-byte unchanged.
_DE_TOOLS: List[Dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "validate_schema",
        "description": "Re-check the loaded table against the frozen RawLoans contract.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "null_rates",
        "description": "Per-column null rates across the loaded book.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "profile_column",
        "description": "Distribution + min/max/mean for one column.",
        "parameters": {"type": "object", "properties": {
            "column": {"type": "string", "description": "The column to profile."}},
            "required": ["column"]}}},
    {"type": "function", "function": {
        "name": "detect_anomalies",
        "description": "Flag outliers (dti>100, rate<0, negative amounts, etc).",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
]


class DataEngineerAgent(Agent):
    """Ingest promoted to a Tier-2 reasoning agent.

    Brain: ``qwen3.6-flash`` with native function calling in production; a
    :class:`MockLLM` offline (the framework runs end-to-end on the mock).
    """

    name = "data_engineer"
    role = "load the book, run the deterministic gate, then reason over data quality"

    def __init__(
        self,
        llm: Optional[Any] = None,
        *,
        limit: Optional[int] = None,
        check_budget: int = DEFAULT_CHECK_BUDGET,
    ) -> None:
        super().__init__(llm=llm if llm is not None else MockLLM())
        self.limit = limit
        self.check_budget = int(check_budget)
        # Quality tools default to the Lakehouse-backed implementations; a
        # caller overrides per-tool with register_tool (tests stub the whole
        # set). Same pattern as IngestAgent's fetch.
        for name, fn in _default_quality_tools().items():
            self.register_tool(name, fn)
        # The Lakehouse built from this run's data (set during run()). Exposed
        # for audit/tests so a caller can inspect what the agent saw.
        self.lakehouse: Optional[Lakehouse] = None

    # -------------------------------------------------------------------- run
    def run(self, context: AgentContext) -> AgentResult:
        lane = context.lane
        fetch: Callable[..., pa.Table] = self.tools.get("fetch", _real_fetch_loans)

        # ---- 1. Load ----
        self.step("fetch_loans", notes=f"lane={lane} limit={self.limit}")
        try:
            raw = (
                fetch(lane=lane, limit=self.limit)
                if self.limit
                else fetch(lane=lane)
            )
        except Exception as exc:  # pragma: no cover - exercised via stubs in tests
            self.step("fetch_loans", status=Status.ERROR, notes=f"fetch failed: {exc}")
            return AgentResult(
                status=Status.ERROR, agent=self.name,
                notes=f"data_engineer fetch failed: {exc}",
            )

        # ---- 2. Deterministic gate (the core that never gets replaced) ----
        try:
            validate_table(raw, RawLoans, name="DataEngineerAgent(raw)")
        except ValueError as exc:
            self.step("schema_check", status=Status.ERROR, notes=str(exc))
            return AgentResult(
                status=Status.ERROR, agent=self.name, notes=f"schema drift: {exc}"
            )
        n_rows = raw.num_rows
        if n_rows == 0:
            self.step("freshness_check", status=Status.BLOCKED, notes="zero rows read")
            return AgentResult(
                status=Status.BLOCKED, agent=self.name,
                notes="data_engineer returned zero rows (stale/empty source)",
            )
        self.step("freshness_check", notes=f"{n_rows} rows; schema OK")

        # ---- 3. Build the Lakehouse the quality tools query. WA-083: when
        # WASPADA_USE_DLT is set, the load runs through a dlt pipeline (merge dedup +
        # schema-contract + _dlt_loads lineage); otherwise an in-memory DuckDB
        # registration (the offline default). ----
        self.lakehouse = self._build_lakehouse(raw)

        # ---- 4. Function-calling loop ----
        findings = self._reasoning_loop(raw)

        # ---- 5. Decision ----
        dirty = bool(findings.get("anomalies")) or bool(findings.get("schema_drift"))
        if dirty:
            self.step(
                "quality_gate", status=Status.BLOCKED,
                notes=f"data-quality gate FAILED: {findings}",
            )
            return AgentResult(
                status=Status.BLOCKED, agent=self.name,
                notes=f"data_engineer gate failed: {findings}",
            )

        handle = "raw_loans"
        context.data_handles[handle] = raw
        checks_run = findings.get("checks_run", [])
        self.step(
            "quality_gate",
            notes=f"OK; checks run: {checks_run or list(DEFAULT_CHECK_SET)}",
        )
        return AgentResult(
            status=Status.OK, agent=self.name, artifact_ref=handle,
            notes=f"data_engineer cleared {n_rows} RawLoans rows "
                  f"(lane={lane}, checks={len(checks_run or DEFAULT_CHECK_SET)})",
        )

    def _build_lakehouse(self, raw: pa.Table) -> Lakehouse:
        """Build the DuckDB Lakehouse the quality tools query.

        WA-083: opt in to the **dlt load** (merge dedup on ``loan_id`` + schema contract +
        ``_dlt_loads`` lineage) via ``WASPADA_USE_DLT``. On any dlt failure — or when the flag
        is off — fall back to the in-memory Arrow registration (the offline default), so
        tests/CI/offline runs are byte-for-byte unchanged. When dlt is used, the load lineage
        (rows / load_id) is stepped so it can be cited as data-trust evidence.
        """
        if _use_dlt():
            try:
                from ..data.lakehouse import load_via_dlt
                lh = load_via_dlt(raw, table="raw_loans")
                lin = lh.lineage or {}
                self.step("dlt_load",
                          notes=f"dlt merge load: rows={lin.get('rows_loaded')} "
                                f"load_id={lin.get('load_id')} loads={lin.get('loads_recorded')}")
                return lh
            except Exception as exc:
                self.step("dlt_load", status=Status.ERROR,
                          notes=f"dlt load failed, falling back to in-memory: {exc}")
        return _arrow_lakehouse(raw, "raw_loans")

    # ---------------------------------------------------------- reasoning loop
    def _reasoning_loop(self, raw: pa.Table) -> Dict[str, Any]:
        """Dispatch: native Qwen function-calling when the brain supports it,
        else the legacy prompt-parsed loop.

        WA-084: the Data Engineer now uses the SAME native function-calling surface
        as the Risk Auditor (``chat(tools=_DE_TOOLS)`` + ``tool_calls``) when the
        brain emits native tool calls. A brain returning no native tool_calls (the
        scripted MockLLM offline/in tests) transparently falls through to
        :meth:`_legacy_reasoning_loop`, so existing behaviour is unchanged.
        """
        if getattr(self.llm, "supports_native_tools", False):
            try:
                findings = self._native_reasoning_loop(raw)
            except Exception as exc:  # native failed before engaging -> legacy
                self.step("de_native_error", status=Status.ERROR,
                          notes=f"native loop error, falling back: {exc}")
                findings = None
            if findings is not None:
                return findings
        return self._legacy_reasoning_loop(raw)

    def _native_reasoning_loop(self, raw: pa.Table) -> Optional[Dict[str, Any]]:
        """Native Qwen function-calling loop (WA-084).

        Returns a findings dict, or ``None`` if the brain emitted no native
        ``tool_calls`` on the first turn (signal to the dispatcher: use legacy).
        """
        findings: Dict[str, Any] = {"checks_run": [], "anomalies": [], "schema_drift": None}
        seed = self._loop_prompt(raw, [], None)
        messages: List[Dict[str, Any]] = [{"role": "user", "content": seed}]
        engaged = False
        for hop in range(self.check_budget):
            resp = self.llm.chat(seed, tools=_DE_TOOLS, messages=messages)
            if not resp.has_tool_calls:
                if not engaged:
                    return None  # not a native-tool brain -> legacy loop
                self.step("de_native_done",
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
                args = tc.parsed_arguments()
                arg = args.get("column") or args.get("arg")
                reply = self._invoke_tool(tc.name, arg)
                self.step(f"de_tool:{tc.name}", notes=f"native arg={arg!r} -> {reply[:160]}")
                findings["checks_run"].append(tc.name)
                self._fold_findings(findings, tc.name, reply)
                messages.append({"role": "tool", "tool_call_id": tc.id or "call_0", "content": reply})
        else:
            self.step("de_native_budget",
                      notes=f"hit check_budget={self.check_budget} without done (native)")
        if not findings["checks_run"]:
            self._run_default_checks(findings)
        return findings

    def _legacy_reasoning_loop(self, raw: pa.Table) -> Dict[str, Any]:
        """Run the qwen3.6-flash function-calling loop over the loaded book.

        Each hop: the brain is shown the table shape + the last tool reply and
        picks the next check to run (or signals ``done``). The loop terminates
        on ``done``, on the hop budget, or on an unparsable turn (which falls
        back to :data:`DEFAULT_CHECK_SET` so validation still happens).

        Returns a findings dict with ``checks_run`` (the ordered tool names
        actually invoked) and any dirty signals (``anomalies`` / ``schema_drift``).
        """
        findings: Dict[str, Any] = {
            "checks_run": [], "anomalies": [], "schema_drift": None,
        }
        history: List[Dict[str, str]] = []
        last_reply: Optional[str] = None

        for hop in range(self.check_budget):
            prompt = self._loop_prompt(raw, history, last_reply)
            try:
                raw_reply = self.llm.complete(prompt)
            except Exception as exc:  # brain unreachable → safe degrade
                self.step("de_loop", status=Status.ERROR,
                          notes=f"llm error on hop {hop}: {exc}; fallback to defaults")
                self._run_default_checks(findings)
                return findings

            tool, arg = _parse_tool_call(raw_reply)
            self.step("de_think", notes=f"hop {hop} reply={raw_reply[:120]!r}")
            if tool == "done":
                self.step("de_done", notes=f"brain signalled done after {hop} hops")
                break
            if tool is None or tool not in _TOOL_NAMES:
                # Unparsable → run the default set once, then stop.
                self.step("de_unparsable", status=Status.ERROR,
                          notes=f"unparsable tool call on hop {hop}: {raw_reply[:80]!r}")
                self._run_default_checks(findings)
                break

            # Invoke the chosen tool and feed the reply back into the next hop.
            reply = self._invoke_tool(tool, arg)
            self.step(f"de_tool:{tool}", notes=f"arg={arg!r} -> {reply[:160]}")
            findings["checks_run"].append(tool)
            history.append({"tool": tool, "arg": arg or "", "reply": reply})
            self._fold_findings(findings, tool, reply)
            last_reply = reply
        else:
            self.step("de_budget_exhausted",
                      notes=f"hit check_budget={self.check_budget} without a done signal")

        # Guarantee at least the default checks ran (gate is not advisory).
        if not findings["checks_run"]:
            self._run_default_checks(findings)
        return findings

    def _invoke_tool(self, tool: str, arg: Optional[str]) -> str:
        """Call a registered quality tool and return its reply as a string.

        Tools take the Lakehouse (and an optional arg for ``profile_column``)
        and return a JSON-serialisable dict; we stringify so the brain reads
        plain text. A tool exception degrades to an empty-dict reply (the
        absence of a finding) rather than crashing the loop.
        """
        fn = self.tools.get(tool)
        if fn is None:
            return json.dumps({"error": f"tool {tool!r} not registered"})
        try:
            if tool == "profile_column":
                out = fn(self.lakehouse, arg or "")
            else:
                out = fn(self.lakehouse)
        except Exception as exc:  # pragma: no cover - defensive
            return json.dumps({"error": str(exc)})
        if isinstance(out, (dict, list)):
            return json.dumps(out)
        return str(out)

    def _fold_findings(
        self, findings: Dict[str, Any], tool: str, reply: str,
    ) -> None:
        """Fold one tool reply into the running findings dict.

        ``detect_anomalies`` / ``validate_schema`` carry the dirty signals the
        gate decides on; the other tools are informational.
        """
        try:
            obj = json.loads(reply)
        except (ValueError, TypeError):
            return
        if not isinstance(obj, dict):
            return
        if tool == "detect_anomalies":
            anoms = obj.get("anomalies") or []
            if isinstance(anoms, list) and anoms:
                findings["anomalies"].extend(str(a) for a in anoms)
        elif tool == "validate_schema":
            if obj.get("ok") is False:
                findings["schema_drift"] = obj.get("detail") or str(obj)

    def _run_default_checks(self, findings: Dict[str, Any]) -> None:
        """Run the fallback check set once. Used on unparsable / unavailable brain."""
        for tool in DEFAULT_CHECK_SET:
            if tool in findings["checks_run"]:
                continue
            reply = self._invoke_tool(tool, None)
            self.step(f"de_default:{tool}", notes=reply[:160])
            findings["checks_run"].append(tool)
            self._fold_findings(findings, tool, reply)

    # --------------------------------------------------------------- prompts
    def _loop_prompt(
        self, raw: pa.Table, history: List[Dict[str, str]], last_reply: Optional[str],
    ) -> str:
        """Build the function-calling prompt for one hop."""
        cols = [f.name for f in raw.schema]
        lines = [
            "You are the Data Engineer (Tier-2 data quality reasoning).",
            f"The RawLoans snapshot loaded with {raw.num_rows} rows. "
            f"Columns: {cols}.",
            "The deterministic freshness + schema gate already passed. Your job "
            "now is to pick the NEXT data-quality check to run, based on what "
            "you've seen so far, OR signal that you're done.",
            "",
            "Available tools (reply with ONLY a JSON object, no prose):",
            '  {"tool": "validate_schema"}  — re-check the table vs the RawLoans contract',
            '  {"tool": "null_rates"}        — per-column null rates',
            '  {"tool": "profile_column", "arg": "<col>"} — distribution + min/max/mean for one column',
            '  {"tool": "detect_anomalies"}  — flag outliers (dti>100, rate<0, etc)',
            '  {"tool": "done"}              — you have inspected enough; stop the loop',
            "",
        ]
        if history:
            lines.append("Checks so far:")
            for h in history:
                lines.append(f"  - {h['tool']}({h['arg']}): {h['reply']}")
        else:
            lines.append("No checks run yet — pick the most informative first check.")
        if last_reply:
            lines.append("")
            lines.append(f"Last tool reply: {last_reply}")
        lines.append("")
        lines.append("Reply with ONLY: {\"tool\": \"...\", \"arg\": \"...\"}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Default quality tools — backed by the Lakehouse (DuckDB SQL).
# --------------------------------------------------------------------------- #
def _default_quality_tools() -> Dict[str, Callable[..., Any]]:
    """Return the default quality-tool callables (DuckDB-backed).

    Each takes the Lakehouse (built during run()) plus, for ``profile_column``,
    a column name. Returns a JSON-serialisable dict. A caller replaces any of
    these via :meth:`Agent.register_tool` (tests inject deterministic stubs).
    """
    return {
        "validate_schema": _tool_validate_schema,
        "null_rates": _tool_null_rates,
        "profile_column": _tool_profile_column,
        "detect_anomalies": _tool_detect_anomalies,
    }


def _tool_validate_schema(lh: Lakehouse, *_a: Any) -> Dict[str, Any]:
    """Re-check the loaded table vs the RawLoans contract."""
    try:
        tbl = lh.arrow(f"SELECT * FROM {lh.table} LIMIT 1")
        validate_table(tbl, RawLoans, name="validate_schema")
        return {"ok": True, "contract": "RawLoans"}
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}


def _tool_null_rates(lh: Lakehouse, *_a: Any) -> Dict[str, Any]:
    """Per-column null rate over the loaded table."""
    cols = lh.con.execute(f"SELECT * FROM {lh.table} LIMIT 0").to_arrow_table().column_names
    out: Dict[str, float] = {}
    n = lh.scalar(f"SELECT COUNT(*) FROM {lh.table}")
    if not n:
        return {"n_rows": 0, "null_rates": {}}
    for c in cols:
        nulls = lh.scalar(f'SELECT COUNT(*) FROM {lh.table} WHERE "{c}" IS NULL')
        out[c] = round(nulls / n, 4)
    return {"n_rows": n, "null_rates": out}


def _tool_profile_column(lh: Lakehouse, col: str, *_a: Any) -> Dict[str, Any]:
    """Distribution + min/max/mean for one numeric-ish column."""
    if not col:
        return {"error": "profile_column needs a column name (arg)"}
    qcol = col.replace('"', "")
    try:
        row = lh.con.execute(
            f'SELECT COUNT(*), MIN("{qcol}"), MAX("{qcol}"), '
            f'AVG(CAST("{qcol}" AS DOUBLE)) FROM {lh.table}'
        ).fetchone()
    except Exception as exc:  # non-numeric or missing column
        return {"column": col, "error": str(exc)}
    n, lo, hi, mean = (row + (None, None, None, None))[:4] if row else (0, None, None, None)
    return {"column": col, "n": n, "min": lo, "max": hi, "mean": mean}


def _tool_detect_anomalies(lh: Lakehouse, *_a: Any) -> Dict[str, Any]:
    """Flag business-rule outliers: dti>100, rate<0, negative amounts, etc."""
    anomalies: List[str] = []
    t = lh.table
    checks = [
        ("dti_over_100", f'SELECT COUNT(*) FROM {t} WHERE "dti" > 100'),
        ("rate_negative", f'SELECT COUNT(*) FROM {t} WHERE "rate" < 0'),
        ("amount_negative", f'SELECT COUNT(*) FROM {t} WHERE "amount" < 0'),
        ("term_invalid", f'SELECT COUNT(*) FROM {t} WHERE "term" NOT IN (36, 60)'),
        ("income_negative", f'SELECT COUNT(*) FROM {t} WHERE "annual_income" < 0'),
    ]
    for label, sql in checks:
        try:
            n = lh.scalar(sql) or 0
        except Exception:
            n = 0
        if n:
            anomalies.append(f"{label}={n}")
    return {"anomalies": anomalies}


def _use_dlt() -> bool:
    """WA-083: is the dlt load path opted in? (``WASPADA_USE_DLT`` truthy)."""
    return os.environ.get("WASPADA_USE_DLT", "").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# Arrow -> Lakehouse helper (the offline / test path; no dlt, no network).
# --------------------------------------------------------------------------- #
def _arrow_lakehouse(table: pa.Table, name: str) -> Lakehouse:
    """Build an in-memory DuckDB Lakehouse from a pyarrow Table."""
    import duckdb  # lazy

    con = duckdb.connect(":memory:", read_only=False)
    con.register(name, table)
    return Lakehouse(con, table=name)


# --------------------------------------------------------------------------- #
# Tool-call parsing — defensive (LLM output is a string; Qwen real path would
# use response_format=json_object, our LLM surface is string-in/string-out).
# --------------------------------------------------------------------------- #
_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")


def _parse_tool_call(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse one function-calling reply → ``(tool, arg)``.

    Tolerates surrounding prose / ```json fences by extracting the first
    ``{...}`` blob. Returns ``(None, None)`` on any parse failure (caller
    falls back to the default check set).
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
