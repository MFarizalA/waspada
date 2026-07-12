"""Arbiter agent (WA-016) — Round 3 of the bounded risk debate.

After the Skeptic (:class:`~waspada.agents.risk_auditor.RiskAuditorAgent`,
Round 1) challenged and the Actuary
(:class:`~waspada.agents.risk_model.RiskModelAgent`, Round 2) rebutted, the
:class:`ArbiterAgent` reads both arguments and rules: ``uphold`` (the model's
band stands), ``override`` (the Skeptic's critique wins), or ``escalate``
(low confidence — punt to the human
:class:`~waspada.agents.base.ApprovalGate`).

Brain: ``qwen3.7-max`` (the top tier) via
:meth:`~waspada.agents.llm.LLM.with_model`; the offline mock brain ignores the
override. Every ruling cites which argument it found more compelling and a
confidence (0-1). Below :data:`ARBITER_CONFIDENCE_THRESHOLD` (default 0.6) the
arbiter escalates rather than rule — safe degrade, the human gate has the final
call. Unparsable ruling → escalate.

The orchestrator maps the arbiter's ruling onto the terminal dispute states:

    uphold    → resolution="upheld",       resolved_by="arbiter"
    override  → resolution="overridden",   resolved_by="arbiter"
    escalate  → routed to the gate → escalated_approved / escalated_rejected
"""
from __future__ import annotations

import json
import re
from typing import Any, List, Optional, Tuple

from .base import Agent
from .llm import LLM, MockLLM, qwen_tier
from .protocol import AgentContext, AgentResult, Dispute, DisputeRound, Status

__all__ = ["ArbiterAgent", "ARBITER_CONFIDENCE_THRESHOLD"]


# Below this stated confidence the arbiter punts to the human gate rather than
# rule. Tunable (the brief asked for ~0.6) — exported so the orchestrator +
# tests reference one constant.
ARBITER_CONFIDENCE_THRESHOLD = 0.6

# Valid arbiter rulings (the JSON ``ruling`` vocabulary).
_RULINGS = ("uphold", "override", "escalate")


