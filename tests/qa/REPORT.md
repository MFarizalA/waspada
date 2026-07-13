# WA-012 QA Report — WASPADA Collections Lane

**Reviewer:** Reza (QA Engineer) · **Verdict:** **PASS (advisory)** — ship-blocker-free
**Date:** 2026-07-07 · **Repo state tested:** `main` @ `901466e` (working tree, with concurrent edits noted below)
**Baseline suite:** 108 passed, 1 skipped (WSL/GPU) — `tests/` excluding `tests/qa/`
**QA suite:** 36 passed, 1 xfailed across `tests/qa/` (4 check files + conftest)
**Combined:** 144 passed, 1 skipped, 1 xfailed

---

## Verdict & headline

**Advisory PASS.** No blockers. One **major** finding (F-DV-05: the BQ ingest
path's `LIMIT`-without-`ORDER BY` returns a status-clustered, 100%-default
sample) that does not break the shipped pipeline — the production pipeline
reads the full table — but silently invalidates any sample-based smoke/analysis
that trusts `fetch_loans(limit=N)` to be representative. The model's
leakage guard is sound. Two minor findings and two nits round it out.

**The leakage gate holds.** `label_default` is not derivable from any model
feature. `delinquency_status` (a deterministic bijection with the label) is
correctly excluded from the model matrix via `LEAKAGE_EXCLUDED`. Vintage
train/test windows do not overlap. This is the finding I most wanted to
verify, and it is clean.

> ⚠️ **Concurrent edits.** While QA ran, another worker (Bimo, WA-007) landed
> commit `901466e` (benchmark harness) and left untracked `api/`, `Dockerfile`,
> `.dockerignore`, and an extra `dashboard/public/sample-payload.json`. The
> findings below were re-verified against the post-edit working tree at
> `901466e`. The benchmark findings (section 6) describe the post-edit state;
> the task body's "3.9× claim" premise is stale and resolved (see F-BM-01).

---

## Findings summary

| ID      | Severity | Area                  | One-line                                                                 |
|---------|----------|-----------------------|--------------------------------------------------------------------------|
| F-DV-05 | **major**| Data / ingest path    | `fetch_loans(limit=N)` returns a 100%-default clustered slice (N≤88098). |
| F-LK-01 | info     | Leakage (documented)  | `delinquency_status` is a label bijection — correctly excluded.          |
| F-LK-04 | info     | Leakage (documented)  | `payment_ratio`/`outstanding_ratio` are snapshot-derived, not panel-leak.|
| F-PI-04 | minor    | Pipeline / fixture    | `vintage_default_rate` has a spurious `2024` cohort key (reconstruction drift). |
| F-PI-05 | minor    | Contract / dashboard  | Vintage `Alert.segment = {"vintage": …}` doesn't match TS `Segment` shape.|
| F-DM-01 | nit      | Dashboard             | Unreferenced stale placeholder `public/sample-payload.json` (p=1.0 rows).|
| F-BM-01 | info     | Benchmark             | Task body's "3.9× claim" is stale; README is now honest (no such claim). |

No blockers. No secret/credential leaks found (`.env`/`secrets/` not read or
printed; creds used only to authenticate the live BQ smoke queries).

---

## 1. Data validation (`test_data_validation.py`)

### F-DV-01 — RawLoans contract: schema/null/range — **PASS**
The synthetic fixture and the live book both validate as a `RawLoans` superset
with zero nulls and in-domain numerics. Backed by `TestRawLoansContract` and
`TestLiveBigQueryDataQuality`.
**Evidence (live, full-table aggregate):** 1,000,000 rows; null rate 0.0 across
all 13 `RawLoans` fields; `amount>0`, `term∈[12,60]`, `rate∈[5.0, 31.45]`,
`dti∈[0, 45]`, `outstanding_principal ≤ amount` (0 violators), 0 duplicate
`loan_id`s.

