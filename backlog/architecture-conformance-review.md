---
id: architecture-conformance-review
state: review-complete
owner: claude
reviewed: 2026-07-19
method: 5 parallel read-only layer audits (society / debate+governance / data+lakehouse / infra / cross-cutting), reconciled against origin branch topology
question: does the CURRENT architecture (code) equal the PLANNED architecture (HACKATHON.md / README / backlog)?
---

# Architecture conformance review — planned vs actual

## Verdict

**Where it counts, yes — the built architecture equals the plan.** The agent society,
the bounded debate, its governance, the frozen contract, the ML model + its
introspection, the policy layer, the audit trail, and auth are all **real,
load-bearing, and built as designed** — verified at file:line across five
independent audits. This is **not agent-washing**: the arithmetic is genuinely
deterministic (sklearn/DuckDB/pandas, never LLM), the debate ruling actually
changes the work-list (WA-048), the model's `explain()` internals actually feed
both debate voices, and the human gate actually fails closed.

**The divergence is real but concentrated, and mostly honest.** It clusters in the
**data/infra layer**, and separates cleanly into three kinds: (1) **doc-vs-reality
drift** — mostly *fixed-pending-merge* on unmerged branches; (2) **provisioned-but-
unused scaffolding** the marketing docs oversell but the *code* honestly caveats;
(3) a short list of **genuine gaps** — two that touch judging (native-function-calling
on 2/3 agents; the un-run benchmark baseline) and three operational foot-guns (a
stale deployed image, one branch-only Terraform file, a missing bcrypt pin in root).

> **Reconciliation note (important):** the five subagents read the local working
> tree (on `feature/wa-077-real-data-cutover`, `ce3cd1b`), which lags both develop
> and several pushed-but-unmerged fix branches. Every finding below is
> **reconciled against `origin` topology** (verified 2026-07-19):
> `origin/develop=1a9a514`, `origin/main=bdf98fd`. Two of the agents' scariest
> findings were **stale** and are corrected here (WA-080; rds_grant).

---

## Per-layer conformance

| Layer | Verdict | Headline |
|---|---|---|
| **Agent society + orchestration** | ✅ MATCH | 6 members wired, deterministic spine, exact flash/plus/max tiering, two lanes, DISPUTED first-class. One real gap: native function calling (below). |
| **Debate protocol + governance** | ✅ MATCH (load-bearing) | 3 rounds, 4 resolutions, ≥2 admissibility, stratified audit, WA-048 write-back, asymmetric gate, WA-026 memory, fail-closed. Gaps are in cost-claim precision + MCP transport, not the logic. |
| **Cross-cutting (contract/model/policy/audit/auth)** | ✅ MATCH (disciplined) | Frozen contract + additive columns, sklearn + vintage split + leakage guard + real `explain()`, policy layer, triple-swallowed SLS audit, startup secret guard. Gaps: benchmark baseline, bcrypt pin. |
| **Data + lakehouse** | 🟠 PARTIAL | Contract + Raw read + DuckDB engine are MATCH; pushdown/medallion-write/dlt are disclosed 🟡 or dead scaffolding. "Wide in ambition, narrow in deception." |
| **Deployment + infra (IaC)** | 🟠 PARTIAL | FC/OSS/SLS/CI backbone MATCH; but *deployed ≠ code ≠ what main can safely apply.* Stale image + one branch-only .tf + doc drift. |

---

## Divergence register (reconciled)

