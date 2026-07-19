---
id: pd-model-hosting (assign a WA-number on dispatch)
state: research + proposal
owner: unassigned — data/model lane (Bimo/Stefanie)
researched: 2026-07-19
depends-on: OSS write path (shared prerequisite — see §3)
---

# Where do we host the classical ML (PD) model?

**Verdict: keep it in-process in Function Compute. Do NOT move it to PAI-EAS.**
Optionally add **persist + version the model binary to OSS** as a *governance*
enhancement (reproducible/auditable scoring) — not for speed, and not on EAS.

---

## 1. Current hosting (verified in code)
`waspada/model/risk.py` + the `risk_model` agent: a sklearn `LogisticRegression`
**trained per-run, in-process** inside the FC invocation (`train()` fits the current
`FeatureFrame`, `predict()` scores inline). `save_model`/`load_model` pickle helpers
exist but write a **local** path; the agent re-fits fresh each run. Marginal cost ≈ **$0**
— it rides an FC call we already pay for. `explain()` (signed logit contributions +
band edges) is what the Risk Auditor cites in the debate.

## 2. PAI-EAS — researched, and it's the wrong tool here
- **Billing:** pay-as-you-go is per-minute **while the instance runs**; an always-on
  endpoint = a dedicated instance running **24/7**. The free **Serverless** (scale-to-zero)
  tier **only supports SDWebUI/ComfyUI**, *not* custom models — so a sklearn model gets
  **no free/scale-to-zero option**. Even the cheapest CPU instance 24/7 ≈ **tens of $/month**,
  plus EIP/bandwidth + OSS. (Ref points: 24-core CPU ≈ $1.02/hr; 4-core+T4 subscription ≈ $570/mo.)
- **Integration effort:** export sklearn→PMML (built-in PMML processor) or a custom Python
  processor; package ENV → OSS; deploy via **EASCMD** (Linux CLI) to a **dedicated resource
  group**; FC then calls the EAS HTTP endpoint (+token) **per score** (network hop).
- **Architectural blockers (beyond cost):**
  1. **Train-per-run** — the Actuary fits the *current* book each run; EAS serves a static
     pre-trained model (doesn't fit retrain-per-request without a separate train pipeline).
  2. **The debate needs the model's *internals*, not just its output** — EAS returns
     `p_default`, not `explain()` drivers/band-edges, so it would **break the debate's
     evidence grounding**.
- **When EAS *does* earn its cost:** large/GPU models, high sustained QPS, autoscaling, or a
  shared model registry — none apply to a tiny per-run logistic regression.

**Sources:** EAS billing (`/help/en/pai/billing-of-eas`), ML Platform pricing
(`/en/product/machine-learning/pricing`), PMML processor (`/help/en/pai/user-guide/pmml-processor`),
custom Python processor (`/help/en/pai/user-guide/develop-custom-processors-by-using-python`),
deploy inference services (`/help/en/pai/user-guide/deploy-inference-services`).

## 3. Proposal — persist + version the PD model binary in OSS (governance, $0)
Score with a **frozen, versioned, auditable** model instead of a subtly-different re-fit each
run. Stays in-process (no EAS), keeps `explain()` intact.

> **Not for speed** — a `LogisticRegression.fit` is fast (ms on the demo book, seconds at 1M rows).
> The saving is marginal; the value is **reproducibility + auditability**.

**Design:**
1. **OSS write path — the shared prerequisite.** `oss.py` is **read-only today** (no
   `put_object` anywhere; the medallion RAM policy *grants* PutObject on staging/mart but code
   never uses it). Add one upload helper. **This same write path unblocks FOUR things — build
   it once:** (a) this PD model binary, (b) dlt Option-B / medallion feature writes (WA-047),
   (c) the dispute-memory-OSS TODO (`dispute_memory.py: TODO(WA-047) add OSSMemory when the
   bucket policy allows PutObject`), (d) any future Silver/Gold outputs.
2. **Artifact = the existing pickled model dict** (coef_, band_edges, feature list —
   `explain()`-compatible) + a lineage header: `model_id` (`pd-lr-<timestamp|sha>`),
   `trained_as_of`, `n_train_rows`, AUC/metrics, feature list, schema/contract version.
   Stored at `oss://<staging|mart>/models/pd/<model_id>.pkl` + a `latest.json` manifest.
3. **Publish (train offline)** — a CI/manual entrypoint fits the model and uploads the versioned
   binary + `latest` pointer. **Not per-run.**
4. **Serve (risk_model agent)** — on run, `load_model` from OSS (`latest`, or a pinned
   `model_id`) when configured/present; else **fall back to train-per-run** (keeps offline /
   tests / demo working with no OSS). Scoring + `explain()` unchanged.
5. **Governance payoff** — stamp `model_id` + `trained_as_of` + metrics into the step log /
   SLS audit + the dashboard payload, so **every decision cites the exact model version**.
   "This run scored with `pd-lr-20260719`, trained as-of T, AUC=X" — a reproducible, defensible
   decision trail (matches the audit ethos; a genuine rubric/story asset).

**Tradeoffs:** needs a **retrain cadence** + accepts **train/serve separation** (score the
current book with a model trained earlier — the standard production split). Keep the
train-per-run fallback so nothing offline breaks.

**Effort:** OSS write helper = small (shared). Publish + load wiring + audit stamping + tests
≈ **0.5 day** once the write path exists.

**Sequencing / ownership:** data/model lane (Bimo/Stefanie to dispatch). **Build the OSS write
path first** (it's the shared unblock for four features), then this is a thin layer on top.
Not a solo-start — it touches `risk_model.py` + a new OSS write capability.
