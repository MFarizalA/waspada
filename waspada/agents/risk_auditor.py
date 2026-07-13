"""Risk-auditor agent (WA-014) — the Skeptic.

After the classical-ML Actuary (:class:`~waspada.agents.risk_model.RiskModelAgent`)
scores 100% of the book, the Skeptic audits the **top-K riskiest accounts**
(K=8 default) and, where its independent view diverges from the Actuary's band,
opens a :class:`~waspada.agents.protocol.Dispute` with a Round-1 challenge round.

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
from typing import Any, Dict, List, Optional, Tuple

import pyarrow as pa

from .base import Agent
from .llm import LLM, MockLLM
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


class RiskAuditorAgent(Agent):
    """Audit the top-K riskiest accounts and open disputes where the Skeptic
    disagrees with the Actuary's band."""

    name = "risk_auditor"
    role = "audit top-K scores and open disputes"

    def __init__(self, llm: Optional[LLM] = None, *, k: int = 8) -> None:
        super().__init__(llm=llm if llm is not None else MockLLM())
        self.k = k
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

        # Record which evidence path is active this run — the MCP client
        # (WA-015) when attached, else the local in-memory stubs.
        self.step(
            "evidence_source",
            notes=f"mcp={getattr(self._mcp_client, 'name', None) or 'local-stub'}",
        )

        # Top-K by p_default (the riskiest slice is where a wrong band costs most).
        probs = scored.column("p_default").to_pylist()
        order = sorted(range(scored.num_rows), key=lambda i: (-float(probs[i]), i))
        top = order[: max(0, int(self.k))]
        self.step("audit_top_k", notes=f"auditing top-{len(top)} of {scored.num_rows} by p_default")

        disputes: List[Dispute] = []
        n_parsed = 0
        n_parse_fail = 0
        for idx in top:
            parsed = self._audit_one(scored, features, idx)
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

    def _audit_one(
        self, scored: pa.Table, features: Optional[pa.Table], idx: int,
    ) -> Optional[Tuple[str, Optional[float], str, List[str]]]:
        """Ask the Skeptic for its view on one account; parse JSON.

        Returns ``(view, confidence, claim, evidence)`` or ``None`` on parse
        failure (graceful degrade — the caller skips the account).
        """
        ctx = self._account_context(scored, features, idx)
        prompt = self._prompt(ctx)
        try:
            raw = self.llm.complete(prompt)
        except Exception as exc:  # brain unreachable → skip, not crash
            self.step("audit_call", status=Status.ERROR, notes=f"llm error: {exc}")
            return None
        parsed = _parse_view_json(raw)
        if parsed is None:
            self.step("audit_parse_fail", notes=f"unparsable reply: {raw[:80]!r}")
            return None
        view, confidence, claim, evidence = parsed
        # Supplement thin LLM evidence with the hard feature facts the agent
        # gathered, so an opened dispute always cites real numbers.
        if not evidence:
            evidence = ctx["feature_facts"]
        return view, confidence, claim, evidence

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
            stats = stats_fn(scored, seg) if seg is not None else {}
        except Exception:  # pragma: no cover - defensive; stats are enrichment only
            stats = {}
        if not isinstance(stats, dict):
            stats = {}

        return {
            "loan_id": loan_id,
            "p_default": p_default,
            "score_band": band,
            "segment": seg if isinstance(seg, dict) else {},
            "feature_facts": feature_facts,
            "feature_row": feat_row or {},
            "portfolio_stats": stats,
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

    @staticmethod
    def _should_dispute(model_band: str, auditor_view: str) -> bool:
        """Admissibility: dispute iff the band/view ordinals differ by ≥ DISPUTE_GAP."""
        # .title() normalizes case for multi-word levels ("very high" → "Very High").
        b = _BAND_ORDINAL.get(str(model_band).strip().title())
        v = _VIEW_ORDINAL.get(str(auditor_view).strip().lower())
        if b is None or v is None:
            return False
        return abs(b - v) >= DISPUTE_GAP


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
    """Citeable feature facts for one account (the numbers a dispute references)."""
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
