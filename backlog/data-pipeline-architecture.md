---
id: data-pipeline-architecture
state: design
owner: claude
designed: 2026-07-19
scope: the end-to-end data pipeline — source → OSS → WASPADA → debate — architecture, modeling, and integration with the current condition
related: WA-047 (read resolver), WA-078 (loader), WA-082 (model), WA-083 (dlt), WA-087 (secrets), WA-088 (batch); NEW: WA-089 (pluggable source), WA-090 (medallion activation)
---

# Data pipeline — source → OSS → WASPADA → debate

## 0. TL;DR
WASPADA has all the *pieces* of a data pipeline but no cohesive **source→OSS→society** design.
This doc defines it: a **medallion** flow (Raw/Staging/Mart on OSS) fed by a **pluggable source
layer** (Lending Club CC0 today; synthetic-SDV and Bondora as options), landed **date-partitioned**
via **dlt**, refreshed on a **schedule (FC time trigger)**, read by the society through a
**latest-partition resolver**, and consumed by the debate as scored accounts + cited evidence.
Two new tickets close the uncovered pieces: **WA-089** (pluggable source) and **WA-090** (activate
the dead Staging/Mart tiers). Everything is $0-runtime and Alibaba-native.

## 1. The end-to-end flow
```
                       ┌──────────────── PRODUCER (batch, scheduled) ────────────────┐
 SOURCE  ──extract──▶  TRANSFORM→RawLoans ──validate──▶  LAND: OSS Raw (Bronze)
 (LC CSV /            (map source schema      (frozen        loans/dt=<YYYYMMDD>/loans.parquet
  SDV synthetic /      → RawLoans)             contract)     via dlt: merge on loan_id + _dlt_loads lineage
  Bondora / DB)                                             │
                       └───────────────────────────────────┼──── FC Time Trigger (daily) ─────────┘
                                                            │
                       ┌──────────────── CONSUMER (on-demand /api/run OR scheduled pre-score) ─────┐
                        READ: oss.py resolves LATEST dt= partition (Bronze)
                          │
                          ▼  Data Engineer  — validate + null/anomaly gate + cite freshness/lineage
                          ▼  Data Analyst   — FeatureFrame + aggregates ──write──▶ OSS Staging (Silver)
                          ▼  Risk Model     — score → ScoredAccounts
                          ▼  Society debate — Auditor challenge → Actuary rebut → Arbiter rule → adjudicate
                          ▼  Insight        — DashboardPayload ──write──▶ OSS Mart (Gold)
                          ▼  Dashboard      — reads Mart payload (instant) OR the live run
```

## 2. Data modeling — the medallion + the frozen contract
The **frozen data contract** (`schema.py`) *is* the data model; the medallion is where each stage lands.

| Tier | Bucket | Contents | Producer | Status today |
|---|---|---|---|---|
| **Bronze (Raw)** | `waspada-prod-raw` | `RawLoans` parquet, `loans/dt=<batch>/` | ingest loader / dlt | ✅ live (but flat `loans.parquet`, single object) |
| **Silver (Staging)** | `waspada-prod-staging` | `FeatureFrame` + analyst aggregates | Data Analyst | ❌ **dead** (provisioned, never written) |
| **Gold (Mart)** | `waspada-prod-mart` | `ScoredAccounts` + `DashboardPayload` | Insight | ❌ **dead** (provisioned, never written) |