### F-DV-02 — FeatureFrame contract: built frame validates, no nulls — **PASS**
`build_features()` output validates against `FeatureFrame`, all fields non-null,
`label_default` is boolean with both classes present, ratios finite and
in range. Backed by `TestFeatureFrameContract`.

### F-DV-03 — BQ ingest path returns a valid RawLoans superset — **PASS**
`fetch_loans(lane="collections", limit=50)` returns an Arrow table that
`validate_table()` accepts. Backed by `test_fetch_loans_returns_rawloans_superset`.

### F-DV-04 — Portfolio data quality (live, 1M rows) — **PASS (with notes)**
- **Label distribution:** default rate = (88,098 Charged Off + 54,319 Default) / 1,000,000 = **14.24%**. Plausible, two-sided.
- **Status mix:** Current 42.0%, Fully Paid 36.1%, Charged Off 8.8%, Default 5.4%, Late 31-120 5.4%, Late 16-30 2.2%.
- **`total_paid > amount`** for 361,328 rows (36%) — these are the Fully Paid cohort and include interest; expected, not a defect.
- **Vintage coverage:** only **2021, 2022, 2023** (`issue_date` ∈ [2021-01-01, 2023-12-31]). No 2020, no 2024+.

### F-DV-05 — `fetch_loans(limit=N)` is a silently biased, status-clustered sample — **MAJOR**
**Severity: major** (broken for sampling/analysis, with workaround: use the full
table or add `ORDER BY`). Not a blocker because the production pipeline reads
the full table, not a LIMIT slice.

**Description.** The `loans` table is **physically clustered by
`current_status`**. Storage-order offsets 0–88,097 are entirely `Charged Off`;
`Current` begins around offset 88,098. `BigQueryClient.fetch_loans()` issues
`SELECT <cols> FROM ... [LIMIT N]` with **no `ORDER BY`**
(`waspada/data/bq.py:146-148`), so for any `N ≤ 88098` the result is a
**100%-default** slice.

**Evidence (live).**
```
LIMIT   1000: default_rate=1.0000   first_status='Charged Off'
LIMIT   5000: default_rate=1.0000   first_status='Charged Off'
LIMIT  20000: default_rate=1.0000   first_status='Charged Off'
OFFSET      0: 'Charged Off'
OFFSET  50000: 'Charged Off'
OFFSET  88000: 'Charged Off'
OFFSET  90000: 'Current'
```
Full-table rate via aggregate (unbiased): **0.1424**.

**Impact.** (1) The build's own `tests/test_bq_smoke.py::test_fetch_loans_is_rawloans_superset`
uses `limit=10` and "passes" while validating a degenerate 10-row all-default
slice — its schema check is valid but it is **not** a representative smoke
sample. (2) Any sample-based data-quality or label-distribution check that
calls `fetch_loans(limit=N)` will see a wildly wrong default rate (this QA's
first cut asserted `0.05 < rate < 0.30` on a 20k LIMIT and failed at
`rate == 1.0`). (3) If a future agent uses `fetch_loans(limit=N)` for ad-hoc
EDA or a quick model fit on a "sample", it will train/evaluate on pure
defaults.

**Repro.**
```
PYTHONPATH=/workspace python tests/qa/_probe_sampling.py
```
**Recommended fix (out of scope — route to WA-002 owner):** either cluster the
table on a non-status column, or make `fetch_loans` add
`ORDER BY loan_id` (deterministic, unbiased) when a `limit` is requested;
document that LIMIT is not a random sample.

**Backing tests:** `test_live_label_distribution_via_aggregate_not_biased_limit`
(asserts the *unbiased* rate is plausible), `test_live_limit_sample_is_biased_clustered_by_status`
(encodes the bias as the expected behaviour so the finding is executable).

---

## 2. Model sanity (`test_model_sanity.py`)

The model (`waspada/model/risk.py`) **is implemented** (commit `1ee2f78`),
contrary to the task body's note that it "doesn't exist yet". Tests run for real.

