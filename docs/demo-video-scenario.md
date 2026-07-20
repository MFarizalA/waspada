# Demo video scenario (~3 min) — the product demo

**Goal:** show a *society of AI agents debating real loan-collection decisions* on Alibaba Cloud.
The money shot is the live Qwen debate. Record the screen; a calm voiceover reads the beats.
**Names on screen/voice: industry only — Risk Auditor, Actuary, Credit Arbiter** (never Skeptic/Judge/Defendant).

**Setup before recording:** open `https://app.waspada.xyz`, log in (`analyst@waspada.demo` / `waspada123`).
Have the dashboard loaded. If doing the live debate, be ready to trigger **one** `brain=qwen` run
(conserves credits — do it once, cleanly).

---

### 0:00–0:25 · The problem + the dashboard
> "Collections teams can only call a fraction of delinquent borrowers each day — deciding *which*,
> and defending it, is the job. WASPADA is an early-warning system where a **society of agents**
> makes that call."

Show the **EWS dashboard**: the work-list (riskiest accounts + recommended action), portfolio
health (NPL ratio, worst vintage), and the cohort alerts. Note the bilingual EN/中文 toggle.

### 0:25–1:20 · The society debate  ← THE MONEY SHOT
> "But a single score has no second opinion. So the agents argue about the riskiest accounts."

Scroll to the **Agent Society panel**. Point at the **debate flow-chart** (the node graph): the
society spine, then the selected dispute's branch lighting up round by round.
- **Risk Auditor** challenges a score (cites feature evidence via MCP).
- **Actuary** defends or concedes — from the model's *own* signed drivers, not a guess.
- **Credit Arbiter** rules when they disagree.
> "Each claim cites evidence through a real MCP tool interface. The confidence dots — green, amber,
> red — show how sure each agent is. The agents run on **Qwen Cloud**, tiered by cognitive load:
> flash to triage, plus to analyze, max to rule."

*(Optional live money-shot: trigger **one** `brain=qwen` run and let the flow-chart animate as the
SSE rounds arrive. Otherwise the committed fixture shows a real completed debate — same UI.)*

### 1:20–2:00 · Governance — the debate is load-bearing, and a human is in control
> "Disagreement is a first-class state — and the human governs it."

Three quick beats:
- **The debate changes the decision.** Show a **DISPUTED** row: the ruling updates the band *and*
  the recommended action — not just the transcript. Governance is asymmetric: raising risk
  auto-applies; *cancelling* a collector call needs the **human gate** (the Human Gate panel).
- **The Model Card** (side panel): AUC, a *calibrated* default probability, the served band-mix bar,
  a drift flag, and the exact model version (`pd-lr-…`) that scored the run.
- **The Parameter Matrix:** edit the band→action grid or a knob (dispute gap, audit K), hit **Run
  with this matrix** — the work-list and debate obey it, stamped with a `policy_id`.
> "A calibrated, version-tracked model, drift-monitored, under a policy the analyst sets. The human
> doesn't just approve one call — they set the rules the whole society plays by."

### 2:00–2:35 · One engine, two lanes
> "The same society runs a second product lane — with zero architecture change."

Show (or narrate over the CLI) the **Origination** lane: instead of call/watch/auto-cure, the
society decides **approve / refer / reject** on new applications, with its own application-time
model — the debate, gate, and dashboard reused verbatim.

### 2:35–3:00 · Real data + Alibaba + efficiency
> "It runs on **real Lending Club loan data** in Alibaba **OSS**, loaded through a **dlt** pipeline
> with lineage the Data Engineer cites, scored by a leakage-guarded sklearn model — all on
> **Function Compute**. The society scores 100% of the book classically and spends LLM budget only on
> the contested few — a measurable efficiency gain over one-call-per-account. Live at
> **app.waspada.xyz**."

---

**Shot list (if editing):** dashboard (blue theme) → debate flow-chart mid-argument → a dispute +
Human Gate → Model Card → Parameter Matrix run → origination (approve/refer/reject) → OSS/FC console
flash → live URL. **Fallback if live Qwen is flaky:** the committed fixture shows a real completed
debate + a populated model card — record that; it's the same UI, zero risk.