Contract flow (unchanged, additive-column-safe):
`RawLoans → FeatureFrame → ScoredAccounts → DashboardPayload`. The debate reads **ScoredAccounts**
(+ analyst aggregates as MCP-cited evidence, + the model's `explain()` drivers). Adjudication
appends `final_band`/`override_reason` as additive columns; the served action derives from `final_band`.

## 3. Source layer — pluggable (WA-089)
The "source" is currently hard-wired to the Lending Club CSV. Make it **pluggable** so the same
downstream pipeline runs on any source, each **mapping to the frozen `RawLoans`** contract:

| Source | License | Fit | Mapping effort |
|---|---|---|---|
| **Lending Club** (default) | CC0 | consumer loans = collections | done (WA-078 loader) |
| **Synthetic (SDV / built-in)** | none (no real data) | any shape we define | trivial; realism via SDV trained on CC0 LC |
| **Bondora** | public | EU P2P, cross-sectional | moderate remap |
| **SQL DB** (future prod) | — | core-banking/LMS | dlt SQL source + incremental cursor/CDC |

Design: a `Source` abstraction (or dlt sources) selected by **`WASPADA_DATA_SOURCE`**
(`lending_club` | `synthetic` | `bondora` | `sql`); each yields a `RawLoans`-conformant Arrow table.
Downstream (features → score → debate) is **source-agnostic** because it only sees `RawLoans`.

## 4. Ingestion — source → OSS (producer)
- **Transform + validate:** map source schema → `RawLoans`; `validate_table(RawLoans)` is the gate.
- **Land partitioned:** write `loans/dt=<YYYYMMDD>/loans.parquet` (owner convention, WA-088). Each
  batch is a new immutable partition (auditable, backfillable, rollback = point at the prior partition).
- **dlt (WA-083):** the load runs through dlt — `merge` dedup on `loan_id`, schema contract (freeze),
  `_dlt_loads` lineage (freshness + rows-loaded the Data Engineer can cite). Incremental cursors load
  only new/changed loans (matters at scale).
- **Schedule (WA-088):** an FC **Time Trigger** (`CRON_TZ=Asia/Jakarta 0 0 6 * * *`) runs the producer
  daily → a fresh partition. Fail-safe: a failed run leaves the last-good partition; alert via SLS.
- **Secrets (WA-087):** the producer uses the FC **RAM role** for OSS (no static keys).

## 4b. dlt across the pipeline (the load backbone)
dlt is the **load engine end-to-end** — the merge / schema-contract / lineage / incremental backbone.
Every place data moves *into* a store, dlt drives it:

| Stage | dlt's role | Ticket |
|---|---|---|
| **Source → OSS Raw** (ingest) | dlt source (filesystem / SQL / API) → **dlt filesystem destination on OSS**; `merge` dedup on `loan_id`, schema-contract (freeze), `_dlt_loads` lineage, **incremental cursors** (only new/changed loans) | WA-083 |
| **OSS → DuckDB** (read for the Data Engineer) | dlt **Option A** (oss2 read → Arrow → dlt → DuckDB) — the read carries a real contract + lineage, not a bare load | WA-083 |
| **Features → Staging (Silver)** | dlt **filesystem destination** (Option B) → OSS Staging | WA-090 |
| **Scheduled refresh** | the FC time-trigger runs **the dlt pipeline** each batch → a fresh `dt=` partition | WA-088 |
| **Debate evidence** | the Data Engineer cites dlt `_dlt_loads` **freshness/lineage + contract-pass** as data-trust evidence in the society's argument | WA-084 (done) |

**Design vs current (honesty):** the above is the **design**. In the **current code** dlt is still
**declared-but-unused** — a dependency never imported (the WA-047 scaffold was removed for the exact
`dlt.readers.filesystem` bug). **WA-083** is what makes it real, and it is **PoC-proven** in
`backlog/WA-047-dlt-research.md` (merge-dedup + contract-reject + `_dlt_loads` audit rows, verified
against dlt 1.28.2). So: dlt is the designed backbone and proven feasible; implementation lands in WA-083.

## 5. Read — OSS → WASPADA (consumer)
- **Latest-partition resolver (WA-047):** `oss.py` resolves the newest `loans/dt=*/` partition instead
  of a fixed `OSS_KEY`. `as_of`/pinned-partition override for reproducible/backfill runs.
- **Quality gate:** the Data Engineer validates + profiles the read book and **cites freshness +
  `_dlt_loads` lineage** as data-trust evidence in the debate (WA-084 native tools already wired).
- **Pushdown (future):** `httpfs`/`read_parquet('s3://…')` to avoid the full bulk download (WA-047 §read-path).

## 6. How the debate consumes it (OSS → society)
1. Data Engineer reads Bronze, gates it (dirty → BLOCKED), publishes `raw_loans`.
2. Data Analyst builds `FeatureFrame` + aggregates → publishes them (and, WA-090, writes Silver).
3. Risk Model scores → `ScoredAccounts` (+ `explain()` drivers).
4. Risk Auditor audits the stratified K-slice, opens disputes; Actuary rebuts; Arbiter rules;
   adjudication writes `final_band` back → the work-list changes.
5. Insight assembles `DashboardPayload` → (WA-090) writes Gold; the dashboard reads it.
The **data quality/freshness/lineage** from steps 1–2 become **cited evidence** in the debate —
the pipeline's provenance feeds the society's arguments, closing the loop.

## 7. Current condition → target (integration / migration)
| Concern | Current | Target | Ticket |
|---|---|---|---|
| Source | hard-wired LC CSV | pluggable (`WASPADA_DATA_SOURCE`) | **WA-089** |
| Land layout | flat `loans.parquet` | `loans/dt=<batch>/` partitions | WA-088 + WA-047 |
| Load engine | one-shot loader | dlt (merge/contract/lineage/incremental) | WA-083 |
| Refresh | manual upload | FC Time Trigger (daily) | WA-088 |
| Read | fixed `OSS_KEY` | latest-partition resolver | WA-047 |
| Staging/Mart | dead buckets | Data Analyst→Silver, Insight→Gold | **WA-090** |
| Model | retrain per run | versioned artifact in OSS | WA-082 |
| Secrets | static OSS AK + plaintext env | RAM role + KMS | WA-087 |
| Pushdown | bulk download | httpfs/s3 pushdown | WA-047 §read-path |

**Migration order (each independently shippable, no big-bang):**
1. WA-089 pluggable source (keeps LC as default; adds synthetic — instantly publishable/legal-clean).
2. WA-047 partition resolver + re-land current data under `dt=` (unblocks partitioned layout).
3. WA-083 dlt load (Option A) — contract + lineage the Data Engineer cites.
4. WA-090 activate Silver/Gold writes (shared OSS write path).
5. WA-088 schedule the producer; WA-082 model versioning; WA-087 secrets; WA-047 pushdown.

## 8. Principles
- **Source-agnostic downstream** — everything past `RawLoans` never knows the source. Swap LC→synthetic
  →Bondora→DB by changing one selector.
- **Immutable partitions** — each batch is a new `dt=`; never mutate; rollback = repoint.
- **Provenance feeds the debate** — freshness/lineage/contract-status are cited evidence, not just logs.
- **$0 runtime, Alibaba-native** — OSS + dlt (in-process) + FC time trigger + RAM/KMS; no new billed service.
- **Legal-clean by construction** — LC (CC0) or synthetic; no company/PII data ever touches it.