### F-MS-01 — Vintage split + AUC above chance — **PASS**
On a 400-row synthetic frame with a real (dti/rate) signal, the out-of-time
hold-out AUC beats 0.5. Split metadata lists method, train/test years; indices
are disjoint. `test_auc_above_chance_on_separable_data`.

### F-MS-02 — Predicted probabilities sane + signal is used — **PASS**
`predict()` output validates against `ScoredAccounts`; `p_default ∈ [0,1]`,
finite; high-dti bucket scores higher on average than low-dti, confirming the
model uses the engineered signal (not ignoring it). Backed by
`test_predict_probs_in_unit_interval`, `test_high_dti_high_rate_scores_higher_on_average`.

> **Calibration** is not asserted: with `class_weight="balanced"` and only a
> hold-out AUC, a Brier/calibration curve check is deferred until a real
> validation set is wired. Non-blocking; noted for the model ticket.

---

## 3. Data leakage (`test_leakage.py`) — **the critical gate: PASS**

### F-LK-01 — `delinquency_status` is a deterministic label bijection, correctly excluded — **info (documented)**
`delinquency_bucket(current_status)` maps `Charged Off`/`Default` → `"Default"`,
and `is_default(current_status)` is `True` for exactly those two. Therefore
`delinquency_status == "Default"` **iff** `label_default == True` — a perfect
proxy for the label. **This is excluded from the model matrix** by
`waspada/model/risk.py:LEAKAGE_EXCLUDED` and is in neither `NUMERIC_FEATURES`
nor `CATEGORICAL_FEATURES`. The guard holds. Backed by
`test_delinquency_status_is_deterministic_proxy_for_label`,
`test_model_feature_columns_exclude_leakage_set`.

> **Residual risk (noted, not blocking):** `delinquency_status` is still a
> `FeatureFrame` contract field, so any *future* consumer that selects "all
> columns" as features would reintroduce the leak. The guard is a convention in
> `risk.py`, not enforced at the schema boundary. Acceptable for the MVP;
> recommend a regression test (this QA's `test_model_feature_columns_exclude_leakage_set`
> is exactly that) stays green.

### F-LK-02 — Label is exactly `is_default(current_status)` — **PASS**
No hidden extra logic widens the leakage surface. `test_label_default_equals_is_default_of_current_status`.

### F-LK-03 — Model feature matrix excludes the full leakage set — **PASS**
`{loan_id, delinquency_status, label_default, as_of_date} ∩ FEATURE_COLUMNS = ∅`.
Backed by `test_model_feature_columns_exclude_leakage_set`,
`test_label_and_id_not_in_feature_columns`.

### F-LK-04 — `payment_ratio` / `outstanding_ratio` provenance — **info (documented, not a leak)**
Both are derived from cross-sectional snapshot totals
(`total_paid/amount`, `outstanding_principal/amount`). Per the owner's ruling
(`schema.py:20-27`) the source is a **single snapshot**, not a monthly panel,
so these are "current as of snapshot" values — legitimate snapshot signal, not
post-outcome panel leakage.

**Caveat (documented):** `outstanding_ratio ≈ 0` strongly indicates a Fully
Paid loan (non-default), so the model can learn a powerful non-default signal
from it. That is snapshot information, not a label leak, so it is **not** a
blocker — but it means a naive interpretation of "feature importance" will
over-weight `outstanding_ratio`. Worth a note in the model docs; not a QA gate.

### F-LK-05 — Vintage train/test windows do not overlap — **PASS**
`_vintage_split()` produces disjoint train/test indices, and when the split is
a real vintage split (≥2 cohorts) the train/test years are disjoint. Backed by
`test_vintage_split_windows_disjoint`.
**Note:** vintage year is reconstructed from `loan_age` + `as_of_date`
(`issue_year_from_frame`) because the frozen `FeatureFrame` has no `issue_date`.
The reconstruction uses integer month-floor division, so it is **month-granular
approximate** (see F-PI-04 for the observable side-effect). The split is
self-consistent but only as precise as the reconstruction allows.

