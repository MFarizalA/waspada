# Deployment notes

This folder collects the deployment artifacts for WASPADA.

- `deploy/iac/` — OpenTofu infrastructure for Alibaba Cloud (WA-027). See
  `deploy/iac/README.md` for the full walkthrough.

## Required environment variables

The following variables are required for a safe, working deploy:

| Variable | Purpose | Notes |
|---|---|---|
| `WASPADA_JWT_SECRET` | JWT signing secret | **Required in every environment.** Must be at least 32 bytes. The app refuses to start if it is missing or too short. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))` |
| `DATABASE_URL` | User store | ApsaraDB RDS MySQL connection string in prod; unset uses local SQLite. |
| `DASHSCOPE_API_KEY` | Live reasoning | Only needed when `WASPADA_LLM_PROVIDER=qwen`. |
| `OSS_*` | Object storage | Required for the real OSS data path. |

Set these in your environment or in a `.env` file (copied from `.env.example`).
