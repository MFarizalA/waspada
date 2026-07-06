# WASPADA

**W**arning **&** **A**pproval **S**ystem for **P**ortfolio **A**nd **D**efault
**A**nalytics — an autonomous **multi-agent, GPU-accelerated risk decision-support
system** for a multifinance lender's data analyst.

WASPADA spans **two decisions in the loan lifecycle on one shared risk engine**:

- **Origination** — approve / reject / price new applications.
- **Collections / Early-Warning (EWS)** — which existing accounts are about to
  roll into NPL, and how to prioritize limited collector capacity.

The **Collections/EWS lane is built end-to-end** (ingest → features → model →
rank → dashboard) with a coordinating multi-agent layer and a human approval
gate. Origination is architected as an additive second lane (deferred).

**Stack:** BigQuery (data warehouse) · cuDF + cuML / RAPIDS (GPU pipeline,
runs in WSL2 on an NVIDIA MX570) · a multi-agent layer (orchestrator + four
specialized agents) over a mockable LLM brain (Gemini-ready, mock by default) ·
React/TypeScript dashboard.

> Built by an autonomous AI software company — Stefanie (PM) · Bimo (backend) ·
> Kirana (frontend) · Reza (QA). Agents building agents, humans on the sign-off
> gate at every layer. See **[HACKATHON.md](HACKATHON.md)** for the full brief.

---

## What it does

Every day a collections analyst must tell the team **which accounts to chase and
how to spend limited collector capacity.** Today that means pulling millions of
payment rows and grinding cleaning + roll-rate + scoring in pandas/SQL/Excel —
hours of work, so the work-list is stale before it's used.

WASPADA automates that loop:

1. **Ingest** the loan book from BigQuery (freshness + schema-checked).
2. **Feature-engineer** with cuDF on GPU (DPD buckets, payment ratios, tenure,
   product/region, delinquency trend).
3. **Model** default probability per account (cuML on GPU; sklearn CPU path
   ships now — GPU is a drop-in).
4. **Rank & segment** → a ranked collections work-list + portfolio health +
   cohort deterioration alerts.
5. **Approve** — a human reviews and releases the work-list (the approval gate).
6. **Visualize** on an analyst-facing dashboard.

A **multi-agent layer** wraps each step: an **orchestrator** plans the run and
coordinates four specialized agents (**ingest → analytics → risk-model →
insight**), each with a clear role, holding the human approval gate before the
work-list is released.

---

## Architecture — two lanes, one engine

Origination and collections are the same problem — *score entities by default
risk → rank → recommend, human approves* — differing only at three points. So
we build one engine + one set of agents and run it in two lanes.

```
                         ┌─────────────────────────────────────────────┐
                         │              ORCHESTRATOR (primary)          │
                         │   plan → run → report · holds the gate       │
                         └──────┬──────────────────────────────────────┘
                                │  AgentContext (artifacts via handles)
            ┌───────────────────┼───────────────────┬──────────────────┐
            ▼                   ▼                   ▼                  ▼
       ┌─────────┐         ┌──────────┐        ┌──────────┐       ┌──────────┐
       │ INGEST  │ ──────▶ │ ANALYTICS│ ─────▶ │RISK-MODEL│ ─────▶│ INSIGHT  │
       │  agent  │  RawLoans│  agent   │FeatureF│  agent   │Scored │  agent   │
       │ BigQuery│  Arrow   │ cuDF/skl │ Arrow  │ cuML/skl │Accts  │ rank+alert│
       └─────────┘         └──────────┘        └──────────┘       └────┬─────┘
                                                                        │
                                                          ApprovalGate ◀──┤
                                                                        ▼
                                                          DashboardPayload (JSON)
                                                                        │
                                                                        ▼
                                                            React/TS dashboard
```

**Frozen data contract** (`waspada/schema.py`) — four types locked once so every
ticket cites the same shapes verbatim:

| Contract | Shape | Built by |
|---|---|---|
| `RawLoans` | one row per loan (cross-sectional snapshot) | WA-002/003 (BigQuery ingest) |
| `FeatureFrame` | per-loan features + `label_default` | WA-004 (cuDF/sklearn features) |
| `ScoredAccounts` | `p_default` + band/segment/action | WA-005 (risk model) |
| `DashboardPayload` | ranked work-list + health + alerts (JSON) | WA-006 (ranking) |

