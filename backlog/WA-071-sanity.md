---
id: WA-071-sanity
state: done
priority: P1
owner: claude
tested: 2026-07-19
method: fresh clone of develop (1a9a514) in a clean dir + fresh venv; live prod smoke; findings only
---

# WA-071 · Sanity test — verdict

**Call: NOT judge-ready — two P0 quickstart blockers + one critical deploy gap.**
The product itself is sound (full suite green, CLI works, dashboard builds, hygiene
clean), but **a judge who follows the README verbatim cannot install it**, and the
**live site is running a stale image**. Both are fixable fast; neither is in scope
to fix here (findings only).

| # | Check | Result |
|---|-------|--------|
| 1 | Fresh-clone quickstart | 🔴 **FAIL — 2 × P0** |
| 2 | Full test suite | ✅ PASS (443 passed / 2 known-fail / 7 skip / 1 xfail) |
| 3 | Live prod smoke | 🟠 health+login PASS; **run = CRITICAL (stale deploy)** |
| 4 | Dashboard build | ✅ PASS (1 cosmetic) |
| 5 | Repo hygiene | ✅ PASS |

---

## 1. Fresh-clone quickstart — 🔴 FAIL (judge-blocking)

Fresh `git clone` of `develop` (`1a9a514`), `python -m venv .venv`, then the
README's **primary** install command **verbatim**:

### P0-1 — `pip install -r requirements.txt` is unsatisfiable
```
$ pip install -r requirements.txt
ERROR: Could not find a version that satisfies the requirement cudf-cu12>=26.6
       (from versions: 23.6.0, …, 24.10.1)
ERROR: No matching distribution found for cudf-cu12>=26.6
```
`requirements.txt:7-8` hard-pin `cudf-cu12>=26.6` + `cuml-cu12>=26.6` (RAPIDS GPU).
PyPI's newest `cudf-cu12` is **24.10.1** — `>=26.6` can't resolve from PyPI at all,
and even the NVIDIA-index versions are **Linux+GPU only**. A judge on
Mac/Windows/vanilla-Linux dies on the first install command.
**Rating: judge-blocking. Owner: backend (Bimo). A broken quickstart is a dead submission.**

### P0-2 — even past cudf, `requirements.txt` is missing the API deps
After installing the non-GPU subset, the tests can't collect:
```
$ pytest -q
ImportError … tests/test_auth.py:15: from fastapi.testclient import TestClient
E   ModuleNotFoundError: No module named 'fastapi'
```
`fastapi` and `uvicorn` are **not in root `requirements.txt`** — they live in
`api/requirements.txt`, which the README's Quick start **never tells you to install**.
So the documented flow (`pip install -r requirements.txt` → `pytest` → `uvicorn api.main:app`)
cannot run the tests *or* the API server.
**Rating: judge-blocking. Owner: backend (Bimo).**

