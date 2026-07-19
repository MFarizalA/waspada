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

### 0:25–1:30 · The society debate  ← THE MONEY SHOT
> "But a single score has no second opinion. So the agents argue about the riskiest accounts."

Trigger the live run (**Run live / `brain=qwen`**). On the **Agent Society panel**, let the debate stream:
- **Risk Auditor** challenges a score (cites feature evidence via MCP).
- **Actuary** defends or concedes (its classical-ML score + the model's own drivers).
- **Credit Arbiter** rules when they disagree.
> "Each claim must cite evidence — pulled through a real MCP tool interface. The agents run on
> **Qwen Cloud**, tiered by cognitive load: flash for triage, plus for analysis, max for the ruling."

### 1:30–2:15 · Governance — the debate is load-bearing
Show a **DISPUTED** account and the **human approval gate**.
> "Disagreement is a first-class state. A ruling doesn't just change the transcript — it changes the
> work-list: the account's band and its recommended action update. And governance is asymmetric —
> raising risk auto-applies, but *cancelling* a collector call needs human approval."

### 2:15–3:00 · Real data + Alibaba + efficiency
> "This runs on **real Lending Club loan data** in Alibaba **OSS**, validated and loaded through a
> **dlt** pipeline with lineage the Data Engineer cites as evidence, scored by a leakage-guarded
> sklearn model — all on **Function Compute**."

Close on the efficiency line + the live URL:
> "The society scores 100% of the book with a classical model and spends LLM budget only on the
> contested few — a measurable efficiency gain over a one-call-per-account single agent. It's live
> at **app.waspada.xyz**."

---

**Shot list (if editing):** dashboard → society panel mid-debate → a dispute + gate → OSS/FC console
flash → the live URL. **Fallback if the live Qwen run is flaky:** the dashboard ships a committed
fixture that shows a real completed debate transcript — record that instead; it's the same UI.