Arrow tables flow between stages; `schema_from_dataclass` + `validate_table`
assert every hand-off matches the contract (drift fails loud, not silent).

---

## The multi-agent layer

The agent substrate (`waspada/agents/`) is lane-agnostic and runs **offline by
default** (deterministic `MockLLM`; Gemini is an opt-in via `WASPADA_LLM_PROVIDER=gemini`).

- **Orchestrator** (`Orchestrator`) — the primary agent. `plan(lane)` builds the
  step sequence; `run()` executes ingest→analytics→risk-model→insight, threading
  artifacts via `AgentContext`; `report(payload)` writes a plain-language
  analyst summary. A failure in any stage surfaces (not swallowed).
- **Ingest / Analytics / Risk-Model / Insight agents** — thin wrappers over the
  pipeline components, each producing its contract artifact.
- **ApprovalGate** — the human-in-loop checkpoint. Before the work-list is
  released the gate must approve. `WASPADA_AUTO_APPROVE=1` short-circuits to
  approve **but logs it `auto=True`** so an audit can tell a rubber-stamp from a
  real human sign-off. *Humans in control.*

Every agent records auditable `Step`s; the orchestrator records `Handoff`
envelopes (frm→to) so a full run can be reconstructed.

---

## Acceleration evidence — the "why GPU" proof (measured, honestly)

The CPU-vs-GPU benchmark lives in [`data/benchmark.json`](data/benchmark.json),
run on the real LendingClub-derived snapshot (336,916 train rows, 27 features):

| Stage | CPU (pandas/sklearn) | GPU (cuDF/cuML) | Speedup |
|---|---|---|---|
| BigQuery ingest | 35.91 s | — | (I/O bound; same path) |
| Feature engineering | 0.268 s | 0.341 s | **0.79× (GPU slower)** |
| Model training | 8.53 s | 7.30 s | **1.2×** |

**What this honestly shows:** at this data volume and feature count, the GPU
does not yet win on feature engineering (cuDF's per-op overhead exceeds the
compute saved on ~337k rows × 27 features) and only marginally wins on a linear
model fit. The HACKATHON brief flags this risk explicitly: *"Weak speedup on
trivial ops — the real GPU win needs heavier compute (joins, many-feature
engineering, tree models)."* The benchmark harness to stress heavier workloads
(WA-007) and a GPU tree model are on hold; the CPU path ships now and the GPU
estimators are a drop-in (`train`/`predict` keep their signatures). The
acceleration story is **architected and measured**, not claimed — the honest
number today is "~1.2× on training; GPU feature engineering is slower at this
scale pending a heavier workload."

---

## Project layout

```
waspada/
├── schema.py              # FROZEN data contract (RawLoans, FeatureFrame, ScoredAccounts, DashboardPayload)
├── config.py              # env/lane loading (collections | origination)
├── wsl.py                 # run_gpu() helper for the WSL/cuML path
├── data/bq.py             # BigQuery client → RawLoans-shaped Arrow (WA-002)
├── features/collections.py# cross-sectional FeatureFrame + label (WA-004)
├── model/risk.py          # sklearn LogisticRegression, vintage split, no-leakage (WA-005, CPU)
├── insight/ranking.py     # rank + portfolio health + alerts + payload (WA-006)
└── agents/
    ├── base.py            # Agent base + ApprovalGate (WA-008)
    ├── protocol.py        # AgentContext / AgentResult / Handoff / Step / Status
    ├── llm.py             # MockLLM (offline) / GeminiLLM (lazy SDK import)
    ├── ingest.py          # IngestAgent — wraps BigQuery (WA-009)
    ├── analytics.py       # AnalyticsAgent — wraps features (WA-009)
    ├── risk_model.py      # RiskModelAgent — wraps model.risk (WA-009)
    ├── insight.py         # InsightAgent — wraps ranking + gate (WA-009)
    ├── orchestrator.py    # Orchestrator — plans/runs/reports (WA-010)
    └── __main__.py        # CLI: python -m waspada.agents (WA-010)
dashboard/                 # React/TS EWS dashboard (Kirana, WA-011)
gpu/                       # WSL entry points for cuDF/cuML (on hold)
tests/                     # 101 passing tests (1 skipped: live BQ smoke)
data/benchmark.json        # measured CPU-vs-GPU numbers
backlog/                   # ticket specs (WA-001..WA-012)
```

