"""Risk-auditor agent (WA-014 + WA-049) — the Skeptic.

After the classical-ML Actuary (:class:`~waspada.agents.risk_model.RiskModelAgent`)
scores 100% of the book, the Skeptic audits a **stratified slice of K accounts**
(K=8 default) and, where its independent view diverges from the Actuary's band,
opens a :class:`~waspada.agents.protocol.Dispute` with a Round-1 challenge round.

WA-049 changed *which* K. The audit used to be top-K by ``p_default`` — only the
accounts the model had already flagged — which meant the Skeptic could only ever
review candidate **false positives** (a wasted collector call: cheap) and never
**false negatives** (a "Very Low" account that rolls to NPL: the loss the product
exists to prevent). The slice is now stratified across ``riskiest`` /
``boundary`` / ``contradictory`` (see :func:`_select_audit_slice`), at the same K
and therefore the same LLM-call ceiling.

This is Round 1 only (challenge). Rebuttal (Round 2) + Arbiter (Round 3) land in
WA-016; an opened dispute therefore carries an *open* resolution here (``""``)
and the orchestrator routes the run to the gate action ``resolve_risk_dispute``.

Brain: Qwen (``qwen3.6-flash`` + native function calling) in production; the
framework runs offline on a :class:`~waspada.agents.llm.MockLLM`. Every claim
must cite evidence (HACKATHON.md § debate protocol) — the agent gathers the
account's feature context + portfolio stats via local stub tools (the real MCP
tools arrive in WA-015) and the LLM's JSON-mode reply carries the citation.

Admissibility rule (when does the Skeptic open a dispute?)
-----------------------------------------------------------
Both the Actuary's band and the Skeptic's view map onto a common 5-point risk
ordinal; a dispute is admissible when they differ by **≥ 2** (matches the
``bands agree (< 2 apart)`` gate in HACKATHON.md's debate sequence diagram):

    Actuary band ordinal:  Very Low=1 Low=2 Medium=3 High=4 Very High=5
    Skeptic view ordinal:  Low=1      Medium=3      High=5

So ``Very High + Low/Medium`` → dispute, ``Very High + High`` → no dispute (the
examples in the WA-014 brief). Symmetric on the low end
(``Very Low + High`` → dispute).
"""
from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pyarrow as pa

from ..model.risk import explain as _explain, format_drivers as _format_drivers
from ..schema import RISK_LEVELS
from .base import Agent
from .llm import ChatResponse, LLM, MockLLM, ToolCall
from .protocol import AgentContext, AgentResult, Dispute, DisputeRound, Status

__all__ = ["RiskAuditorAgent"]


# Common 5-point risk ordinal. Band (Actuary) and view (Skeptic) both project
# onto this so a single gap rule decides admissibility. Band keys are the
# frozen waspada.schema.RISK_LEVELS vocabulary.
_BAND_ORDINAL: Dict[str, int] = {
    "Very Low": 1, "Low": 2, "Medium": 3, "High": 4, "Very High": 5,
}
_VIEW_ORDINAL: Dict[str, int] = {"low": 1, "medium": 3, "high": 5}
DISPUTE_GAP = 2  # |band_ordinal − view_ordinal| ≥ DISPUTE_GAP → dispute opened

# Valid auditor-view vocabulary (the Skeptic's independent read).
_VIEWS = ("Low", "Medium", "High")

# Maximum back-and-forth turns in the native tool-calling loop (WA-041).
# Bounds the audit: the Skeptic gets at most a few tool pulls before it must
# give its final verdict. Generous enough for portfolio_stats + lookup_account.
_MAX_TOOL_TURNS = 4


# --------------------------------------------------------------------------- #
# Native tool schemas (WA-041) — the OpenAI-compatible ``tools`` array passed
# to QwenLLM.chat(). Qwen decides when (and whether) to call these, not
# hard-wired Python. The results are fed back so the model's final answer is
# grounded in real evidence.
# --------------------------------------------------------------------------- #
_AUDITOR_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "portfolio_stats",
            "description": (
                "Get portfolio-level and segment-level statistics for the "
                "current loan book — NPL ratios, segment breakdowns. Call "
                "this to understand the portfolio context before judging a "
                "specific account."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "segment": {
                        "type": "object",
                        "description": (
                            "Optional segment filter (product, region). "
                            "Omit for book-wide stats."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_account",
            "description": (
                "Look up the feature details for a specific loan account — "
                "payment_ratio, outstanding_ratio, dti, rate, loan_age, "
                "grade, delinquency_status. Call this to get the evidence "
                "needed to form an independent risk view."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "loan_id": {
                        "type": "string",
                        "description": "The loan ID to look up.",
                    },
                },
                "required": ["loan_id"],
            },
        },
    },
]