### Root cause + suggested fix (not applied here)
`api/requirements.txt` is already a **complete, CPU-only, working** set (fastapi,
uvicorn, sklearn, pyarrow, pandas, numpy, duckdb, oss2, openai, mcp, auth deps — **no
cudf**). The README simply points at the wrong file. Cheapest fix: **point Quick start
at `pip install -r api/requirements.txt`**, and move the GPU lines behind an optional
extra-index note (they already degrade gracefully at runtime — "OSS not configured →
synthetic snapshot" path proves the CPU path is self-sufficient).

### What DOES work once deps are installed (verbatim, exit 0)
```
$ python -m waspada.agents --lane collections --auto-approve --top-n 50
[waspada] OSS not configured -> using synthetic 200-row snapshot (offline demo).
[waspada] audit: shipped 50 record(s) via local (run_id=6db76bd1b038)
WASPADA Collections run — 50 accounts on the work-list. Top risks: LN00000009 (p=1.00, call) …
[waspada] dashboard payload written to data\dashboard-payload.json
```
The CLI demo, tests, and dashboard all work — the *only* quickstart defect is the
install instruction itself.

---

## 2. Full test suite — ✅ PASS
```
$ pytest -q     # (with api/requirements.txt installed)
2 failed, 443 passed, 7 skipped, 1 xfailed, 19 warnings in 48.06s
```
**443 passed** — matches the expected baseline. The **2 failures are the known
`test_stream.py` scripted-debate SSE tests** (`test_stream_scripted_debate_emits_rounds_resolution_done`,
`test_stream_scripted_multiple_disputes_one_resolution_each`) — WA-077 remainder, Bimo
owns them; excluded per the brief. 7 skips are the documented live-only smoke tests; no
new breakage.
- *Cosmetic:* `InsecureKeyLengthWarning` — the test JWT secret is 28 bytes (<32). Harmless
  in tests; **confirm the prod `WASPADA_JWT_SECRET` is ≥32 bytes** (the startup guard should
  already enforce this — worth a one-line check).

---

## 3. Live prod smoke — 🟠 health/login PASS, run = CRITICAL

Against `https://waspadaprod-api-vouqzqqkiu.ap-southeast-1.fcapp.run` (read-only):
```
GET  /api/health                 → 200 {"status":"ok","service":"waspada"}        ✅
POST /api/auth/login (seeded)     → 200 {token: eyJ…, user: analyst@waspada.demo}  ✅
POST /api/run?brain=mock          → 200 {payload: …200-row synthetic book…}        🔴
```
**CRITICAL:** the brief said `/api/run?brain=mock` should now **503 "data source
unavailable"** (WA-077 guard, bucket empty until WA-078 lands) — *"if it returns
synthetic-looking data instead, that's a critical finding."* It returned **200 with the
synthetic n=200 stub** (`LN00000009…`, `DKI Jakarta`). This proves the **deployed prod
image predates WA-077** (and WA-080) — the cutover is committed to develop (`1a9a514`)
but **not deployed**. The running container is stale.
- **Rating: critical (release/deploy gap). Owner: backend/Stefanie.** A redeploy of a
  current develop image is needed — but respect the two WA-070 landmines: **`tofu apply`
  only from develop** (main lacks `custom_domain.tf` → apply-from-main destroys
  `app.waspada.xyz`), and **upload the OSS parquet before deploying the WA-077 cutover**
  (else the run button 503s live).
- *Also observed:* the mock run's step log shows `disputes=0` — the agent-society debate
  is invisible on the default `brain=mock` path (needs `brain=qwen`). Not a regression;
  relevant to the demo story (lead with the recorded qwen debate / static fixture).

---

## 4. Dashboard build — ✅ PASS
```
$ cd dashboard && npm ci && npm run build
… added packages (package-lock.json present, npm ci clean, exit 0)
> tsc -b && vite build
✓ 55 modules transformed.
dist/index.html  0.54 kB │ dist/assets/index-*.css 28 kB │ dist/assets/index-*.js 195 kB
✓ built in 1.21s
```
Builds clean; `dist/index.html` + assets produced.
- *Cosmetic:* `npm ci` reports **2 vulnerabilities (1 moderate, 1 high)** — run `npm audit`
  before submission; not judge-blocking. Owner: frontend.
- *Note:* live render/login was verified separately (WA-067 custom domain renders inline);
  a full `npm run dev` interactive session wasn't run here (dev server is long-lived), but
  the production build + the live prod login (§3) both pass.

---

## 5. Repo hygiene — ✅ PASS
- **Secrets:** `git grep` for `AKIA…/LTAI…/password=/secret=` surfaces **no real cloud
  credentials** — only test fixtures (`supersecret1`) and the intentionally-public seeded
  demo password (`waspada123`). ✅
- **`.env`:** gitignored (`.gitignore` has `.env` + `.env.*`; `git check-ignore .env`
  confirms). ✅
- **`.env.example`:** every sensitive field is an **empty placeholder** (OSS keys,
  `DASHSCOPE_API_KEY`, `WASPADA_JWT_SECRET`, `DATABASE_URL`). The only filled value is
  `WASPADA_DEMO_PASSWORD=waspada123` — the intentional public judge credential. ✅
- **LICENSE:** present at root, canonical **MIT** ("MIT License" + `Copyright (c) 2026
  Muhammad Farizal Afkar` + standard grant text, 21 lines). GitHub will auto-detect. ✅

---

## Must-fix before a judge touches it
1. **P0-1 + P0-2 — the quickstart install** (backend/Bimo). Point Quick start at
   `api/requirements.txt` (or add fastapi/uvicorn to root + move cudf/cuml behind the
   optional NVIDIA-index note). **This is the single highest-value fix in the review.**
2. **Critical — redeploy a current image** (backend/Stefanie). Live prod runs pre-WA-077
   code; it serves synthetic data and lacks the WA-080 timeout fix. Deploy from develop,
   honoring the two landmines above.
3. *Cosmetic:* npm audit (2 vulns); confirm prod JWT secret ≥32 bytes.

**Out of scope (per ticket):** live `brain=qwen` prod debate (WA-069/WA-079); performance/load.