---

## Quick start

### Prerequisites
- Python 3.11+ (developed on 3.11; tested on 3.12)
- A virtualenv
- (optional) Google Cloud creds for live BigQuery; (optional) an NVIDIA GPU +
  WSL2 for the cuDF/cuML path; (optional) a Gemini API key for the real LLM brain

### Install
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# GPU deps need the NVIDIA index (Linux/WSL only):
# pip install --extra-index-url=https://pypi.nvidia.com -r requirements.txt
```

### Configure
```bash
cp .env.example .env
# Fill in BQ_PROJECT / BQ_DATASET / BQ_TABLE / GOOGLE_APPLICATION_CREDENTIALS
# (optional) WASPADA_LLM_PROVIDER=gemini + GEMINI_API_KEY
# (optional) WASPADA_AUTO_APPROVE=1 for smoke runs
```

### Run the pipeline (CLI)
```bash
# Offline (no BQ creds): runs end-to-end on a synthetic snapshot, writes the payload.
python -m waspada.agents --lane collections --auto-approve --top-n 50

# Output (stdout): the analyst report.
# Output (data/dashboard-payload.json): the DashboardPayload the dashboard reads.
```

### Run the dashboard
```bash
cd dashboard && npm install && npm run dev
# Loads the payload from fixtures (or the generated data/dashboard-payload.json).
```

### Run the tests
```bash
python -m pytest tests/ -v          # 101 passed, 1 skipped (live BQ smoke)
```

---

## Key design decisions

- **Frozen contract, validated at every seam.** `waspada/schema.py` is the single
  source of truth; Arrow tables are validated against it at each hand-off.
- **No outcome leakage.** `label_default`, `delinquency_status`, and
  `current_status` are explicitly **excluded** from model features
  (`LEAKAGE_EXCLUDED`); a test documents the rule.
- **Vintage split.** Train on older origination cohorts, test on newer — an
  out-of-time-ish check (issue year is reconstructed from `loan_age` + `as_of_date`
  because the frozen `FeatureFrame` carries no `issue_date`).
- **Offline by default.** The framework runs end-to-end on `MockLLM` with no
  network; tests block sockets to prove it. Gemini is opt-in.
- **Humans in control.** The `ApprovalGate` blocks the work-list release; an
  auto-approve is logged distinctly so an audit can tell it from a real sign-off.
- **CPU ships now, GPU is a drop-in.** The CPU path (sklearn) is the production
  path today; `train`/`predict` signatures are identical so a cuML estimator swaps
  in without touching the agents.

---

## Status

| Ticket | Deliverable | Status |
|---|---|---|
| WA-001 | Repo scaffold + frozen data contract | ✅ done |
| WA-002 | BigQuery ingest layer (Arrow client) | ✅ done |
| WA-003 | LendingClub → BigQuery mapping | ✅ done |
| WA-004 | Collections features + label (cuDF + pyarrow) | ✅ done |
| WA-005 | Risk model (CPU adaptation) | ✅ done |
| WA-006 | Ranking, segmentation, work-list, alerts | ✅ done |
| WA-007 | CPU-vs-GPU benchmark harness | ⏸ on hold (GPU) |
| WA-008 | Agent framework + ApprovalGate | ✅ done |
| WA-009 | Pipeline agents (ingest/analytics/risk-model/insight) | ✅ done |
| WA-010 | Orchestrator + CLI | ✅ done |
| WA-011 | React/TS dashboard | ✅ done (Kirana) |
| WA-012 | QA / data-leak check | (Reza) |

---

## License & data

Built for the Gen AI Academy APAC hackathon. Data is the public LendingClub loan
snapshot (no private portfolios). Secrets and credentials are never committed
(see `.gitignore`); `models/`, `*.pkl`, and `/data/` dumps are gitignored.
