---
id: WA-070-review
state: done
priority: P1
owner: claude
reviewed: 2026-07-19
method: read-only, 4 parallel analysis passes (features / architecture / requirements / demo-risk)
---

# WA-070 · Pre-submission review — verdict

**Call: READY-WITH-FIXES.** The agent-society core that Track 3 actually judges —
6-agent society, bounded evidence-grounded debate, model tiering, human gate,
native function calling, MCP — is **real, honest, and matches the code**. A judge
who reads the prose and watches a *working* demo comes away accurate.

It is **not submission-ready today** for four reasons, none of them in the debate
engine: (1) the **live deploy is stale** — it runs old synthetic code without the
WA-080 timeout fix; (2) the **repo is still private**; (3) the **required
artifacts** (public architecture diagram, text description, demo video) don't
exist yet; (4) the **efficiency benchmark** — a Track-3 *explicit* requirement —
has no measured single-agent baseline. Most fixes are low-effort; two are
substantive (benchmark baseline, live-qwen verification).

Scope note: read-only. Cites WA-064 findings + WA-069 report; does not re-run them.
Fixes below get their own tickets — this is the gap list, not the fixes.

---

## MUST-FIX before submission (ranked)

### P0 — blocks a credible submission or the live demo

| # | Gap | Why it blocks | Owner | Effort |
|---|-----|---------------|-------|--------|
| 1 | **Deployed prod image is stale** (`fc_image_tag=0ae6be8`, `variables.tf:48`) — predates WA-080, WA-077, the WA-067 route fix. Live prod serves the synthetic n=200 stub (WA-069). | A live `brain=qwen` demo *today* reproduces the ~120s timeout WA-069 hit — **the WA-080 fix isn't in the container.** | backend / Stefanie | build+deploy a develop image (~30 min), gated on the WA-077 sequencing (#5b) |
| 2 | **Repo is private** (PAT remote). | Gates *three* requirements at once: public URL, License "About" detection, cloud-proof permalink resolving. Nothing judge-facing works until flipped. | owner | ~30 min, after WA-071 secrets re-confirm |
| 3 | **Efficiency benchmark has no measured baseline.** `AGENT_SOCIETY_BENCH.json`: `single_agent_baseline.status="not_run"`, `baseline_recall_at_call_tier=null`. The "~10.7× fewer calls" hero is vs a *by-construction* baseline, not a run one. | Track 3's single most-scrutinized requirement is a "measurable efficiency gain vs single-agent." A judge opening the JSON sees a one-sided comparison. | backend | run baseline ~1–2 h (needs `openai` SDK), OR reframe the claim honestly ~30 min |
| 4 | **Live `brain=qwen` debate unverified end-to-end.** WA-080 is unit-tested + reviewed, never run live; WA-079 (live verify) still `todo`. And WA-080 parallelized **only the audit** — the per-dispute rebuttal→arbiter loop (`orchestrator._resolve_disputes`) is **still sequential** on qwen-max, so sub-180s isn't guaranteed. | The debate is *the showpiece*. On default `brain=mock` it opens **zero disputes** (WA-069). If the live path also fails, the headline feature doesn't happen on camera. | backend / Stefanie | WA-079: run qwen K=1–2, capture timing; consider parallelizing the debate loop (collision-free, orchestrator.py only) |
| 5 | **Two deploy landmines** — (a) `custom_domain.tf` is on develop, **not main**; a `tofu apply` from main plans to **DESTROY `app.waspada.xyz`**. (b) WA-078's parquet was **never uploaded**; deploying the WA-077 cutover against an empty bucket makes `/api/run` return 503 (synthetic fallback removed by design). | Either one silently breaks the live demo. | backend / infra | process guardrails: only apply from develop; upload OSS parquet *before* deploying WA-077 |

### P1 — judge-catchable, mostly trivial

