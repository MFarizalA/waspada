# Deployment video scenario (~1–2 min) — Alibaba Cloud proof

**Goal:** prove WASPADA genuinely runs on Alibaba Cloud — 5 services, IaC, live. Screen recording of
the Alibaba Cloud console + the repo. Short and factual.

**Setup:** log into the Alibaba Cloud console (Singapore / ap-southeast-1); have the repo's
`deploy/iac/main.tf` open in a tab.

---

### 0:00–0:20 · The services (console tour)
Click through, a few seconds each:
- **OSS** → the buckets `waspada-prod-raw` / `-staging` / `-mart`; open `loans.parquet` (the real
  50k-row Lending Club book — the data door).
- **Container Registry (ACR)** → the `api` image (the deployed container).
- **Function Compute** → the `waspadaprod-api` function (custom-container, port 8080), and its
  **custom domain** `app.waspada.xyz`.
- **Simple Log Service (SLS)** → the audit logstore (every run's step log).
- **ApsaraDB RDS (MySQL)** → the auth instance.

> "Five Alibaba Cloud services: OSS for the loan book, Container Registry and Function Compute for
> the serverless backend, Simple Log Service for the audit trail, and RDS MySQL for auth."

### 0:20–0:50 · Infrastructure as code
Show `deploy/iac/main.tf`: scroll the `alicloud_*` resources (OSS buckets + RAM policies, FC function
+ custom domain, SLS project/logstore, RDS instance, VPC).
> "It's all declared in OpenTofu — one file provisions the whole stack. And the RAM roles mean the
> function accesses OSS and SLS by role, not static keys."

### 0:50–1:20 · The deploy pipeline + the live proof
- Show `.github/workflows/build-image.yml`: buildx (linux/amd64) → push to ACR → Function Compute
  pulls the image.
- Hit the live endpoints on camera:
  - `https://app.waspada.xyz` → the dashboard renders.
  - `https://app.waspada.xyz/api/health` → `{"status":"ok","service":"waspada"}`.

> "Push to main builds and pushes the image; Function Compute serves it — live at app.waspada.xyz,
> reading real data from OSS."

---

**Key proof link for the form:** the permalink to `deploy/iac/main.tf` on `main` — it tells the whole
Alibaba-Cloud story standalone (OSS + ACR + FC + SLS + RDS in one file).
