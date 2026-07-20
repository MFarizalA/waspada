# Debate Mechanism

> The heart of WASPADA: a **bounded, evidence-cited, adversarial debate** that
> turns a silent model score into a decision you can interrogate. This is what
> makes the [six-agent society](02-system-architecture.md) *causally* matter — the
> debate changes the work-list, it doesn't just narrate beside it.

## 1. When does a debate start? — Admissibility

The Actuary bands every account. The **Skeptic** audits a slice and forms its own
independent view. A **dispute opens** only when the two disagree meaningfully:

Both map onto a shared 5-point ordinal:

```
Actuary band:  Very Low=1  Low=2  Medium=3  High=4  Very High=5
Skeptic view:  Low=1                Medium=3               High=5
```

A dispute is **admissible** iff `|band − view| ≥ dispute_gap` (default **2**). So
*Very High + Low* disputes; *Very High + High* does not. The `dispute_gap` is
**human-configurable** via the [parameter matrix](09-ml-governance.md#the-parameter-matrix)
(WA-095) — tighten it to 1 to argue more, loosen to 3–4 to argue less.

## 2. Which accounts get audited? — The stratified slice (WA-049)

The Skeptic audits **K accounts** (default 8), but *which* K matters. The naive
choice — top-K by `p_default` — only ever surfaces candidate **false positives**
(a wasted collector call: cheap). The expensive error in collections is the
**false negative** — an account banded "Very Low" that quietly rolls to default.

So the slice is **stratified** at the same K (same LLM-call ceiling):

| Stratum | Picks | Catches |
|---------|-------|---------|
| `riskiest` | top by `p_default` | over-calling (the old behaviour) |
| `boundary` | nearest a band edge | where the model is least certain |
| `contradictory` | low band + adverse evidence | **the false-negative catcher** |

`contradictory` is a zero-LLM-cost rule screen over columns already on the table.
Short strata spill their quota to `riskiest`, so the audit always spends its budget.

## 3. The three rounds

For each admissible dispute, the orchestrator runs a bounded debate:

```
Round 1 · Skeptic  challenge  ──"opens dispute"──▶  cites evidence, states a view + confidence
Round 2 · Actuary  rebuttal   ──"uphold|concede"─▶  defends the band from the model's OWN drivers
Round 3 · Arbiter  ruling      ──"uphold|override|escalate"─▶  reads both, rules with confidence
                                                    │
                                                    ▼
                                            terminal resolution
```

- **Round 1 — Skeptic (challenge).** Runs the native tool-loop, pulling
  `portfolio_stats` / `lookup_account` evidence, and returns a JSON view
  (`{auditor_view, confidence, claim, evidence}`). Every claim cites evidence.
- **Round 2 — Actuary (rebuttal).** Defends *from the model's own signed logit
  contributions* (`explain()`, WA-050) — so it contests its actual reasoning, not a
  guess. Returns `{verdict: uphold|concede, …}`. A **concession** ends the debate
  (the Skeptic's view stands → the band is revised).
- **Round 3 — Arbiter (ruling).** Reads Rounds 1–2 and rules `uphold` (band stands),
  `override` (Skeptic wins — and it **must name the revised band**, WA-048), or
  `escalate` (genuinely uncertain). Below a confidence threshold
  (`arbiter_confidence`, default 0.6, matrix-configurable) it escalates rather than
  rule.

## 4. Adjudication — the loop that closes (WA-048)

A debate that only produces a transcript is theatre. WASPADA **applies the ruling**:

- `Dispute.revised_band` + `applied` carry the society's decision.
- The orchestrator writes **additive** `final_band` + `override_reason` columns back
  and **re-derives `recommended_action`** from `final_band`.
- **`p_default` / `score_band` are never rewritten** — the model's score stays the
  auditable fact; the override is a *reason-coded layer* on top.

**Direction rule** (owner-confirmed): an **escalation** (society raises risk) is
auto-applied (worst case: a wasted call). A **de-escalation** (society cancels a
call) needs the **human gate** (worst case: a missed default). Escalations that go
to the human are surfaced in the dashboard's Human Gate panel.

## 5. The cost ceiling

The debate is **deterministically bounded**: ≤ K×3 debate *rounds* (K challenges +
≤ K rebuttals + ≤ K rulings). Each challenge is itself a bounded native tool-loop
(≤ 4 turns), so the worst-case LLM-call count is ≤ K×6; typical runs far less. No
open-ended agent chatter — a hard requirement for a system that must finish inside
the Function Compute invocation timeout.

**WA-080** collapses the audit wall-clock: the K per-account audits run
**concurrently** on the thread-safe Qwen client (from *sum* of K chains to the
*longest single* chain), while the scripted mock stays sequential/deterministic.

## 6. Cross-run memory (WA-026)

Before opening a debate, the orchestrator consults **dispute memory**. A prior
**human** ruling on the same loan short-circuits the debate (reuse the ruling, spend
no LLM calls) — the strongest precedent. Any other prior ruling is injected as
context so the debate *sees* precedent without being silenced.

## 7. What the user sees

- **Live**: the SSE stream (`/api/run/stream`) emits `round` / `resolution` events;
  the dashboard's **debate flow-chart** lights up node by node as they arrive.
- **After**: the transcript (per-round claims + cited evidence + confidence tiers),
  the resolution, and — for escalations — the Human Gate panel.

**Related:** [Harness Architecture](03-harness-architecture.md) ·
[ML Governance](09-ml-governance.md) · [System Architecture](02-system-architecture.md)
