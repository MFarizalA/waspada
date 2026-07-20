"""Risk-Model agent (WA-009 + WA-016) — scoring + dispute rebuttal (Round 2).

Wraps :mod:`waspada.model.risk` (train + predict). Reads the FeatureFrame the
analytics agent published, fits the model (vintage split, leakage-safe), scores
every account, and publishes the :class:`~waspada.schema.ScoredAccounts` table.
Flags the highest-risk band in its notes (the WA-009 acceptance: "risk-model
agent flags score bands").

WA-016 adds :meth:`RiskModelAgent.defend_score` — Round 2 of the bounded debate
(the Actuary rebuts the Skeptic's challenge). Brain ``qwen3.7-plus`` (mid-tier)
via :meth:`~waspada.agents.llm.LLM.with_model`; on the offline mock brain the
override is a no-op. Every rebuttal cites evidence (HACKATHON.md § debate
protocol). A concession closes the dispute (resolution="overridden",
resolved_by="risk_model"); an uphold sends it to the Arbiter (Round 3). An
unparsable rebuttal auto-escalates (safe degrade — the orchestrator routes to
the human gate).
"""
from __future__ import annotations

import json
import re
from typing import Any, List, Optional, Tuple

import pyarrow as pa

from ..model.risk import (
    explain as _explain,
    format_drivers as _format_drivers,
    predict as _predict,
    train as _train,
)
from ..schema import ScoredAccounts, validate_table
from .base import Agent
from .llm import LLM, MockLLM, qwen_tier
from .protocol import AgentContext, AgentResult, Dispute, DisputeRound, Status

__all__ = ["RiskModelAgent"]


# The Actuary's rebuttal verdict vocabulary (Round 2). ``uphold`` = the model
# stands by its band; ``concede`` = it accepts the Skeptic's critique. Anything
# else from the brain is treated as unparsable (→ auto-escalate).
_VERDICTS = ("uphold", "concede")


