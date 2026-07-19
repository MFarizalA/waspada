# WASPADA — submission-day checklist (deadline: 2026-07-20)

Stefanie is offline (Kimi K3 quota to ~22–23 Jul), so the owner drives. This is the ordered
critical path. **Bold = owner-only.** Everything code/doc is already built + green.

## Status going in
- ✅ Product is real: 6-agent society, bounded debate, governance, MCP, model tiering, FC deploy.
- ✅ Data foundation (source→OSS→dlt→medallion) landed on `integrate/full-land`, suite 486 green.
- ✅ Real Lending Club data (50k rows) already in OSS. Diagram + submission description written.
- ⚠️ Deployed image is STALE (serves old synthetic). Repo is PRIVATE. Videos not shot.

## The path (≈ do in this order)

### 1. Land + release the code (~10 min)
```bash
git fetch origin
# land the integration on develop (fast-forward, doesn't touch your WA-077 checkout)
git push origin origin/feature/submission-day:develop
# release develop -> main so main has EVERYTHING (incl. custom_domain.tf + the diagram/desc)
git push origin origin/develop:main            # if main is behind + FF-able; else open a merge PR
```
> If `main` isn't fast-forwardable, do a normal merge locally from a clean checkout. The point:
> **`main` must contain `deploy/iac/custom_domain.tf`** (else an apply-from-main destroys the domain)
> and the docs (for the cloud-proof permalink + diagram link).

### 2. **Flip the repo PUBLIC** (owner, ~2 min)
GitHub → Settings → Danger Zone → Change visibility → Public. **Unblocks 3 requirements at once:**
public repo URL, License auto-detection, and the cloud-proof permalink resolving.

### 3. Deploy a current image (~20 min) — real data is already in OSS
Build + push the image (CI on the `main` push does this: `build-image.yml`), then roll FC onto it:
```bash
cd deploy/iac
tofu apply -replace=alicloud_fcv3_function.api -var-file=secrets.tfvars   # apply FROM develop/main-current
```
- **Real 50k-row LendingClub data is in OSS** (`loans.parquet`), so `/api/run` reads real data (no 503).
- Landmine (now resolved by step 1): `custom_domain.tf` + `rds_grant.tf` are on main → safe.
- Verify: `https://app.waspada.xyz` renders; `POST /api/run?brain=mock` returns real data.

### 4. **Record the two videos** (owner) — see `docs/demo-video-scenario.md` + `docs/deployment-video-scenario.md`
- **Demo (~3 min):** the dashboard + the live Qwen debate (the money shot). **Do ONE `brain=qwen` run** (conserves credits) and capture the Agent-Society panel streaming the debate.
- **Deployment (~1–2 min):** the Alibaba console tour (OSS/ACR/FC/SLS/RDS) + `main.tf` + the live URL.

### 5. (If credits allow) the efficiency baseline — `docs`/WA-085
One `python -m waspada.bench_society.run_bench` with `DASHSCOPE_API_KEY` set fills the single-agent
baseline → substantiates the Track-3 "measurable efficiency gain." ~cents. Skip if out of credits;
the calls-per-account gain (~10.7×) still stands by construction.

### 6. **Submit the form** (owner)
| Field | Value |
|---|---|
| Track | **Track 3 — Agent Society** |
| Public repo | `https://github.com/MFarizalA/waspada` (after step 2) |
| License | MIT (auto-detected once public) |
| Cloud proof | permalink to `deploy/iac/main.tf` on `main` (OSS+ACR+FC+SLS+RDS in one file) |
| Architecture diagram | `docs/architecture.svg` (linked from README) |
| Text description | paste from `docs/submission-description.md` (industry names, Qwen tiers, MCP, Alibaba) |
| Demo video | the ~3-min upload |
| Live URL | `https://app.waspada.xyz` |

## If time runs out — minimum viable submission
Steps **1 → 2 → 4(demo only) → 6**. A public repo + the diagram + the description + a demo video
(even on the current synthetic deploy) + the `main.tf` cloud proof = a valid, complete entry. The
redeploy (3) and baseline (5) are quality upgrades, not blockers.

## Your (Bimo) parallel track
**WA-077** (real-data cutover) + its 2 red `test_stream` SSE tests are yours and independent — finish
if you can, but they don't block the submission (the deployed demo works either way).