class ArbiterAgent(Agent):
    """Read Rounds 1-2 of a dispute and return a final ruling (Round 3)."""

    name = "arbiter"
    role = "rule on contested risk scores after the debate"

    def __init__(self, llm: Optional[LLM] = None, *, threshold: float = ARBITER_CONFIDENCE_THRESHOLD) -> None:
        super().__init__(llm=llm if llm is not None else MockLLM())
        self.threshold = float(threshold)

    def run(self, context: AgentContext) -> AgentResult:  # type: ignore[override]
        """The Arbiter is not a pipeline step — it is invoked per-dispute via
        :meth:`rule` from the orchestrator's dispute-resolution flow. It never
        appears in ``COLLECTIONS_STEP_ORDER``, so ``run`` is not exercised.

        Implemented (not left abstract) only to satisfy :class:`Agent`'s ABC
        contract so the orchestrator can construct an ArbiterAgent eagerly in
        ``_build_agents``. Calling it is a programmer error.
        """
        raise NotImplementedError(
            "ArbiterAgent is invoked via .rule(dispute) from the orchestrator's "
            "dispute-resolution flow, not via .run(context). It is not a pipeline "
            "step (see COLLECTIONS_STEP_ORDER)."
        )

    def rule(self, dispute: Dispute) -> Tuple[str, str, float, DisputeRound]:
        """Rule on ``dispute`` after reading Rounds 1-2.

        Returns ``(ruling, rationale, confidence, round)`` where ``ruling`` is
        one of ``uphold`` / ``override`` / ``escalate`` and ``round`` is the
        Round 3 :class:`DisputeRound` to append to the transcript. Low
        confidence (below :attr:`threshold`) forces ``escalate`` even if the
        brain offered a confident-ish uphold/override — the human gate gets the
        borderline calls. Unparsable → ``escalate``.
        """
        brain = self.llm.with_model(qwen_tier("max"))
        model_name = getattr(brain, "model_name", None) or getattr(brain, "name", None)

        prompt = self._ruling_prompt(dispute)
        try:
            raw = brain.complete(prompt)
        except Exception as exc:  # brain unreachable → escalate
            self.step("rule_call", status=Status.ERROR, notes=f"llm error: {exc}")
            return self._escalate_round(model_name, "arbiter brain unreachable")

        parsed = _parse_ruling_json(raw)
        if parsed is None:
            self.step("rule_parse_fail", notes=f"unparsable ruling: {raw[:80]!r}")
            return self._escalate_round(model_name, "could not parse ruling")
        ruling, confidence, rationale, evidence = parsed

        # Low-confidence uphold/override → escalate (borderline calls go human).
        if ruling in ("uphold", "override") and confidence is not None and confidence < self.threshold:
            self.step("rule_low_confidence", notes=f"ruling={ruling} conf={confidence} < {self.threshold} → escalate")
            round_ = DisputeRound(
                round_no=3, speaker=self.name,
                claim=f"ESCALATE: {ruling} proposed but confidence {confidence:.2f} below threshold {self.threshold:.2f}",
                confidence=confidence, model=model_name, evidence=list(evidence),
            )
            return "escalate", (rationale or "low confidence"), float(confidence), round_

        claim = f"{ruling.upper()}: {rationale}".strip()
        round_ = DisputeRound(
            round_no=3, speaker=self.name,
            claim=claim, confidence=confidence,
            model=model_name, evidence=list(evidence),
        )
        self.step("rule_done", notes=f"ruling={ruling} conf={confidence}")
        return ruling, (rationale or ""), float(confidence) if confidence is not None else 0.0, round_

    # --------------------------------------------------------- ruling helpers
    def _ruling_prompt(self, dispute: Dispute) -> str:
        r1 = dispute.rounds[0] if len(dispute.rounds) > 0 else None
        r2 = dispute.rounds[1] if len(dispute.rounds) > 1 else None
        lines = [
            "You are the Arbiter in a bounded risk debate. Read both arguments "
            "and rule finally. Do not re-open the debate.",
            f"Account {dispute.loan_id}: model band={dispute.model_band}, "
            f"auditor view={dispute.auditor_view} risk.",
        ]
        if r1:
            lines.append(
                f"Round 1 (Skeptic, model={r1.model}): \"{r1.claim}\" "
                f"(confidence={r1.confidence})."
            )
        if r2:
            lines.append(
                f"Round 2 (Actuary, model={r2.model}): \"{r2.claim}\" "
                f"(confidence={r2.confidence})."
            )
        lines.append(
            "Rule which side wins, or escalate if genuinely uncertain. Reply "
            "with ONLY a JSON object, no prose, exactly this shape:"
        )
        lines.append(
            '{"ruling": "uphold|override|escalate", "confidence": 0.0-1.0, '
            '"rationale": "one-sentence decision", "evidence": ["fact1"]}'
        )
        return "\n".join(lines)

    def _escalate_round(self, model_name: Optional[str], reason: str) -> Tuple[str, str, float, DisputeRound]:
        round_ = DisputeRound(
            round_no=3, speaker=self.name,
            claim=f"ESCALATE: {reason}",
            confidence=None, model=model_name, evidence=[],
        )
        return "escalate", reason, 0.0, round_


# --------------------------------------------------------------------------- #
# JSON parsing — the arbiter's ruling (tolerant of prose / fences).
# --------------------------------------------------------------------------- #
_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")


def _parse_ruling_json(raw: str) -> Optional[Tuple[str, Optional[float], str, List[str]]]:
    """Parse the Arbiter's ruling JSON → (ruling, confidence, rationale, evidence).

    ``ruling`` must be ``uphold`` / ``override`` / ``escalate`` (lower-cased);
    anything else returns ``None`` (caller escalates). Returns ``None`` on any
    parse failure.
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
    ruling = str(obj.get("ruling", "")).strip().lower()
    if ruling not in _RULINGS:
        return None
    conf_raw = obj.get("confidence")
    try:
        confidence = float(conf_raw) if conf_raw is not None else None
    except (TypeError, ValueError):
        confidence = None
    if confidence is not None:
        confidence = max(0.0, min(1.0, confidence))
    rationale = str(obj.get("rationale", "")).strip()
    ev_raw = obj.get("evidence", [])
    evidence = [str(e) for e in ev_raw] if isinstance(ev_raw, list) else []
    return ruling, confidence, rationale, evidence
