# WASPADA — Engineering Wiki

**WASPADA** is a loan-risk **collections** decision-support system built around a
**six-agent AI society** that debates the classical model's scores before a human
acts. The name is Indonesian/Malay for *vigilant* — and, fittingly, a level on
Indonesia's national early-warning ladder (*Normal → **Waspada** → Siaga → Awas*).

The product's thesis: **a score you can argue with beats a score you can't.** The
classical PD model scores 100% of the book; a society of agents audits a slice,
opens a bounded debate where they diverge, and a human holds the final gate. Every
claim cites evidence; every run leaves an audit trail.

---

## Pages

| # | Page | What it covers |
|---|------|----------------|
| 01 | [Data Architecture](01-data-architecture.md) | The frozen data contract, the medallion (OSS Bronze/Silver/Gold), dlt + DuckDB, partitioning |
| 02 | [System Architecture](02-system-architecture.md) | The end-to-end system: agents, orchestrator, API, dashboard, cloud |
| 03 | [Harness Architecture](03-harness-architecture.md) | The agent framework — base classes, tools, the LLM surface, the approval gate |
| 04 | [Debate Mechanism](04-debate-mechanism.md) | The three-round adversarial debate, admissibility, adjudication, cost ceiling |
| 05 | [Tech Stack](05-techstack.md) | Every language, library, and service, and why |
| 06 | [Team & Collaboration](06-team-and-collaboration.md) | The two lanes, ownership boundaries, the git worktree workflow |
| 07 | [Alibaba Cloud Infrastructure](07-alibaba-cloud-infra.md) | OSS, Function Compute, RDS, ACR, SLS, RAM — the IaC |
| 08 | [LLM / Qwen Model](08-llm-qwen-model.md) | The reasoning brains, model tiering, native function calling, egress control |
| 09 | [ML Governance](09-ml-governance.md) | The PD model, calibration, drift monitoring, versioning, the parameter matrix |

## The one-paragraph mental model

A deterministic **orchestrator** walks a fixed pipeline of agents. The
**Data Engineer** loads the portfolio from OSS into a DuckDB lakehouse; the
**Data Analyst** builds features + aggregates; the **Actuary** (a classical
sklearn model) scores every account; the **Skeptic** audits a stratified slice
and opens **disputes** where its independent read diverges; the **Arbiter** rules
on each dispute; and where the society is unsure, it **escalates to a human gate**.
The **Insight** agent assembles the dashboard payload. The whole thing runs offline
on a deterministic mock brain, or live on **Qwen**.

## Conventions

- **Frozen contract**: the four data shapes in `waspada/schema.py` are the API
  between every layer. Additive-only.
- **Offline-first**: nothing reaches the network unless a caller opts in. Every
  feature has a guarded fallback so tests + CI are byte-for-byte deterministic.
- **Ticket IDs**: `WA-NNN` map to files in `backlog/`. This wiki cites them so you
  can read the original design.