---

## 4. Pipeline integration (`test_pipeline_integration.py`)

### F-PI-01 — End-to-end ingest→features→model→ranking→payload — **PASS**
The full chain runs on the synthetic fixture and produces a
`DashboardPayload`-shaped dict with the three required keys, JSON-serializable
end to end, `recommended_action ∈ {call, watch, auto-cure}`. Backed by
`TestEndToEndPipeline`.

### F-PI-02 — `portfolio_health.status_mix` sums to 1.0 — **PASS**
On both the live pipeline output and the committed fixture. Backed by
`test_fixture_status_mix_sums_to_one`.

### F-PI-03 — Dashboard fixture is a real run, not the p=1.0 placeholder — **PASS**
`dashboard/fixtures/sample-payload.json`: 20 rows, all `Very High`/`call`,
`p_default ∈ [0.9711, 0.9815]`, sorted descending. This is the top-N of a real
scoring run (`rank()` sorts by `p_default` desc), **not** the old placeholder
(which was `p_default = 1.0` for every row). The placeholder shape is asserted
gone. Backed by `test_fixture_all_one_band_is_a_real_run_not_placeholder`.

> The fixture is legitimately all-`Very High` because it is the **top-20** of the
> scored book — the highest-risk tail. That is the dashboard's purpose (the
> work-list), so a single band is expected here, not a bug.

### F-PI-04 — Spurious `2024` vintage cohort key (reconstruction drift) — **MINOR**
**Severity: minor.** `portfolio_health.vintage_default_rate` in the fixture
contains a `"2024"` key, but the source `loans` table has `issue_date` only in
2021–2023. `"2024"` is an artifact of `issue_year_from_frame()`'s month-floor
reconstruction: `floor((as_of_year*12 + as_of_month − loan_age) / 12)` can
round a late-2023 issuance with small `loan_age` up into 2024.
**Impact:** one extra (small) cohort bucket in the vintage chart; misleading
but not wrong-data. **Fix (out of scope):** reconstruct with day granularity,
or carry `issue_date` through to the scored table for cohort math.
**Backing:** `test_fixture_vintage_keys_are_plausible` (xfail sentinel — flips
to XPASS when fixed).

### F-PI-05 — Vintage `Alert.segment` shape mismatch (Python ↔ TS) — **MINOR**
**Severity: minor.** `waspada/insight/ranking.py:209` emits vintage
deterioration alerts with `segment = {"vintage": "<year>"}`. The Python
contract (`Alert.segment: Optional[Dict[str, str]]`) is permissive so this is
legal on the server side. But `dashboard/src/types.ts:51` narrows
`Alert.segment` to `Segment | null` where `Segment = {product, region}`. A
vintage alert's segment has **no** `product`/`region` keys, so a TS consumer
reads `segment.product`/`segment.region` as `undefined`.
**Impact:** frontend rendering of a vintage alert's segment would show
undefined/blank; the narrowing guard `isDashboardPayload` only checks
top-level keys so the payload still loads. The fixture currently has only an
`npl_ratio` alert (`segment: null`), so this is latent — it bites the first
time a vintage alert fires.
**Fix (out of scope):** either widen the TS `Alert.segment` to
`Record<string, string> | null` to match the permissive Python contract, or
have `ranking.py` emit segment as `{product: "", region: "", vintage: "<year>"}`.
**Backing:** `test_python_contract_allows_vintage_segment_dict` documents the
server-side permissiveness.

---

## 5. Dashboard (manual review)

