# Tech Stack

> Every language, library, and service in WASPADA, and the reasoning behind each
> choice. The through-line: **boring, deterministic, offline-capable** where it
> matters, with the network quarantined behind opt-in flags.

## Backend — Python

| Library | Version | Role |
|---------|---------|------|
| **scikit-learn** | ≥1.5 | the classical PD model (LogisticRegression + calibration) |
| **pandas** | ≥2.2 | feature-matrix wrangling inside the model layer |
| **pyarrow** | ≥17 | the in-memory table format for the frozen data contract |
| **duckdb** | ≥1.0 | in-process SQL engine the Data Engineer / Analyst query |
| **dlt[duckdb]** | ≥1.4 | the real load pipeline (merge + schema contract + lineage) |
| **oss2** | ≥2.18 | Alibaba OSS client (portfolio Parquet, model binaries) |
| **openai** | ≥1.0 | points at DashScope's OpenAI-compatible endpoint (Qwen) |
| **mcp** | ≥1.0 | Model Context Protocol server/client for evidence tools |
| **aliyun-log-python-sdk** | ≥0.9 | SLS audit stream (fail-safe local fallback) |
| **FastAPI** + **uvicorn** | ≥0.115 / ≥0.30 | the API + ASGI server on Function Compute |
| **PyJWT**, **passlib[bcrypt]**, **bcrypt** | — | JWT auth (bcrypt pinned `<4.1` — passlib compat) |
| **pymysql** | ≥1.1 | RDS MySQL driver for the auth store |
| **python-dotenv** | ≥1.0 | local `.env` loading |

> **GPU note:** `cudf-cu12` / `cuml-cu12` are declared in the root
> `requirements.txt` but the GPU path is **on hold**; the active + deployed path is
> **CPU sklearn**. The deployed `api/requirements.txt` is deliberately CPU-only and
> lean.

## Frontend — TypeScript / React

| Library | Version | Role |
|---------|---------|------|
| **React** | 18.3 | the dashboard SPA |
| **Vite** | 5.4 | dev server + build |
| **TypeScript** | 5.6 | the whole frontend; mirrors the frozen contract in `types.ts` |

**Zero runtime UI dependencies** — no component library, no CSS framework, no chart
lib. Everything is hand-rolled on a **token-driven design system**
(`styles/tokens.css`): the debate flow-chart is hand-rolled SVG/CSS (not React
Flow), the theme is cloud-blue tokens, bilingual EN / 简体中文 via a tiny custom
i18n. This keeps the bundle small (~200 KB JS) and the design fully in our control.

## Data & ML

- **Frozen contract**: dataclass-backed Arrow tables (`RawLoans` → `FeatureFrame` →
  `ScoredAccounts` → `DashboardPayload`).
- **Medallion**: OSS Bronze/Silver/Gold, date-partitioned.
- **Model**: sklearn LogisticRegression + isotonic calibration + a model registry.
  See [ML Governance](09-ml-governance.md).

## Cloud — Alibaba

| Service | Role |
|---------|------|
| **Object Storage Service (OSS)** | the portfolio data lake + model binaries |
| **Function Compute (v3)** | the FastAPI backend (custom container, port 8080) |
| **Container Registry (ACR)** | the backend image |
| **ApsaraDB RDS (MySQL 8.0)** | the auth store |
| **Simple Log Service (SLS)** | the queryable audit stream |
| **RAM** | roles/policies (STS-scoped OSS/SLS/ACR access) |
| **VPC / DNS** | networking + the custom domain |

Provisioned by **OpenTofu / Terraform** (`deploy/iac/`). See
[Alibaba Cloud Infra](07-alibaba-cloud-infra.md).

## AI / Reasoning

- **Qwen** (via Qwen Cloud / DashScope, OpenAI-compatible mode) — the debate brains,
  tiered `flash` / `plus` / `max` by cognitive load.
- **MockLLM** — the deterministic offline brain the whole system runs on for tests.

See [LLM / Qwen Model](08-llm-qwen-model.md).

## Tooling & workflow

- **pytest** — 530+ tests, all offline/deterministic (no creds, no network, no GPU).
- **Git worktrees** (`scripts/wt.sh`) — per-agent isolated checkouts to avoid the
  shared-working-directory collisions two coding agents caused early on.
- **GitHub Actions** — image build/push on release to `main`.
- Runtime: **Git Bash** (POSIX) on Windows for dev; Linux container in production.

## Why this stack

- **Determinism over magic**: sklearn + a frozen contract + offline mocks mean the
  whole system is reproducible and testable without any cloud.
- **In-process over services**: DuckDB (not a warehouse), the model in-process (not
  PAI-EAS) — $0 marginal cost, no extra failure surface.
- **Opt-in network**: OSS / dlt / SLS / Qwen all have guarded fallbacks, so "it works
  on my laptop with no credentials" is literally true.

**Related:** [Alibaba Cloud Infra](07-alibaba-cloud-infra.md) ·
[LLM / Qwen](08-llm-qwen-model.md) · [Data Architecture](01-data-architecture.md)
