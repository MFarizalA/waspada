# HACKATHON.md — WASPADA · Loan-Risk Decision-Support for a Multifinance Lender

**WASPADA** (Indonesian: *vigilant / on alert*) = **W**arning **&** **A**pproval
**S**ystem for **P**ortfolio **A**nd **D**efault **A**nalytics — the backronym
spans both lanes: *Approval* (origination) and *Default* (collections/EWS).
*(name locked 2026-07-06.)*

The **submission**: an autonomous **multi-agent decision-support app** for a
multifinance lender's risk analyst — one GPU-accelerated engine spanning **two
decisions in the loan lifecycle**: approving new applications (*origination*) and
catching accounts about to go bad (*collections / early-warning*). **Built by** our
own AI company (Stefanie + Bimo + Kirana + Reza), humans on the sign-off gate at
both layers. *Agents building agents.*

## Two lanes, one engine
Origination and collections are the same problem — *score entities by default
risk → rank → recommend, human approves* — differing at only three points, so we
build one engine + one set of agents and run it in two lanes.

| | **Origination** (new applications) | **Collections / EWS** (existing accounts) |
|---|---|---|
| When | At application | Daily, on the live book |
| Input | Applicant attributes (income, DTI, tenure, amount, collateral) | Payment history (DPD, roll rates, payment ratios, trend) |
| Label | Defaulted over loan life | Rolls to NPL / default in next 30 days |
| Decision | Approve / reject / limit / price | Prioritize collector work-list / intervene |
| Output | Application risk score + recommendation | Account risk score + ranked work-list + alerts |

**Shared spine (build once):** the agents, BigQuery ingest, the cuDF+cuML GPU
pipeline, the ranking/dashboard/alert layer, and the CPU-vs-GPU benchmark. **Per
lane (small):** which features, which label, how the recommendation is phrased.
**Sequencing:** build the shared engine + **Collections/EWS first** (sharpest
bottleneck, biggest data → best acceleration number), then add **Origination** as
an additive second lane. Data: LendingClub→origination, Freddie Mac panel→collections.

## The real user & problem
- **User:** a risk / collections **data analyst** at an Indonesian multifinance
  lender (consumer installment financing — motorbikes, appliances, multipurpose).
- **Bottleneck (data-dependent, specific):** every day the analyst must tell the
  collections team **which accounts are about to roll into NPL and how to spend
  limited collector capacity.** Today = pulling millions of payment rows and
  grinding cleaning + roll-rate + scoring in pandas/SQL/Excel → hours → the
  work-list is stale before it's used.
- **Decision it drives:** which at-risk accounts to prioritize this cycle, and
  which portfolio segments to flag.

## The pipeline (ingest → clean → analyze → model → visualize)
1. **Ingest** loan + payment/installment data from **BigQuery**.
2. **Clean & feature-engineer** with **cuDF** — DPD buckets, roll rates, payment
   ratios, tenure, product/region, delinquency trend. *(the GPU step)*
3. **Model** roll-to-NPL / default probability per account with **cuML**.
4. **Rank & segment** the portfolio.

## The output (decision-support artifacts)
- Per-account **risk score** (P(roll to NPL in next 30 days)).
- A ranked **collections work-list** (top-priority accounts for collectors).
- A **portfolio early-warning dashboard** (NPL ratio, roll rates, vintage,
  by product/region/branch).
- Segment **alerts** when a cohort deteriorates.

## Acceleration evidence (the "why GPU" proof)
A concrete **CPU-vs-GPU benchmark** in the demo: same pipeline on pandas+sklearn
vs cuDF+cuML over N million rows → *seconds vs minutes*, i.e. **same-day / on-
demand risk instead of an overnight batch.** That's "lower time-to-insight +
larger data scale + better operational responsiveness" — verbatim from the rubric.

## The multi-agent layer (what judges score)
- **Orchestrator** (primary) — plans the run, coordinates, reports to the analyst.
- **Ingest agent** — BigQuery pull, freshness/schema checks.
- **Analytics agent** — cuDF cleaning + feature engineering.
- **Risk-Model agent** — cuML scoring / roll-rate prediction.
- **Insight agent** — work-list + dashboard data + plain-language alerts.
- **Human (analyst)** — reviews scores, sets thresholds, approves the work-list.
  *(humans in control ✓)*

## Required stack (need ≥2; we use 2–3, all ~$0)
- **BigQuery** (Google Cloud) — the data warehouse / ingest source. Free sandbox.
- **cuDF + cuML / RAPIDS** (NVIDIA) — the GPU pipeline. Runs on the MX570 via WSL2.
- *(optional 3rd)* **Gemini** (free AI Studio tier) — the agents' reasoning brains.

## Data (real portfolios are private → public analog)
- **Start:** LendingClub (~2.2M consumer loans, with status/payment labels) —
  right shape for roll-to-default modeling. Load into BigQuery.
- **Scale flex:** Freddie Mac loan performance (tens of millions of rows) for the
  big GPU benchmark — or a synthetic Indonesian installment portfolio.

## How the company builds it — RESOLVED (2026-07-06)
Option A: **author-in-company, run-in-WSL.** The GPU lives in **WSL** (proven:
cuDF/cuML 26.06 run on the MX570); the worker containers have none. So the three
workers now run on **`terminal.backend: local`**, writing code straight into
`C:/Users/afkar/Developer/waspada`, and invoking WSL for GPU/data steps
(`wsl -e /root/rapids/bin/python …`). Tradeoff accepted by the owner: this relaxes
the container walls for the hackathon deliverable (own machine, public data, human
gate). Option B (GPU-enable a container via `--gpus all`) deferred.

## Build phases (small tickets — throttle-safe)
0. **De-risk** ✅ — GPU-in-WSL proven; cuDF/cuML 26.06 installed + smoke-tested on
   the MX570. Remaining de-risk: BigQuery sandbox + confirm a query.
1. **Foundation** — repo `Developer/waspada` (git + .gitignore + requirements) ✅;
   workers on local backend ✅. Remaining: acquire LendingClub → BigQuery.
2. **Pipeline** (Bimo) — ingest → cuDF features → cuML model → scores/ranking +
   the CPU-vs-GPU benchmark.
3. **Agents** (Bimo) — orchestrator + specialized agents wrapping the steps,
   human-in-loop thresholds/approval.
4. **Dashboard** (Kirana) — analyst-facing EWS view (work-list, scores, portfolio
   health). React/TS or Looker.
5. **QA** (Reza) — data validation, model sanity, pipeline tests, data-leak check.
6. **Submission** — benchmark numbers, demo video, README, the meta-story.

## Open questions (refine with the colleague when free)
- Real fields the analyst actually uses; real NPL/DPD thresholds; how collectors
  receive the work-list.
- (Both lanes confirmed wanted; deadline de-prioritized by owner.)

## Risks
- **Z.ai 429 throttle** — build in small tickets; off-peak.
- **4 GB VRAM** — size/chunk data; Colab T4 for the big benchmark.
- **Weak speedup on trivial ops** — a 5M-row groupby is ~1.1× (CPU already fast);
  the real GPU win needs heavier compute (joins, many-feature engineering, tree
  models). Design the benchmark around a CPU-stressing workload.
- **BigQuery/GCP setup friction** — sandbox is free but needs a project + auth.