| # | Gap | Owner | Effort |
|---|-----|-------|--------|
| 6 | **README says "RDS PostgreSQL"** (`README:22`) but IaC is **MySQL 8.0** (`main.tf:299`) and `api/db.py` has no PostgreSQL. Flat contradiction in the two most-read files. | docs | ~10 min — highest payoff:effort in the whole review |
| 7 | **`docs/` doesn't exist** — cloud-topology architecture diagram, `submission-description.md`, and the video scripts (WA-073/074/075) all land there, none started. The only diagram is an inline README Mermaid of *agent flow*, not cloud topology. | Claude (docs) + owner (diagram/shoot) | WA-073 bundle ~4–6 h |
| 8 | **Internal doc contradictions** a read-in judge will notice: Data Analyst marked 🟡 "planned" in `HACKATHON:141` vs ✅ in README (it's fully wired); the Skeptic row `HACKATHON:143` says both "single-shot JSON, not a loop" *and* "native function-calling loop"; **`app.waspada.xyz` appears nowhere** in README/HACKATHON (docs still point at the `*.fcapp.run` download-quirk URL). | docs | ~30 min |
| 9 | **`brain=mock` shows no debate** — default path opens 0 disputes; a judge running defaults sees a pipeline but no society argument. | frontend / Stefanie | ensure the default view leads with the static fixture / recorded qwen debate, not a silent mock run |

### P2 — credibility / polish

| # | Gap | Owner | Effort |
|---|-----|-------|--------|
| 10 | **IaC advertises unused infra** the prose disowns: a "DuckDB analytical engine on RDS" (`main.tf:294-296,312`; not a real ApsaraDB feature; `get_analytics_connection` self-described "unreachable in production") and a 3-bucket **write-policy** medallion the code only ever reads from (no `put_object` anywhere). HACKATHON says "RDS auth-only, federation descoped / OSS read-only, nothing writes." Reads as aspirational scaffolding. **Touches deploy/iac — WA-077 blast radius; flag, don't fix now.** | backend / infra | reconcile prose↔IaC, or add honest "future work" framing |
| 11 | **HTTP-only custom domain** — judges type seeded creds (`analyst@waspada.demo / waspada123`) into a "Not Secure" page for a *credit-risk* product. No mixed-content (same-origin), so cosmetic — but a bad look. | backend | free Alibaba DV cert → `enable_https=true` + PEMs → scoped re-apply |
| 12 | **dlt claim** (declared dep, cited in `README:152/306`, `HACKATHON:140`, `data_engineer.py:11` docstring; **never imported**; `lakehouse.py` openly disavows it). | — | **KNOWN/ACCEPTED — owner ruled in WA-081 (cancelled): "dlt stays." Do not purge.** Logged as managed exposure. |

---

## Per-area verdicts

### A. Feature claims vs reality — mostly SOLID, three real GAPs
**WORKS (verified):** deterministic FeatureFrame + sklearn (leakage guard, vintage split); insight ranking/health/alerts/EL payload; agent framework + gate + CLI (offline ~3s); **MCP server+client (byte-parity verified, WA-069 §7 — one of the strongest claims)**; dispute memory; SLS audit + local fallback; JWT/bcrypt auth (live-verified on prod); model tiering; native function calling.
**GAPs:** the debate opens **0 disputes on the mock path** and is unproven live on qwen; the **benchmark baseline was never run**; the API cutover introduces a **503/500 path** and currently reds 4 tests on the working tree.

> **Test-suite honesty note:** on the *current working tree* the suite is **441 passed / 4 failed / 7 skipped** — the 4 failures (`test_stream.py` ×3, `test_auth.py::test_run_with_valid_token_passes_gate`) are caused by **Bimo's in-flight uncommitted WA-077 cutover** removing the API's synthetic fallback, not a regression. On committed `develop` HEAD it is green (429 after WA-080). The "green offline" claim is true on committed HEAD, temporarily false mid-cutover — expected, but don't ship/deploy the WA-077 branch until its own suite is green (full-suite-after-merge rule).

### B. Architecture coherence — core SOLID, data/infra layer STALE
The agent-society narrative is real and matches the code (society membership, two lanes with Origination correctly deferred, tiering, function calling, bounded ≤K×3 debate, DISPUTED first-class, gate fails closed, FC deploy — all verified). **Every divergence is in the data/infra layer** and surfaces the moment a judge opens `deploy/iac/` (which the submission invites as deployment proof): PostgreSQL-vs-MySQL (#6, HIGH), unused DuckDB-on-RDS + medallion scaffolding (#10, MED-HIGH credibility), MCP-stdio-vs-InProcess (LOW-MED), stale agent markers (#8, LOW). None touch the debate engine the track is judged on.

### C. Requirements coverage — ~2 of 7 required items truly satisfied
**Exist:** MIT `LICENSE` at root; `main.tf` as a strong standalone cloud-proof (14 `alicloud_*` resource types). **Missing/partial:** public repo (private), architecture diagram (cloud topology), text description, demo video, README track badge. Critical path: **flip public (unblocks 3) → merge develop→main for stable permalinks → WA-073 docs bundle → WA-074 video.**

### D. Demo risk — NOT ready for a live qwen debate; SAFE on the static/recorded path
The deployed container is materially behind develop (stale image #1), the real-data story is mid-flight (#5), and the live debate is unproven (#4). **Pragmatic demo:** keep the working (synthetic-but-live) deployment *or* land real data verified-first; **lead with the static dashboard + a pre-recorded qwen debate (WA-074)**; treat any live-qwen click as optional and rehearsed. Hold the two P0 landmines (#5).

---

## What holds up (the strengths — lead the pitch with these)
- **Genuine 6-agent society** with a real, bounded, evidence-grounded debate — the Track-3 ask, and it's honest.
- **Real MCP** server+client with byte-parity verified evidence tools.
- **Model tiering** (flash/plus/max) mapped to cognitive load, per-agent, real IDs.
- **Native function-calling loops** with tool-role feedback + JSON-mode fallback.
- **Human-gate governance** with asymmetric escalate/de-escalate rules (WA-048), fail-closed.
- **Honest engineering culture** — `lakehouse.py` disavowing the removed dlt scaffold, "not run" left visible in the bench JSON, 🟡 markers on genuinely-incomplete work. This *helps* under scrutiny; the fixes above are about closing the few places the docs got *ahead* of the code, not the reverse.