class RiskModelAgent(Agent):
    """Train + score the risk model on the analytics FeatureFrame."""

    name = "risk_model"
    role = "score P(default) per account and attach risk bands"

    def __init__(self, llm: Optional[Any] = None) -> None:
        super().__init__(llm=llm if llm is not None else MockLLM())
        # WA-050: the fitted model artifact from the last run(), kept so the
        # Actuary's Round-2 defense can introspect its OWN coefficients (the
        # score it defends is a number it can now decompose, not a black box).
        self._model: Optional[dict] = None

    def run(self, context: AgentContext) -> AgentResult:
        if not context.prior_results:
            self.step("train", status=Status.ERROR, notes="no predecessor")
            return AgentResult(status=Status.ERROR, agent=self.name, notes="no FeatureFrame input")
        frame_handle = context.prior_results[-1].artifact_ref
        frame: Optional[pa.Table] = context.data_handles.get(frame_handle) if frame_handle else None
        if frame is None:
            self.step("train", status=Status.ERROR, notes=f"handle {frame_handle!r} missing")
            return AgentResult(
                status=Status.ERROR, agent=self.name,
                notes=f"FeatureFrame handle {frame_handle!r} not found",
            )

        self.step("train", notes=f"rows={frame.num_rows} (vintage split)")
        try:
            model = _train(frame)
        except Exception as exc:
            self.step("train", status=Status.ERROR, notes=str(exc))
            return AgentResult(status=Status.ERROR, agent=self.name, notes=f"train failed: {exc}")

        auc = model.get("metrics", {}).get("auc")
        self.step(
            "train_done",
            notes=f"split={model['split']['method']} auc={auc}" if auc else f"split={model['split']['method']}",
        )

        try:
            scored = _predict(model, frame)
            validate_table(scored, ScoredAccounts, name="RiskModelAgent(scored)")
        except Exception as exc:
            self.step("predict", status=Status.ERROR, notes=str(exc))
            return AgentResult(status=Status.ERROR, agent=self.name, notes=f"predict failed: {exc}")

        # Flag the highest-risk level ("Very High") count — the "flags score bands" check.
        bands = scored.column("score_band").to_pylist()
        n_highest = sum(1 for b in bands if b == "Very High")
        # WA-051: record which banding calibration was used, so the audit trail
        # is honest about whether "Very High" meant an absolute PD threshold or a
        # per-batch rank.
        edges = model.get("band_edges")
        banding_mode = "absolute" if edges else "relative"
        edges_note = (" edges=[" + ", ".join(f"{e:.3f}" for e in edges) + "]") if edges else ""
        self.step("score_bands",
                  notes=f"Very High(highest)={n_highest} of {len(bands)}; "
                        f"banding={banding_mode}{edges_note}")

        handle = "scored_accounts"
        context.data_handles[handle] = scored
        # WA-050: keep the model so defend_score() can cite its own drivers, and
        # publish it as an in-process handle so the Skeptic can ground its
        # challenge in the same attribution. In-process only — never serialized
        # into the dashboard payload.
        self._model = model
        context.data_handles["risk_model"] = model
        return AgentResult(
            status=Status.OK, agent=self.name, artifact_ref=handle,
            notes=f"scored {scored.num_rows} accounts; Very High={n_highest}",
        )

    # ---------------------------------------------------- Round 2 (WA-016)
    def defend_score(
        self,
        dispute: Dispute,
        scored: Optional[pa.Table] = None,
        features: Optional[pa.Table] = None,
    ) -> DisputeRound:
        """Rebut the Skeptic's Round-1 challenge (the Actuary speaks).

        Reads the dispute's Round 1 claim + the account's feature context and
        asks the rebuttal brain (``qwen3.7-plus`` via :meth:`with_model`) to
        either ``uphold`` (stand by the band) or ``concede`` (accept the
        critique), citing evidence.

        Returns a Round 2 :class:`DisputeRound` (speaker=``risk_model``). The
        caller (orchestrator) reads ``claim`` for the verdict keyword — an
        ``uphold``/``concede`` token is embedded as the first line of the claim
        so the orchestrator can route without re-parsing the JSON.

        Unparsable rebuttal → returns a DisputeRound whose claim is prefixed
        ``UNPARSABLE`` (the orchestrator treats this as auto-escalate to the
        human gate — safe degrade, never a crash).
        """
        # Tier the brain up to the mid rebuttal model. ``with_model`` is a
        # no-op on MockLLM (offline path records model="mock"); on QwenLLM it
        # clones the shared client onto ``qwen3.7-plus``.
        brain = self.llm.with_model(qwen_tier("plus"))
        model_name = getattr(brain, "model_name", None) or getattr(brain, "name", None)

        # Round 1 challenge (the claim the Actuary is rebutting).
        r1 = dispute.rounds[0] if dispute.rounds else None
        challenge = (r1.claim if r1 else "").strip() or "(no challenge text)"

        # Account feature context — the numbers the Actuary cites to defend.
        feat_facts: List[str] = []
        if features is not None and dispute.loan_id != "":
            row = _row_for_loan(features, dispute.loan_id)
            if row:
                feat_facts = _feature_facts(row)
        # The model's own stated confidence (p_default) if we can find the row.
        p_default: Optional[float] = None
        if scored is not None and dispute.loan_id != "":
            p_default = _p_default_for(scored, dispute.loan_id)

        # WA-050: the model's OWN drivers — the signed logit contributions behind
        # this band. This is what lets the Actuary defend the score it produced
        # rather than reason from the same raw values the Skeptic already has.
        drivers = ""
        if self._model is not None and features is not None and dispute.loan_id != "":
            drivers = _format_drivers(_explain(self._model, features, dispute.loan_id, top_n=5))

        prompt = self._rebuttal_prompt(
            dispute, challenge, feat_facts, p_default, drivers,
        )
        try:
            raw = brain.complete(prompt)
        except Exception as exc:  # brain unreachable → safe-degrade round
            self.step("defend_call", status=Status.ERROR, notes=f"llm error: {exc}")
            return DisputeRound(
                round_no=2, speaker=self.name,
                claim="UNPARSABLE: rebuttal brain unreachable",
                confidence=None, model=model_name, evidence=[],
            )

        parsed = _parse_verdict_json(raw)
        if parsed is None:
            self.step("defend_parse_fail", notes=f"unparsable rebuttal: {raw[:80]!r}")
            return DisputeRound(
                round_no=2, speaker=self.name,
                claim="UNPARSABLE: could not parse rebuttal verdict",
                confidence=None, model=model_name, evidence=[],
            )
        verdict, confidence, claim_text, evidence = parsed
        # Supplement thin LLM evidence with the hard feature facts so the
        # rebuttal always cites real numbers (parity with the auditor).
        if not evidence:
            evidence = feat_facts
        # Embed the verdict token as the first line so the orchestrator can
        # route on the claim string without re-parsing JSON.
        claim = f"{verdict.upper()}: {claim_text}".strip()
        self.step("defend_done", notes=f"verdict={verdict} conf={confidence}")
        return DisputeRound(
            round_no=2, speaker=self.name,
            claim=claim, confidence=confidence,
            model=model_name, evidence=list(evidence),
        )

    # --------------------------------------------------------- rebuttal helpers
    def _rebuttal_prompt(
        self, dispute: Dispute, challenge: str,
        feat_facts: List[str], p_default: Optional[float],
        drivers: str = "",
    ) -> str:
        lines = [
            "You are the Actuary (classical risk model) in a bounded risk debate.",
            f"Account {dispute.loan_id}: you scored it "
            f"band={dispute.model_band}",
        ]
        if p_default is not None:
            lines.append(f"Your model's p_default={p_default:.3f}.")
        lines.append(
            f"The Skeptic (risk_auditor) opened a dispute, viewing this as "
            f"{dispute.auditor_view} risk. Its challenge: \"{challenge}\"."
        )
        if feat_facts:
            lines.append("Account features: " + "; ".join(feat_facts) + ".")
        # WA-050: the model's own reasoning — signed logit contributions summing
        # (with the intercept) to this score. A positive term raised the risk, a
        # negative lowered it. Defend from THESE, not just the raw values.
        if drivers:
            lines.append(
                f"Your model's own drivers for this score (feature=value "
                f"(signed logit contribution), largest first): {drivers}. "
                "Positive pushed toward default, negative toward safe."
            )
        lines.append(
            "Defend or concede your band. Reply with ONLY a JSON object, no prose, "
            "exactly this shape:"
        )
        lines.append(
            '{"verdict": "uphold|concede", "confidence": 0.0-1.0, '
            '"claim": "one-sentence rationale", "evidence": ["fact1", "fact2"]}'
        )
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# JSON parsing — the Actuary's rebuttal verdict (tolerant of prose / fences).
# --------------------------------------------------------------------------- #
_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")