class RiskAuditorAgent(Agent):
    """Audit the top-K riskiest accounts and open disputes where the Skeptic
    disagrees with the Actuary's band."""

    name = "risk_auditor"
    role = "audit top-K scores and open disputes"

    def __init__(self, llm: Optional[LLM] = None, *, k: int = 8, max_workers: int = 1,
                 dispute_gap: int = DISPUTE_GAP) -> None:
        super().__init__(llm=llm if llm is not None else MockLLM())
        self.k = k
        # WA-095: admissibility gap the human sets in the parameter matrix. Tighter
        # (1) opens more disputes; looser (3-4) opens fewer. Defaults to the module
        # constant so an un-configured auditor is unchanged.
        self.dispute_gap = int(dispute_gap)
        # WA-080: audit the K accounts concurrently when max_workers > 1. Each
        # account's audit is an independent LLM tool-loop (the dominant cost of a
        # live-Qwen run: up to K x _MAX_TOOL_TURNS sequential calls). Running them
        # in parallel collapses the audit wall-clock from the *sum* of K chains to
        # the *longest single* chain -- the change that lets a live debate finish
        # inside the FC invocation timeout instead of dying mid-audit.
        #
        # Default 1 = sequential = byte-for-byte the pre-WA-080 path. This is
        # deliberate: the scripted MockLLM every test injects is NOT thread-safe
        # (``_next_scripted`` mutates a shared cursor), so parallelism is opt-in
        # and only the live QwenLLM path (a genuinely thread-safe OpenAI client)
        # turns it on. See :meth:`_run_audits`.
        self.max_workers = max(1, int(max_workers))
        # Serializes the local evidence-tool calls (portfolio_stats /
        # lookup_account) so parallel audits stay correct for ANY tool backend --
        # the pyarrow stubs and the in-process MCP client are read-only and
        # already safe, but a stdio MCP client shares one pipe. The lock is held
        # only around the microsecond-scale local read, never around the LLM
        # call, so it costs nothing on the hot path.
        self._tool_lock = threading.Lock()
        # Local stub tools (the WA-015 MCP-backed tools attach via
        # :meth:`attach_mcp`). Same register_tool / tools.get pattern as
        # IngestAgent's ``fetch`` — the defaults here are computed from the
        # in-memory frame so the agent is self-contained offline; a caller
        # injects the WA-015 MCP-backed implementations by attaching an MCP
        # client (stdio subprocess or in-process). Until attached, the tools
        # read the in-memory tables directly (offline default).
        self.register_tool("portfolio_stats", _default_portfolio_stats)
        self.register_tool("lookup_account", _default_lookup_account)
        # The MCP client, once attached (WA-015). ``None`` keeps the local
        # stubs active. Set by :meth:`attach_mcp`; cleared on :meth:`detach_mcp`.
        self._mcp_client: Optional[Any] = None
        # WA-050: the fitted model published by the risk-model agent this run,
        # so the Skeptic's challenge can cite the model's OWN drivers rather than
        # guessing from raw values. Read from the ``risk_model`` handle in run();
        # ``None`` (standalone auditor / no handle) just yields a thinner prompt.
        self._run_model: Optional[dict] = None

    # ---------------------------------------------------- MCP wiring (WA-015)
    def attach_mcp(self, client: Any) -> "RiskAuditorAgent":
        """Bind the auditor's tools to an MCP client (the WA-015 path).

        After this the ``portfolio_stats`` / ``lookup_account`` tools route
        through the supplied client — an :class:`~waspada.mcp.client.StdioClient`
        (real protocol round-trip, the rubric's MCP integration) or an
        :class:`~waspada.mcp.client.InProcessClient` (same store, no subprocess).
        The client must expose ``portfolio_stats(segment=None)`` and
        ``lookup_account(loan_id)`` returning plain dicts. Returns ``self``
        for chaining; :meth:`detach_mcp` restores the local stubs.
        """
        self._mcp_client = client
        # Bind closures that translate the auditor's call signature
        # (table args) into the MCP client's dict-returning surface. The MCP
        # client owns the data (scored+features loaded in its store/server),
        # so the table args are ignored on this path — they're the in-memory
        # fallback the stubs use.
        self.register_tool("portfolio_stats", _make_mcp_portfolio_stats(client))
        self.register_tool("lookup_account", _make_mcp_lookup_account(client))
        return self

    def detach_mcp(self) -> None:
        """Restore the local in-memory stub tools (undo :meth:`attach_mcp`)."""
        self._mcp_client = None
        self.register_tool("portfolio_stats", _default_portfolio_stats)
        self.register_tool("lookup_account", _default_lookup_account)

    @property
    def mcp_client(self) -> Optional[Any]:
        """The attached MCP client (``None`` when running on local stubs)."""
        return self._mcp_client

    # ------------------------------------------------------------------ run
    def run(self, context: AgentContext) -> AgentResult:
        scored: Optional[pa.Table] = self._scored_table(context)
        if scored is None or scored.num_rows == 0:
            self.step("audit", status=Status.ERROR, notes="no scored_accounts input")
            return AgentResult(
                status=Status.ERROR, agent=self.name,
                notes="no ScoredAccounts to audit",
            )

        # Feature context (for cited evidence) is optional but enriches the
        # challenge; absent features just yield a thinner prompt.
        features: Optional[pa.Table] = self._feature_table(context)
        # WA-050: the model published its fitted artifact so the challenge can be
        # grounded in the model's own drivers (no-op when absent).
        self._run_model = context.data_handles.get("risk_model")

        # Record which evidence path is active this run — the MCP client
        # (WA-015) when attached, else the local in-memory stubs.
        self.step(
            "evidence_source",
            notes=f"mcp={getattr(self._mcp_client, 'name', None) or 'local-stub'}",
        )

        # WA-049: the audit slice is STRATIFIED, not top-K.
        top, strata = _select_audit_slice(scored, max(0, int(self.k)))
        self.step(
            "audit_slice",
            notes=(f"auditing {len(top)} of {scored.num_rows} — "
                   + " ".join(f"{k}={v}" for k, v in strata.items())),
        )

        # WA-080: audit every account (parallel when max_workers > 1), then open
        # disputes from the results in ``top`` order. The audit (LLM calls) is the
        # concurrent part; opening disputes is cheap, sequential, and ordered -- so
        # the dispute list (and therefore the downstream debate) is deterministic
        # regardless of which audit finished first.
        results = self._run_audits(scored, features, top)
        disputes: List[Dispute] = []
        n_parsed = 0
        n_parse_fail = 0
        for idx, parsed in zip(top, results):
            if parsed is None:
                n_parse_fail += 1
                continue  # graceful degrade: skip, pipeline continues
            n_parsed += 1
            view, confidence, claim, evidence = parsed
            model_band = str(scored.column("score_band")[idx].as_py())
            if self._should_dispute(model_band, view):
                disputes.append(self._open_dispute(
                    scored, idx, model_band, view, confidence, claim, evidence,
                ))

        # Stash for the orchestrator (DISPUTED routing) + insight (agent_dialogue).
        context.data_handles["risk_disputes"] = disputes
        self.step(
            "audit_done",
            notes=f"parsed={n_parsed} parse_fail={n_parse_fail} disputes={len(disputes)}",
        )
        # Pass the scored table through (artifact_ref unchanged) so the insight
        # agent still resolves its predecessor handle.
        return AgentResult(
            status=Status.OK, agent=self.name, artifact_ref="scored_accounts",
            notes=f"audited top-{len(top)}; opened {len(disputes)} dispute(s)",
        )

    # --------------------------------------------------------- helpers
    def _scored_table(self, context: AgentContext) -> Optional[pa.Table]:
        """Resolve the scored_accounts table from the run context.

        The auditor runs immediately after the risk-model agent, so the
        immediate predecessor's artifact is the scored table; we also fall
        back to scanning prior_results + the shared store by handle name.
        """
        if context.prior_results:
            handle = context.prior_results[-1].artifact_ref
            tbl = context.data_handles.get(handle) if handle else None
            if isinstance(tbl, pa.Table):
                return tbl
        for r in reversed(context.prior_results):
            if r.artifact_ref == "scored_accounts":
                tbl = context.data_handles.get("scored_accounts")
                if isinstance(tbl, pa.Table):
                    return tbl
        return None

    def _feature_table(self, context: AgentContext) -> Optional[pa.Table]:
        tbl = context.data_handles.get("feature_frame")
        return tbl if isinstance(tbl, pa.Table) else None

    def _run_audits(
        self, scored: pa.Table, features: Optional[pa.Table], top: Sequence[int],
    ) -> List[Optional[Tuple[str, Optional[float], str, List[str]]]]:
        """Audit each account in ``top``, returning results in ``top`` order.

        Sequential when ``max_workers <= 1`` (the default) -- literally the
        pre-WA-080 loop, which keeps scripted-MockLLM runs deterministic. When
        ``max_workers > 1`` the per-account audits run on a thread pool: LLM
        calls are network-bound, so threads (which release the GIL during I/O)
        give near-linear speed-up without an async rewrite.
        ``ThreadPoolExecutor.map`` preserves input order, so the caller opens
        disputes deterministically.

        An audit that raises degrades to ``None`` (skip the account) rather than
        failing the whole slice -- matching the graceful-degrade contract the
        sequential path already honours via :meth:`_run_tool_loop`.
        """
        top = list(top)
        if self.max_workers <= 1 or len(top) <= 1:
            return [self._audit_one(scored, features, idx) for idx in top]

        def _safe(idx: int) -> Optional[Tuple[str, Optional[float], str, List[str]]]:
            try:
                return self._audit_one(scored, features, idx)
            except Exception as exc:  # one bad account never kills the slice
                self.step("audit_error", status=Status.ERROR,
                          notes=f"account idx={idx} raised: {exc}")
                return None

        workers = min(self.max_workers, len(top))
        self.step("audit_parallel", notes=f"auditing {len(top)} account(s) on {workers} worker(s)")
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="audit") as ex:
            return list(ex.map(_safe, top))

    def _audit_one(
        self, scored: pa.Table, features: Optional[pa.Table], idx: int,
    ) -> Optional[Tuple[str, Optional[float], str, List[str]]]:
        """Ask the Skeptic for its view on one account; parse JSON.

        WA-041: When the brain supports native tool-calling (``chat`` with
        ``tools``), the Skeptic runs a real Qwen tool-calling loop — Qwen
        decides when to call ``portfolio_stats`` / ``lookup_account``, the
        results are fed back, and the model's final answer is grounded in
        real evidence. The JSON-mode fallback (``complete`` + ``_parse_view_json``)
        is kept for brains that don't support native tools and as a safety net.

        Returns ``(view, confidence, claim, evidence)`` or ``None`` on parse
        failure (graceful degrade — the caller skips the account).
        """
        ctx = self._account_context(scored, features, idx)
        raw = self._run_tool_loop(ctx)
        if raw is None:
            return None
        parsed = _parse_view_json(raw)
        if parsed is None:
            self.step("audit_parse_fail", notes=f"unparsable reply: {raw[:80]!r}")
            return None
        view, confidence, claim, evidence = parsed
        # WA-042: supplement thin LLM evidence with MCP-served analyst
        # aggregates (the primary evidence path) first, then fall back to
        # the per-account feature facts if those are also absent. The
        # hardcoded ``_feature_facts`` is now a true fallback — the primary
        # evidence comes from the MCP-served analyst aggregates when the
        # Data Analyst's output is wired into the AnalyticsStore.
        if not evidence:
            evidence = _mcp_evidence(ctx["portfolio_stats"]) or ctx["feature_facts"]
        return view, confidence, claim, evidence

    def _run_tool_loop(self, ctx: Dict[str, Any]) -> Optional[str]:
        """Run the native tool-calling loop for one account (WA-041).

        If the brain supports ``chat()``, the Skeptic gets the native
        ``portfolio_stats`` + ``lookup_account`` tool schemas and Qwen
        decides when to call them. Tool results are fed back as assistant →
        tool message pairs (the OpenAI conversation shape) so the model sees
        the real evidence before giving its final verdict. When ``tool_calls``
        is empty (or the budget is exhausted), the ``content`` is the final
        JSON answer.

        Falls back to the legacy ``complete()`` JSON-mode path if the brain
        doesn't support ``chat()`` with ``tools`` (e.g. a bare
        legacy LLM subclass). Never crashes.
        """
        prompt = self._prompt(ctx)

        # --- Native tool-calling path (QwenLLM, MockLLM with tool scripts) ---
        if hasattr(self.llm, "chat"):
            try:
                return self._native_tool_loop(ctx, prompt)
            except Exception as exc:
                self.step("audit_call", status=Status.ERROR,
                          notes=f"native tool loop error, falling back: {exc}")
                # Fall through to legacy path

        # --- Legacy JSON-mode fallback ---
        try:
            return self.llm.complete(prompt)
        except Exception as exc:  # brain unreachable → skip, not crash
            self.step("audit_call", status=Status.ERROR, notes=f"llm error: {exc}")
            return None

    def _native_tool_loop(self, ctx: Dict[str, Any], prompt: str) -> Optional[str]:
        """The native tool-calling loop: call → execute → feed back → repeat.

        Uses ``self.llm.chat(tools=..., messages=...)`` with the full
        conversation threaded as OpenAI-format messages so the model sees
        its own tool calls and the results. Terminates when the model stops
        calling tools (``content`` is the final JSON) or the turn budget
        is exhausted.
        """
        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": prompt},
        ]

        for turn in range(_MAX_TOOL_TURNS):
            resp: ChatResponse = self.llm.chat(
                prompt, tools=_AUDITOR_TOOLS, messages=messages,
            )

            if not resp.has_tool_calls:
                # No tool calls → ``content`` is the final answer.
                self.step("audit_native_done",
                          notes=f"tool loop finished after {turn} turn(s); "
                                f"content_len={len(resp.content)}")
                return resp.content

            # The model wants to call tools. Record its assistant message
            # (with the raw tool_calls shape) and execute each one.
            asst_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": resp.content or "",
                "tool_calls": [
                    {
                        "id": tc.id or f"call_{i}",
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }
                    for i, tc in enumerate(resp.tool_calls)
                ],
            }
            messages.append(asst_msg)

            for tc in resp.tool_calls:
                result = self._execute_tool_call(tc, ctx)
                self.step(f"audit_tool:{tc.name}",
                          notes=f"args={tc.arguments[:80]} result={str(result)[:120]}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id or f"call_0",
                    "content": result,
                })

        # Budget exhausted — make one final call without tools to get the verdict.
        self.step("audit_native_budget",
                  notes=f"tool budget exhausted after {_MAX_TOOL_TURNS} turns; "
                        f"requesting final verdict")
        try:
            final = self.llm.chat("\n".join(
                m.get("content", "") for m in messages if m.get("role") == "user"
            ), messages=messages)
            return final.content
        except Exception:
            return None

    def _execute_tool_call(self, tc: ToolCall, ctx: Dict[str, Any]) -> str:
        """Execute one native tool call and return the result as a JSON string.

        Routes ``portfolio_stats`` / ``lookup_account`` to the registered
        tools (local stubs or MCP-backed). Never raises — a tool failure
        degrades to an error string so the loop continues.
        """
        args = tc.parsed_arguments()
        try:
            # WA-080: serialize the actual tool read so parallel audits are safe
            # for any backend (stdio MCP shares one pipe). Held only around the
            # local read, never the LLM call.
            if tc.name == "lookup_account":
                loan_id = str(args.get("loan_id", ctx.get("loan_id", "")))
                fn = self.tools.get("lookup_account", _default_lookup_account)
                # The tool takes (features_table, loan_id); on the MCP path the
                # table is ignored. We stash the features table on ctx.
                with self._tool_lock:
                    row = fn(ctx.get("_features_table"), loan_id)
                return json.dumps(row or {})
            elif tc.name == "portfolio_stats":
                segment = args.get("segment")
                fn = self.tools.get("portfolio_stats", _default_portfolio_stats)
                with self._tool_lock:
                    stats = fn(ctx.get("_scored_table"), segment)
                return json.dumps(stats or {})
            else:
                return json.dumps({"error": f"unknown tool: {tc.name}"})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _account_context(
        self, scored: pa.Table, features: Optional[pa.Table], idx: int,
    ) -> Dict[str, Any]:
        loan_id = str(scored.column("loan_id")[idx].as_py())
        p_default = float(scored.column("p_default")[idx].as_py())
        band = str(scored.column("score_band")[idx].as_py())
        seg = scored.column("segment")[idx].as_py()

        feature_facts: List[str] = []
        feat_row: Optional[Dict[str, Any]] = None
        # ``lookup_account`` is routed through the registered tool. On the MCP
        # path (WA-015) this is the live server round-trip; on the local-stub
        # path it reads the in-memory feature frame directly. The tool takes
        # the features table + loan_id and returns the row dict; an MCP-backed
        # tool ignores the table arg (the client owns the data).
        lookup_fn = self.tools.get("lookup_account", _default_lookup_account)
        try:
            with self._tool_lock:  # WA-080: safe under parallel audits
                row = lookup_fn(features, loan_id) if features is not None else None
        except Exception:  # pragma: no cover - defensive; evidence is enrichment
            row = None
        if isinstance(row, dict) and row:
            feat_row = row
            feature_facts = _feature_facts(feat_row)
        elif features is not None:
            # Fallback to the in-memory scan if the tool returned nothing
            # (e.g. MCP lookup missed because the snapshot is stale). Keeps
            # the audit honest rather than citing empty evidence.
            feat_row = _row_for_loan(features, loan_id)
            if feat_row:
                feature_facts = _feature_facts(feat_row)

        # Portfolio stats via the registered tool (local stub, or MCP in WA-015).
        stats_fn = self.tools.get("portfolio_stats", _default_portfolio_stats)
        try:
            with self._tool_lock:  # WA-080: safe under parallel audits
                stats = stats_fn(scored, seg) if seg is not None else {}
        except Exception:  # pragma: no cover - defensive; stats are enrichment only
            stats = {}
        if not isinstance(stats, dict):
            stats = {}

        # WA-050: the model's own signed logit contributions behind this band, so
        # the Skeptic challenges the model's actual reasoning, not a guess from
        # raw values. Empty string when no model handle is present this run.
        drivers = ""
        band_edges = None
        if self._run_model is not None:
            band_edges = self._run_model.get("band_edges")
            if features is not None:
                drivers = _format_drivers(_explain(self._run_model, features, loan_id, top_n=5))

        return {
            "loan_id": loan_id,
            "p_default": p_default,
            "score_band": band,
            "segment": seg if isinstance(seg, dict) else {},
            "feature_facts": feature_facts,
            "feature_row": feat_row or {},
            "portfolio_stats": stats,
            "drivers": drivers,
            "band_edges": band_edges,
            # WA-041: stash the raw tables for native tool-call execution.
            # The native loop's _execute_tool_call reads these when Qwen
            # emits a tool_calls response.
            "_features_table": features,
            "_scored_table": scored,
        }

    def _prompt(self, ctx: Dict[str, Any]) -> str:
        """Build the JSON-mode challenge prompt for one account."""
        lines = [
            "You are the Skeptic (risk auditor) in a bounded risk debate.",
            f"Account {ctx['loan_id']}: the Actuary (classical ML model) scored it "
            f"p_default={ctx['p_default']:.3f}, band={ctx['score_band']}.",
        ]
        if ctx["feature_facts"]:
            lines.append("Account features: " + "; ".join(ctx["feature_facts"]) + ".")
        stats = ctx["portfolio_stats"]
        if stats:
            stats_line = "; ".join(f"{k}={v}" for k, v in stats.items())
            lines.append(f"Portfolio context: {stats_line}.")
        # WA-050: show the model's own drivers so the Skeptic contests the actual
        # reasoning behind the band (positive = pushed toward default).
        if ctx.get("drivers"):
            lines.append(
                f"The model's own drivers for this score (feature=value "
                f"(signed logit contribution)): {ctx['drivers']}."
            )
        # WA-051: state what the band means in ABSOLUTE PD terms so "Very High"
        # is a threshold, not a batch rank — this is what puts your view and the
        # band on the same scale (avoid contesting a rank you'd actually agree
        # with in absolute terms).
        edges = ctx.get("band_edges")
        if edges and len(edges) == 4:
            lo, low, mid, high, hi = RISK_LEVELS
            e = [f"{x:.2f}" for x in edges]
            lines.append(
                f"Absolute band thresholds (PD): {lo} ≤ {e[0]} < {low} ≤ {e[1]} < "
                f"{mid} ≤ {e[2]} < {high} ≤ {e[3]} < {hi}. "
                f"This account's PD={ctx['p_default']:.3f} places it in {ctx['score_band']}."
            )
        lines.append(
            "Give your INDEPENDENT view of this account's risk. Reply with ONLY a "
            "JSON object, no prose, exactly this shape:"
        )
        lines.append(
            '{"auditor_view": "Low|Medium|High", "confidence": 0.0-1.0, '
            '"claim": "one-sentence rationale", "evidence": ["fact1", "fact2"]}'
        )
        return "\n".join(lines)

    def _open_dispute(
        self, scored: pa.Table, idx: int, model_band: str, view: str,
        confidence: Optional[float], claim: str, evidence: List[str],
    ) -> Dispute:
        loan_id = str(scored.column("loan_id")[idx].as_py())
        round1 = DisputeRound(
            round_no=1,
            speaker=self.name,
            claim=claim or f"Auditor views this {model_band} account as {view} risk.",
            confidence=confidence,
            model=getattr(self.llm, "model_name", None) or getattr(self.llm, "name", None),
            evidence=list(evidence),
        )
        # Round 1 only — resolution is OPEN here. WA-016 adds rebuttal + arbiter
        # (rounds 2-3) and sets resolution/resolved_by; until then the orchestrator
        # routes the live dispute to the human gate (resolve_risk_dispute).
        return Dispute(
            loan_id=loan_id,
            opened_by=self.name,
            rounds=[round1],
            resolution="",        # open / live
            resolved_by="",
            rationale="",
            model_band=model_band,
            auditor_view=view,
        )

    def _should_dispute(self, model_band: str, auditor_view: str) -> bool:
        """Admissibility: dispute iff the band/view ordinals differ by ≥ dispute_gap
        (WA-095: the matrix-configurable gap; defaults to the module DISPUTE_GAP)."""
        # .title() normalizes case for multi-word levels ("very high" → "Very High").
        b = _BAND_ORDINAL.get(str(model_band).strip().title())
        v = _VIEW_ORDINAL.get(str(auditor_view).strip().lower())
        if b is None or v is None:
            return False
        return abs(b - v) >= self.dispute_gap


