# WASPADA — Submission Runbook

**Deadline: 05:00 WIB · Mon 21 Jul.** Target everything **done by 00:00 WIB** (5 h buffer).
**Bold = owner-only** (you). Everything else is built, green, and merged to `develop`.

---

## Where we stand (all ✅)
- Repo is **PUBLIC**; MIT license.
- `develop` has **everything**: two decision lanes (Collections + Origination), the six-agent
  debate, PD-model governance (calibration · drift monitoring · registry), the human parameter
  matrix, the cloud-blue UI + brand, and the docs/wiki (9 pages + diagrams).
- Full suite green offline; both lanes run end-to-end with `python -m waspada.agents --lane <lane>`.
- `develop → main` release verified **conflict-free** (102 commits ahead).
- Only *stale* thing: the deployed FC image (predates recent work) — step 1 refreshes it.

---

## The path — your 0→5 plan, in order

### 0 · Release everything to `main`  (~5 min) — **owner**
```bash
git fetch origin
git checkout main && git pull --ff-only origin main
git merge --no-ff origin/develop -m "release: WASPADA v1 → main (two lanes + governance + docs)"
git push origin main
```
Pushing `main` **triggers the CI image build** (`.github/workflows/build-image.yml`) → pushes
`api:latest` / `:<sha>` to ACR. *(The post-push verify step may show red — cosmetic WA-065 guard;
the image still pushes at the build step.)* Watch the Action go green on the build step.

### 1 · Make sure everything runs smoothly  (~15 min)
**Already verified by me (offline):** full test suite green, `tsc + vite` build green, both lanes
run end-to-end. **You verify the live deploy after redeploy:**
```bash
cd deploy/iac
tofu apply -replace=alicloud_fcv3_function.api -var-file=secrets.tfvars   # roll FC onto the new image
```
Then smoke-test:
```bash
curl -s https://app.waspada.xyz/api/health           # → {"status":"ok","service":"waspada"}
# open https://app.waspada.xyz  → dashboard RENDERS (not a download)
# register an account + sign in → work-list, debate flow-chart, model card, matrix all show
```
Real Lending Club data is already in OSS, so `POST /api/run?brain=mock` returns real data (no 503).
> **Minimum-viable fallback:** if the redeploy is flaky, the **current deploy + the committed fixture
> debate** is enough to record the demo. Redeploy is a quality upgrade, not a blocker.

### 2 · Demo video (~3 min) — **owner** · script: [`demo-video-scenario.md`](demo-video-scenario.md)
The money shot: dashboard → the agent debate (flow-chart lights up round by round) → governance
(model card + parameter matrix) → the two lanes → Alibaba/efficiency close. The committed fixture
shows a **real completed debate** — record that, zero live-Qwen risk. (Optional: one `brain=qwen`
run for a live money-shot if credits allow.)

### 3 · Deployment video (~1–2 min) — **owner** · script: [`deployment-video-scenario.md`](deployment-video-scenario.md)
Alibaba console tour (OSS · ACR · Function Compute · SLS · RDS) + `deploy/iac/main.tf` (all five
services in one file) + the live URL responding.

### 4 · Social-media post — **owner** · copy: [`social-post.md`](social-post.md)
Ready-to-paste EN + 中文 copy + the banner image (`docs/brand/banner.png`). Post to
LinkedIn / X / WeChat as you like.

### 5 · Submit the form — **owner**
| Field | Value |
|---|---|
| Track | **Track 3 — Agent Society** |
| Public repo | `https://github.com/MFarizalA/waspada` |
| License | MIT |
| Cloud proof | permalink to `deploy/iac/main.tf` on `main` (OSS + ACR + FC + SLS + RDS in one file) |
| Architecture | `docs/architecture.svg` + the [engineering wiki](wiki/Home.md) (9 pages + diagrams) |
| Description | paste [`submission-description.md`](submission-description.md) |
| Demo video | the ~3-min upload |
| Deployment video | the ~1–2 min upload |
| Live URL | `https://app.waspada.xyz` |

---

## Suggested WIB timeline (target done 00:00, deadline 05:00)
| Time (WIB) | Step |
|---|---|
| ~19:00 (home) | 0 — release to main, kick CI build |
| ~19:20 | 1 — redeploy FC + smoke-test live |
| ~19:45 | 2 — record demo video |
| ~20:15 | 3 — record deployment video |
| ~20:35 | 4 — post to social |
| ~20:50 | 5 — fill + submit the form |
| **~21:00** | **Done — 8 h before the 05:00 deadline** |

## If time runs out — minimum-viable submission
**0 → (skip redeploy, use current deploy + fixture) → 2 (demo only) → 5.** A public repo + the
diagram/wiki + the description + a demo video + the `main.tf` cloud proof = a valid, complete
Track-3 entry. Everything else is a quality upgrade.
