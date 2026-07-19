---
id: infra-secrets-batch-research
state: research-complete
owner: claude
researched: 2026-07-19
topics: (1) KMS/RAM secret management instead of .env; (2) scheduled batch job for fresh data
verification: code (deploy/iac/main.tf, waspada/data/oss.py) + Alibaba Cloud docs
---

# Infra research — secret management (KMS/RAM) + fresh-data batch job

## Q1 — Alibaba KMS/RAM instead of `.env`? **Yes. Two mechanisms; fixes a real anti-pattern.**

### Current state (verified in code)
- The FC function **already has a RAM execution role** (`alicloud_ram_role.fc_execution`, `main.tf:98`)
  with policies for OSS read/write (`fc_oss_read`/`fc_oss_write`), SLS write, ACR read, VPC/ENI.
- **But the code doesn't use it.** `oss.py:72` authenticates with **static
  `OSS_ACCESS_KEY_ID/SECRET`** from env; `DASHSCOPE_API_KEY` / `WASPADA_JWT_SECRET` /
  `DATABASE_URL` (DB password) are **plaintext FC env vars** (`main.tf:390-408`) — visible in the
  console. Long-lived keys on compute.

### Fix — two different mechanisms (don't conflate)
1. **OSS/SLS/ACR → the FC RAM role (STS). Drop the static keys entirely.**
   FC auto-injects *temporary* STS credentials to the function (`context.credentials`:
   id/secret/securityToken, auto-rotated). Switch `oss.py` `oss2.Auth(static AK)` →
   `oss2.StsAuth(id, secret, token)` (or oss2's RAM-role credentials provider). **Removes
   `OSS_ACCESS_KEY_ID/SECRET` from `.env` + the FC config completely** — the role already grants
   the access. *Low risk, high payoff.*
2. **Genuine secrets (Qwen key, JWT secret, DB password) → KMS Secrets Manager.**
   These can't come from a RAM role (not Alibaba resources). Store in **KMS Secrets Manager**;
   fetch at cold-start via `GetSecretValue` (authorized by the FC role + a KMS-decrypt policy) —
   dynamic (latest value), rotatable, **audited in ActionTrail**, never in the image. Cheaper
   interim: **KMS-encrypted env vars** (FC decrypts at runtime).

### Effort / risk
- OSS→STS: **small, low-risk** (auth swap in `oss.py` + remove 2 env vars + confirm the role's OSS
  policy covers the object). Keep a static-AK fallback for offline/tests.
- KMS for the 3 secrets: **moderate** (KMS secret via IaC + a fetch-at-startup helper + a
  RAM KMS-decrypt policy). Ships as **WA-087**.

## Q2 — Batch job for fresh data? **Yes for real production (not needed for the demo).**

### Why
The society reads OSS **on-demand per `/api/run`**, and the OSS book is a **one-time upload → it
goes stale**. Collections is inherently a **daily batch** (score the portfolio each morning →
work-list). Fresh data needs a **scheduled producer**.

### Mechanism — FC Time Trigger (cron)
A scheduled function (e.g. `CRON_TZ=Asia/Jakarta 0 0 6 * * *` = 6am daily) that **refreshes the OSS
book** (re-ingest from source → transform → upload) and optionally **pre-computes the payload**.
Serverless; no infra to run.

### Ties three threads together
- **dlt (WA-083):** dlt's incremental cursors + `merge` dedup *is* the fresh-data load pattern —
  the scheduled job runs the dlt pipeline to pull only new/changed loans.
- **Model versioning (WA-082):** the same cadence retrains + republishes the model.
- The batch job **is** the "producer" that populates OSS (what we do by hand via the loader today).

### Verdict
Demo: a one-time upload is fine. **Real production: a scheduled FC-time-trigger refresh is the
right architecture** — else every run scores a frozen snapshot. Ships as **WA-088**.

## Sources
- FC → RAM role / STS for OSS: `/help/en/function-compute/latest/grant-function-compute-permissions-to-access-other-alibaba-cloud-services`
- KMS Secrets Manager: `/help/en/kms/key-management-service/user-guide/secret-management-overview`
- FC env-var encryption via KMS: `/help/en/functioncompute/fc/user-guide/environment-variables`
- FC Time Triggers (cron): `/help/en/functioncompute/fc/user-guide/time-triggers`