# --------------------------------------------------------------------------- #
# MCP client adapters (WA-015) — bind an MCP client's dict-returning surface
# onto the auditor's (table, ...) tool signature. The client owns the data
# (scored+features loaded in its store/server), so the table args are ignored.
# --------------------------------------------------------------------------- #
def _make_mcp_portfolio_stats(client: Any):
    """Return a tool fn routing ``portfolio_stats`` through ``client``.

    The auditor calls ``fn(scored, segment)``; the MCP client answers
    ``client.portfolio_stats(segment)``. The scored table is ignored on this
    path (the client's store is the source of truth).
    """
    def _portfolio_stats(scored: pa.Table, segment: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            return client.portfolio_stats(segment) or {}
        except Exception:  # defensive: tool miss degrades to empty stats
            return {}
    return _portfolio_stats


def _make_mcp_lookup_account(client: Any):
    """Return a tool fn routing ``lookup_account`` through ``client``.

    The auditor calls ``fn(features, loan_id)``; the MCP client answers
    ``client.lookup_account(loan_id)``. The features table is ignored on this
    path (the client's store is the source of truth).
    """
    def _lookup_account(features: Optional[pa.Table], loan_id: str) -> Dict[str, Any]:
        try:
            return client.lookup_account(str(loan_id)) or {}
        except Exception:  # defensive: tool miss degrades to empty row
            return {}
    return _lookup_account


# --------------------------------------------------------------------------- #
# JSON parsing — defensive (LLM output is string; Qwen real path would use
# response_format=json_object, but our LLM surface is string-in/string-out).
# --------------------------------------------------------------------------- #
_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")


def _parse_view_json(raw: str) -> Optional[Tuple[str, Optional[float], str, List[str]]]:
    """Parse the Skeptic's JSON reply → (view, confidence, claim, evidence).

    Tolerates surrounding prose / ```json fences by extracting the first
    ``{...}`` blob. Returns ``None`` on any parse failure (caller degrades).
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    m = _JSON_OBJ_RE.search(text)
    blob = m.group(0) if m else text
    try:
        obj = json.loads(blob)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    view = str(obj.get("auditor_view", "")).strip()
    if view.capitalize() not in _VIEWS:
        return None
    view = view.capitalize()
    conf_raw = obj.get("confidence")
    try:
        confidence = float(conf_raw) if conf_raw is not None else None
    except (TypeError, ValueError):
        confidence = None
    if confidence is not None:
        confidence = max(0.0, min(1.0, confidence))
    claim = str(obj.get("claim", "")).strip()
    ev_raw = obj.get("evidence", [])
    evidence = [str(e) for e in ev_raw] if isinstance(ev_raw, list) else []
    return view, confidence, claim, evidence


# --------------------------------------------------------------------------- #
# WA-049 — the audit slice.
#
# The Skeptic used to audit the top-K by ``p_default``: the accounts the model
# had ALREADY flagged. That can only ever surface candidate *false positives* —
# a wasted collector call, which is cheap. The expensive error in collections is
# the *false negative*: an account the model bands "Very Low" that quietly rolls
# to NPL. Top-K never samples those, so the society was structurally incapable of
# catching the error class that motivates the product — and the symmetric half of
# the admissibility rule (``Very Low`` + auditor ``High`` → dispute) was
# unreachable dead code.
#
# So we stratify. Same K, same ≤K×3 LLM-call ceiling, strictly better coverage:
#
#   riskiest       top by p_default — catches over-calling (the old behaviour)
#   boundary       nearest a band edge — where the model is least certain
#   contradictory  low band, adverse evidence — THE FALSE-NEGATIVE CATCHER
#
# ``contradictory`` is a pure rule-based screen over columns already on the
# table: zero extra LLM cost. Short strata spill their quota to ``riskiest`` so
# the audit always spends its full budget.
# --------------------------------------------------------------------------- #
AUDIT_MIX: Dict[str, int] = {"riskiest": 3, "boundary": 2, "contradictory": 3}

# Priority when the budget doesn't divide evenly (and the backfill order). The
# riskiest slice is the Skeptic's primary duty, so a shrinking K degrades
# gracefully back to the pre-WA-049 top-K behaviour rather than starving it.
_STRATA_PRIORITY = ("riskiest", "contradictory", "boundary")

# Delinquency buckets that count as non-performing. Mirrors
# ``waspada.insight.ranking._NPL_BUCKETS`` (kept local to avoid an insight
# import in the agent layer; the two must stay in step).
_NPL_BUCKETS = {"Default", "31-120", "16-30"}

# Bands low enough that active delinquency is a contradiction worth auditing.
_LOW_BANDS = {"Very Low", "Low"}


def _select_audit_slice(scored: pa.Table, k: int) -> Tuple[List[int], Dict[str, int]]:
    """Pick ≤ ``k`` row indices to audit, stratified per :data:`AUDIT_MIX`.

    Returns ``(indices, strata_counts)``. The counts are for the audit log — they
    are how you prove the false-negative stratum actually fired on a given book.
    """
    n = scored.num_rows
    if n == 0 or k <= 0:
        return [], {}

    probs = [float(p) for p in scored.column("p_default").to_pylist()]
    bands = [str(b) for b in scored.column("score_band").to_pylist()]
    try:
        delinq = [str(x) for x in scored.column("delinquency_status").to_pylist()]
    except (KeyError, ValueError, pa.ArrowInvalid):
        delinq = None  # optional monitoring column; without it the screen is off

    budget = min(k, n)

    # Scale AUDIT_MIX (written for the k=8 default) to the actual budget by
    # largest-remainder, so the quotas sum to exactly ``budget``. Ties break on
    # _STRATA_PRIORITY, which is what makes a small K collapse back to plain
    # top-K rather than spending its only slot on a non-riskiest stratum.
    total = sum(AUDIT_MIX.values())
    exact = {name: budget * share / total for name, share in AUDIT_MIX.items()}
    quota = {name: int(v) for name, v in exact.items()}
    for name in sorted(
        exact,
        key=lambda nm: (-(exact[nm] - int(exact[nm])), _STRATA_PRIORITY.index(nm)),
    )[: budget - sum(quota.values())]:
        quota[name] += 1

    # --- candidate orderings, one per stratum ---
    # riskiest: top by p_default (the pre-WA-049 behaviour).
    riskiest = sorted(range(n), key=lambda i: (-probs[i], i))

    # contradictory: the model says safe, the raw evidence says distressed.
    # Ordered by p_default ASC — the *most* confidently-safe delinquent account
    # is the most damning miss, so it is audited first.
    contradictory: List[int] = []
    if delinq is not None:
        contradictory = sorted(
            (i for i in range(n)
             if bands[i] in _LOW_BANDS and delinq[i] in _NPL_BUCKETS),
            key=lambda i: (probs[i], i),
        )

    # boundary: nearest a band edge — where the model is least certain. The edges
    # are the per-batch quintile cutpoints the model itself used (WA-051 makes
    # these absolute; "least certain" survives that change unchanged).
    boundary: List[int] = []
    if n >= 5:
        srt = sorted(probs)
        edges = [srt[int(n * q)] for q in (0.2, 0.4, 0.6, 0.8)]
        boundary = sorted(
            range(n), key=lambda i: (min(abs(probs[i] - e) for e in edges), i),
        )

    pools = {"riskiest": riskiest, "contradictory": contradictory, "boundary": boundary}

    chosen: List[int] = []
    counts: Dict[str, int] = {}
    seen: set = set()

    def take(name: str, limit: int) -> None:
        picked = 0
        for i in pools[name]:
            if len(chosen) >= budget or picked >= limit:
                break
            if i in seen:
                continue
            seen.add(i)
            chosen.append(i)
            picked += 1
        counts[name] = counts.get(name, 0) + picked

    for name in _STRATA_PRIORITY:
        take(name, quota[name])

    # Backfill: a short stratum (e.g. no contradictory accounts on a clean book)
    # spills its quota to the riskiest so the audit always spends its full K.
    if len(chosen) < budget:
        take("riskiest", budget - len(chosen))

    return chosen, counts


# --------------------------------------------------------------------------- #
# Default stub tools (replaced by MCP in WA-015). Same register_tool pattern as
# ingest's fetch: the agent calls tools.get(name, default).
# --------------------------------------------------------------------------- #
def _default_lookup_account(features: pa.Table, loan_id: str) -> Dict[str, Any]:
    """Return the feature row for ``loan_id`` (empty dict if absent)."""
    row = _row_for_loan(features, loan_id)
    return row or {}


def _default_portfolio_stats(scored: pa.Table, segment: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Cheap portfolio-level stats for the prompt's portfolio context.

    Computes the book NPL ratio and, when a segment dict is supplied, the
    segment-level NPL ratio — enough for the Skeptic to cite divergence. The
    real MCP tool (WA-015) will add cure rates / vintage roll rates.
    """
    npl_buckets = {"Default", "31-120", "16-30"}
    n = scored.num_rows
    out: Dict[str, Any] = {}
    try:
        delinq = scored.column("delinquency_status").to_pylist()
    except (KeyError, ValueError, pa.ArrowInvalid):
        delinq = None
    if delinq is not None and n:
        out["book_npl_ratio"] = round(sum(1 for b in delinq if b in npl_buckets) / n, 3)
    if isinstance(segment, dict) and segment:
        seg_product = str(segment.get("product", ""))
        seg_region = str(segment.get("region", ""))
        if seg_product or seg_region:
            seg_rows = [
                i for i in range(n)
                if _seg_matches(scored.column("segment")[i].as_py(), seg_product, seg_region)
            ]
            if seg_rows and delinq is not None:
                seg_npl = sum(1 for i in seg_rows if delinq[i] in npl_buckets) / len(seg_rows)
                out[f"segment_npl_ratio"] = round(seg_npl, 3)
    return out


# --------------------------------------------------------------------------- #
# Small table helpers (pure-Python; the audit slice is tiny).
# --------------------------------------------------------------------------- #
def _row_for_loan(features: pa.Table, loan_id: str) -> Optional[Dict[str, Any]]:
    try:
        ids = features.column("loan_id").to_pylist()
    except (KeyError, ValueError, pa.ArrowInvalid):
        return None
    try:
        pos = ids.index(loan_id)
    except ValueError:
        return None
    names = features.column_names
    return {n: features.column(n)[pos].as_py() for n in names}


def _seg_matches(seg: Any, product: str, region: str) -> bool:
    if not isinstance(seg, dict):
        return False
    p = str(seg.get("product", ""))
    r = str(seg.get("region", ""))
    return (not product or p == product) and (not region or r == region)


def _feature_facts(row: Dict[str, Any]) -> List[str]:
    """Citeable feature facts for one account (the numbers a dispute references).

    WA-042: This is now a FALLBACK. The primary evidence path is the MCP-served
    analyst aggregates (see :func:`_mcp_evidence`). When the Data Analyst's
    output is wired into the AnalyticsStore, the auditor cites those real
    computed statistics first. This function only fires when the MCP path
    returns no citable evidence (e.g. Data Analyst was skipped/blocked).
    """
    facts: List[str] = []
    for key in ("payment_ratio", "outstanding_ratio", "dti", "rate", "loan_age"):
        if key in row and row[key] is not None:
            try:
                val = float(row[key])
                facts.append(f"{key}={val:.2f}")
            except (TypeError, ValueError):
                pass
    if row.get("delinquency_status"):
        facts.append(f"delinquency_status={row['delinquency_status']}")
    if row.get("grade"):
        facts.append(f"grade={row['grade']}")
    return facts


def _mcp_evidence(stats: Optional[Dict[str, Any]]) -> List[str]:
    """Extract citable evidence strings from MCP-served portfolio stats.

    WA-042: When the Data Analyst's aggregates are wired into the
    AnalyticsStore, ``portfolio_stats`` carries an ``analyst_aggregates``
    key with real computed correlations, distributions, and feature
    summaries. This function turns those into concise evidence strings
    the Skeptic can cite in a dispute.

    Returns an empty list when no analyst aggregates are present (the
    caller falls back to per-account ``_feature_facts``).
    """
    if not isinstance(stats, dict) or not stats:
        return []
    agg = stats.get("analyst_aggregates")
    if not isinstance(agg, dict) or not agg:
        return []

    facts: List[str] = []

    # Cite correlations (the strongest cross-feature evidence).
    for corr in agg.get("correlations", []):
        if not isinstance(corr, dict):
            continue
        a = corr.get("a", "?")
        b = corr.get("b", "?")
        r = corr.get("correlation")
        if r is not None:
            try:
                facts.append(f"corr({a},{b})={float(r):.2f}")
            except (TypeError, ValueError):
                pass

    # Cite distribution summaries (quantiles, means).
    for dist in agg.get("distributions", []):
        if not isinstance(dist, dict):
            continue
        col = dist.get("column", "?")
        median = dist.get("median")
        mean = dist.get("mean")
        if median is not None:
            try:
                facts.append(f"median({col})={float(median):.2f}")
            except (TypeError, ValueError):
                pass
        elif mean is not None:
            try:
                facts.append(f"mean({col})={float(mean):.2f}")
            except (TypeError, ValueError):
                pass

    # Cite feature aggregates (the Data Analyst's build_feature explorations).
    for fa in agg.get("feature_aggregates", []):
        if not isinstance(fa, dict):
            continue
        results = fa.get("result", [])
        if isinstance(results, list):
            for row in results[:3]:  # cap at 3 to keep evidence concise
                if isinstance(row, dict):
                    parts = [f"{k}={v}" for k, v in row.items()
                             if not isinstance(v, (list, dict))]
                    if parts:
                        facts.append("; ".join(parts))

    return facts