### A. Genuine open gaps — on develop, fixed nowhere yet
| # | Gap | Sev | Touches | Owner |
|---|-----|-----|---------|-------|
| A1 | **Deployed FC image is stale** (`fc_image_tag=0ae6be8`, predates WA-077/WA-080). Live prod serves the synthetic n=200 stub while the repo claims real OSS data. | 🔴 demo-critical | the product's central "real data" claim | deploy lane (Stefanie) |
| A2 | **`custom_domain.tf` is develop-only** (main:0 / develop:1). A `tofu apply` from `main` (the deploy branch) plans to **destroy `app.waspada.xyz`**. *(rds_grant landmine is RESOLVED — now on main via `bdf98fd`.)* | 🔴 landmine | live demo URL | deploy lane |
| A3 | **Single-agent benchmark baseline never run** (`AGENT_SOCIETY_BENCH.json: single_agent_baseline.status="not_run"`). Track 3 explicitly requires "a measurable efficiency gain over single-agent baselines." Society arm is measured; baseline is empty (openai SDK absent). Honest, but the headline is unsubstantiated. | 🔴 credibility | a stated judging criterion | backend |
| A4 | **Native function calling is real for only 1 of 3 loop agents.** Only `risk_auditor.py` uses the native `chat()`/`tools`/`tool_calls` surface; `data_engineer.py`/`data_analyst.py` use `complete()` + regex-parse a `{"tool","arg"}` blob (they loop, but don't pass `tools=`). Docs claim all three use "the same native function-calling loop." | 🟠 rubric | "sophisticated function-calling" axis | backend |
| A5 | **MCP at runtime is InProcess-and-conditional, not stdio.** Live debate cites evidence via `InProcessClient`, only when the Data Analyst emitted aggregates — else the auditor silently falls back to local stubs (no MCP evidence). The genuine `StdioClient` round-trip runs only in a verify script. | 🟠 rubric | "MCP integration" headline | backend |
| A6 | **Root `requirements.txt` lacks the `bcrypt<4.1` pin** that `api/requirements.txt` has. passlib×bcrypt≥4.1 breaks every password hash. (My quickstart fix points judges at `api/requirements.txt`, so the judge path is safe — root is still latently broken.) | 🟠 correctness | anyone installing root reqs | backend |
| A7 | **"≤K×3 LLM calls" cost claim understated.** R1 is a ≤4-turn tool loop, so true worst case ≈ **K×6**. Still bounded + deterministic; the specific number is optimistic. | 🟡 doc precision | rubric §bounded debate | docs |
| A8 | Low/disclosed: model retrained per-run, persistence unwired (**WA-052** / extended by **WA-082**); LGD hardcoded `0.45`; false-negative catcher is brain-gated (needs `brain=qwen`); batched (not per-account) human gate; single flat OSS object + no pushdown (**WA-047**). | 🟡 | — | various |

### B. Aspirational scaffolding — provisioned/coded but unused (honest in code, oversold in docs)
| # | Item | Note |
|---|------|------|
| B1 | **OSS Staging/Mart buckets + `fc_oss_write` PutObject policy** — dead (no `put_object` anywhere in `waspada/`). Contradicts HACKATHON's "the RAM policy is read-only." | **Activated by the shared OSS-write-path** in WA-082/WA-083. |
| B2 | **DuckDB-on-RDS federation** (`get_analytics_connection`, `DUCKDB_RDS_ENDPOINT`) — self-labeled "unreachable in production," no caller, no `api/main.py` ref. | Env plumbing + IaC for a path the code never runs. |
| B3 | **dlt** — declared dependency (`requirements.txt:15`), never imported. Scaffold removed (WA-047); docs de-claimed. | **WA-083** to make it real. |

### C. Fixed-pending-merge — corrected on pushed branches, not yet on develop
| # | Item | Branch |
|---|------|--------|
| C1 | README/HACKATHON **"RDS PostgreSQL" → MySQL** | `feature/submission-critical-doc-fixes` |
| C2 | **Quickstart install** (`requirements.txt` unrunnable → `api/requirements.txt`) | `feature/submission-critical-doc-fixes` |
| C3 | **dlt-as-live-path prose sweep** + stale `README:306 "✅ lakehouse (dlt + DuckDB)"` | `feature/wa-073-submission-docs` |
| C4 | rds_grant.tf on main (landmine half) | already merged (`bdf98fd`) |

### D. Stale subagent findings — corrected by reconciliation
- **"No audit parallelization / WA-080 missing"** → FALSE. WA-080 is on `origin/develop` (14 `max_workers`/ThreadPool hits in `risk_auditor.py`). The agents read the pre-WA-080 WA-077 branch.
- **"rds_grant.tf develop-only (CRITICAL)"** → FALSE now. It's on `origin/main` (`bdf98fd`). Only `custom_domain.tf` remains branch-only.
- **"WA-073 fixed docs to MySQL"** (session hint) → the MySQL/dlt doc fixes exist only on **unmerged** branches (C1/C3); develop still shows PostgreSQL + dlt.

---

## What to do (mapped to tickets)

1. **Merge the fix branches to develop** (C1–C3) — clears the PostgreSQL, quickstart, and dlt-prose divergences in one sweep. *Low risk, high payoff; several A-items evaporate.*
2. **Deploy a current image + carry `custom_domain.tf` to main** (A1, A2) — deploy lane; the two demo-critical operational items. Apply-only-from-develop until main is reconciled.
3. **Run the single-agent baseline** (A3) — needs the openai SDK / a Qwen run; unsubstantiated Track-3 claim otherwise. Consider a scripted deterministic baseline if live Qwen is unavailable.
4. **Decide on native function calling for DE/DA** (A4) — either wire `data_engineer`/`data_analyst` onto the native `chat(tools=…)` surface (the client already supports it), or soften the docs to "function-calling loop (native for the Risk Auditor; prompt-driven for the data agents)." The honest cheap fix is the doc; the rubric-stronger fix is the code.
5. **Add the `bcrypt<4.1` pin to root `requirements.txt`** (A6) — one line; removes a latent prod-fatal.
6. **Reword the K×3 cost claim** (A7) and **the service-count** (4 vs 5) — doc precision.
7. **Stop advertising B1–B3 as delivered** — either build them (WA-082/083 activate the write path + dlt) or mark them 🟡 in the *docs* the way the *code* already does.

**Bottom line:** the society architecture conforms; the data/infra layer is where the plan outran the code, and it's mostly disclosed. The demo-critical risks are **operational** (stale deploy, one branch-only .tf), not architectural — and the highest-leverage single action is **merging the already-written fix branches into develop.**