def _parse_verdict_json(raw: str) -> Optional[Tuple[str, Optional[float], str, List[str]]]:
    """Parse the Actuary's rebuttal JSON → (verdict, confidence, claim, evidence).

    Tolerates surrounding prose / ```json fences by extracting the first
    ``{...}`` blob. ``verdict`` must be ``uphold`` or ``concede`` (lower-cased);
    anything else returns ``None`` (caller auto-escalates). Returns ``None`` on
    any parse failure.
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
    verdict = str(obj.get("verdict", "")).strip().lower()
    if verdict not in _VERDICTS:
        return None
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
    return verdict, confidence, claim, evidence


# --------------------------------------------------------------------------- #
# Small table helpers (pure-Python; mirror the auditor's lookup pattern).
# --------------------------------------------------------------------------- #
def _row_for_loan(features: pa.Table, loan_id: str) -> Optional[dict]:
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


def _p_default_for(scored: pa.Table, loan_id: str) -> Optional[float]:
    try:
        ids = scored.column("loan_id").to_pylist()
        pos = ids.index(loan_id)
        return float(scored.column("p_default")[pos].as_py())
    except (ValueError, KeyError, pa.ArrowInvalid):
        return None


def _feature_facts(row: dict) -> List[str]:
    """Citeable feature facts (same vocabulary the auditor cites)."""
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