### F-DM-01 — Unreferenced stale placeholder fixture — **NIT**
`dashboard/public/sample-payload.json` (untracked, 23,188 bytes) is the **old
placeholder**: 100 rows, `p_default = 1.0`, `score_band = "Medium"`,
`recommended_action = "call"` for every row. The loader
(`src/lib/payload.ts:27`) reads `fixtures/sample-payload.json`, **not** this
file, so it is unreferenced scratch. Harmless, but it is exactly the
placeholder shape the task body asked me to flag, and a stray copy in
`public/` could be fetched by mistake. **Recommend:** delete it (route to
WA-011 owner).
**Evidence:** `sha256(public/sample-payload.json) ≠ sha256(fixtures/sample-payload.json)`;
first row `p_default=1.0, score_band="Medium"`.

### F-DM-02 — `types.ts` mirrors the Python schema — **PASS (with the F-PI-05 caveat)**
Field names match `schema.py` exactly (`loan_id`, `p_default`, `score_band`,
`segment.{product,region}`, `recommended_action`; `PortfolioHealth.{npl_ratio,
vintage_default_rate, status_mix}`; `Alert.{metric,value,threshold,message,
segment}`). The one divergence is the `Alert.segment` narrowing (F-PI-05).

### F-DM-03 — Dashboard type-checks clean — **PASS**
`npx tsc -b --noEmit` exits 0 (run with the playwright-bundled node v22.11.0,
since no system node is installed in the worker). No type errors.

---

## 6. Benchmark (`data/benchmark.json` / `bench/LAST_RUN.json`)

### F-BM-01 — The "3.9× claim" premise is stale; README is honest — **info (resolved)**
The task body expected a README claim of "cuDF ~3.9× faster than pandas at 4M
rows" to compare against `data/benchmark.json`. **No such claim exists.** As of
HEAD (`901466e`, landed during QA by the WA-007 worker):

- `data/benchmark.json` is a **stale placeholder** (gitignored under `/data/`)
  that points to `bench/LAST_RUN.json` as the source of truth.
- `bench/LAST_RUN.json` reports CPU-only timings honestly:
  - 100k rows: features 0.21s, model 12.0s (GPU `not_run`)
  - 1M rows: features 1.46s, model 27.1s (GPU `not_run`)
- The README (`README.md:118-140`) explicitly states feature engineering is
  **0.79× (GPU slower)** at this scale and training is **~1.2×**, with an
  honesty note that the GPU column is `not_run`, not fabricated.

The earlier in-tree `data/benchmark.json` numbers (`cpu_features_s=0.268`,
`gpu_features_s=0.341`, `train_speedup=1.2`) were consistent with the current
honest README and have simply been superseded by the reproducible harness. No
discrepancy between claimed and evidenced numbers. **No finding.**

> The benchmark harness itself is out of scope per the ticket (WA-007 is
> self-tested). This section only checks claim-vs-evidence consistency.

---

## 7. Things explicitly checked and found clean

- **Secret/credential leak audit:** `.env` and `secrets/bq-key.json` were **not**
  read, printed, or committed. BQ creds were used solely to authenticate the
  live smoke queries. No secrets appear in any test, report, or committed
  output. **No leak.** (P0-leak watch: clear.)
- **No fabricated data:** every number in this report comes from a real query
  or a real test run; repros are listed per finding.

---

## Severity rollup

- **Blockers:** 0
- **Major:** 1 (F-DV-05 — biased LIMIT sample; does not block the shipped full-table pipeline)
- **Minor:** 2 (F-PI-04 spurious vintage key; F-PI-05 alert segment shape)
- **Nits:** 1 (F-DM-01 unreferenced stale fixture)
- **Info (documented/passing):** F-LK-01, F-LK-04, F-BM-01

## Reproducing this report

```
cd /workspace
PYTHONPATH=/workspace python -m pytest tests/qa/ -v          # 36 passed, 1 xfailed
PYTHONPATH=/workspace python tests/qa/_bq_probe.py            # live DQ evidence
PYTHONPATH=/workspace python tests/qa/_probe_sampling.py      # F-DV-05 repro
```
BQ creds must be present in `/workspace/.env` for the live tests; they skip
cleanly otherwise.
