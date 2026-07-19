# WASPADA — Submission Description

**Track 3 · Agent Society**
**Live demo:** https://app.waspada.xyz &nbsp;·&nbsp; **Architecture:** [docs/architecture.svg](architecture.svg)
**Proof of Alibaba Cloud usage:** https://github.com/MFarizalA/waspada/blob/main/deploy/iac/main.tf

---

## Paste-ready description (≈420 words)

**The problem.** Loan-collections teams can only work a fraction of their delinquent accounts each day, so deciding *which* borrowers to call — and defending that decision to a credit committee — is high-stakes triage. A lone risk score is easy to produce but hard to trust: it carries no second opinion, no audit trail, and no way to catch the expensive error, the "safe-looking" account quietly rolling into default.

**The agent society.** WASPADA runs a six-member society of specialized agents that produce, challenge, and defend a collections work-list:

- **Data Engineer** — validates and profiles the freshly-loaded loan book (schema, null rates, anomalies) before anyone trusts it.
- **Data Analyst** — builds features and computes the portfolio aggregates the debate later cites as evidence.
- **Actuary** — scores every account with a classical ML model (logistic regression, vintage-split, leakage-guarded) and defends its score when challenged.
- **Risk Auditor** — independently audits a stratified slice of accounts (the riskiest, the boundary cases, and the *contradictory* ones — low score yet distressed) and opens a formal dispute wherever its view diverges from the Actuary's by two or more risk bands.
- **Credit Arbiter** — rules on any dispute the Actuary won't concede.
- **Insight** — assembles the final work-list, portfolio health, and cohort alerts, released only through a **human approval gate**.

**The debate.** Disputes are resolved by a **bounded debate** (at most K×3 rounds): Auditor challenge → Actuary rebuttal → Arbiter ruling, ending in one of four terminal resolutions or escalating to a human. Every claim must cite evidence, served through a real **Model Context Protocol (MCP)** tool interface (`portfolio_stats`, `lookup_account`). Governance is asymmetric: escalations that *raise* risk auto-apply, while de-escalations that would cancel a collector call require explicit human approval — matching the asymmetric cost of the two errors.

**Qwen Cloud.** The society reasons over **Qwen Cloud (DashScope)**, tiered by cognitive load: **qwen3.7-flash** for triage (Data Engineer, Risk Auditor), **qwen3.7-plus** for analysis and defense (Data Analyst, Actuary), **qwen3.7-max** for the Arbiter's rulings — all through native function-calling loops.

**Alibaba Cloud.** FastAPI runs on **Function Compute** (custom container pulled from **Container Registry**); the loan book lives in **OSS** and is read into an in-process **DuckDB**; **RDS MySQL** backs authentication; **SLS** captures every run's audit trail. All infrastructure is declared in OpenTofu (`deploy/iac/main.tf`).

---

<!-- ===================== INTERNAL NOTES — not for the form ===================== -->
## Notes for the submission assembler (WA-076) — do not paste

- **Live URL:** use `https://app.waspada.xyz` (custom domain, renders inline). Do **not**
  use the `*.fcapp.run` URL — it forces a file download on `/` (shared-domain quirk, WA-067).
  HTTPS is pending a free DV cert; if not landed by submission, the http:// URL still renders.
- **Alibaba-proof link:** `deploy/iac/main.tf` is the best single-file proof (OSS + ACR + FC +
  SLS + RDS in one file). The URL above is a **main-branch** link — it only resolves once
  (a) the repo is **public** and (b) the develop→main release has landed (main is currently
  behind). For a stable **permalink**, press `y` on GitHub to pin the commit SHA after the
  release. Until then the link 404s.
- **Names:** this text uses industry names only (Risk Auditor / Actuary / Credit Arbiter) —
  never the internal codenames (Skeptic / Defendant / Judge). Keep it that way in the form.
- **Diagram:** `docs/architecture.svg` is committed with editable SVG source; it renders inline
  on GitHub and is linked from the README architecture section.
