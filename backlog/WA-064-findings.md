---
id: WA-064
state: review
priority: P1
owner: backend-qa
branch: feature/wa-064-qa-todays-batch
contract_frozen: false
---

# WA-064 · QA review of merged batch (WA-050/051/047p/024/032)

Adversarial QA performed on `develop` after the five tickets merged. Findings are
advisory; Stefanie decides what ships.

## Summary

| Ticket | Verdict | Notes |
|--------|---------|-------|
| WA-050 | **Mostly solid** | Round-trip holds; prompts cite drivers. Synthetic known-coefficient exploit passes. |
| WA-051 | **Mostly solid** | Absolute edges work; relative fallback byte-identical. One edge-case gap in `_risk_level_bands`. |
| WA-047 | **Partial; doc/code drift remains** | Dead `dlt` code removed, but `requirements.txt` still pulls `dlt`, README/HACKATHON still describe dlt as present, and OSS+DuckDB pushdown is not implemented. |
| WA-024 | **Solid** | Expected loss flows scoring → ranking → health → payload; older payloads degrade gracefully. |
| WA-032 | **Mostly solid** | JSON policy validation is fail-loud. Minor UX-ish gaps in malformed-value error messages. |

No P0 blockers. All targeted tests pass (`test_wa050_introspection.py` 7 passed, `test_wa051_band_edges.py` + `test_policy.py` 18 passed, `test_lakehouse.py` 7 passed).

---

## WA-050 · Actuary introspection

### Verified
- `explain()` reconstructs the logit exactly: `intercept + Σ contributions` matches `logit(p_default)` for real fitted models (existing test) and a synthetic known-coefficient model (new exploit).
- Top-N ranking is by `|contribution|` descending, labels carry `feature=value`, inactive one-hot terms are dropped.
- Degrades to `[]` on missing `loan_id`, missing pipeline, or unfitted model.
- `_rebuttal_prompt()` and the Skeptic's `_prompt()` include the driver line when a model handle is present.
- Existing tests assert "drivers" appears in the prompt and the first driver token is rendered.

### Exploit / adversarial finding
**Synthetic known-coefficient model test passes.** I forced a `Pipeline` with a known coefficient vector and identity StandardScaler, then confirmed `explain()` returns the expected signed contributions in the correct order and that the logit round-trip is exact. This proves the implementation is not just memorizing the fitted model's shape; it is the actual linear decomposition.

### Risk / low-priority note
- `explain()` silently drops zero-contribution terms. This is fine for prompt density, but a dashboard that wants to show "all drivers" would need a different top-n setting.
- No validation that `model["pipeline"]` is actually the same pipeline that produced `scored` — if a stale model artifact is passed, the drivers will be silently wrong. This is a usage hazard, not a code defect; the current callers pass the in-process model handle.

---

## WA-051 · Absolute band edges

### Verified
- `train()` persists `band_edges` (the `[20,40,60,80]` percentiles of the training book).
- `predict()` on the same training frame is byte-identical with and without edges (existing regression test).
- Edge-less artifacts fall back to relative quintiles.
- A uniformly healthy synthetic batch scored against a mixed-reference model produces no `Very High` labels and no spurious disputes; the same batch under relative banding forces `Very High` and disputes (existing headline test).
- The Skeptic's prompt states the absolute thresholds when edges are present.

### Exploit / adversarial finding
**Boundary and NaN handling in `_risk_level_bands` is inconsistent with `predict()` clipping.**

- `predict()` clips probabilities to `[0, 1]` and replaces `NaN` with `0` before calling `_risk_level_bands`. However, `_risk_level_bands` itself is a public-ish primitive and does **not** clip or sanitize. If called directly with out-of-range probabilities or NaNs, the behavior is surprising:
  - `NaN` falls through every `<=` comparison and lands in `Very High`.
  - Negative probabilities (`-0.1`) are `<= edges[0]` and become `Very Low`.
  - Probabilities `>1.0` (e.g. `1.1`) become `Very High`.

This is not currently reachable through the main pipeline, but it is a latent correctness hazard if the function is reused elsewhere (e.g. direct scoring of a model artifact, future batch scoring, or external tools). The fix is cheap: add `np.clip(np.nan_to_num(probs, nan=0.0), 0.0, 1.0)` inside `_risk_level_bands` so the primitive is safe regardless of caller.

**Recommendation:** harden `_risk_level_bands` to sanitize inputs, or at least document the precondition that probabilities must be finite and in `[0,1]`.

---

## WA-047 · Lakehouse = OSS + DuckDB (partial)

### Verified
- The dead `dlt` code (`dlt.readers.filesystem`, `source_url`, `_oss_s3_endpoint`) is **removed** from `waspada/data/lakehouse.py`. `git grep` for these strings returns only backlog/docs and the guard test.
- `load_to_duckdb` honestly registers an in-memory Arrow table or a local Parquet file; no source raises a clear `RuntimeError`.
- `get_analytics_connection()` falls back to local DuckDB when `DUCKDB_RDS_ENDPOINT` is unset/blank; the RDS path is lazy-imported and only runs when explicitly configured.
- `test_lakehouse.py` passes, including the AST-based guard that `dlt` is not imported and `_oss_s3_endpoint` is not present.

### Exploit / adversarial finding
**There is still a live dependency on `dlt` in `requirements.txt` (line 15: `dlt[duckdb]>=1.4`).** The code no longer uses it, but every install/pull still drags it in. This contradicts the "honest / no longer pretended" claim and creates a future trap where someone re-imports it because it is already in the environment. Similarly, `README.md` and `HACKATHON.md` still describe the lakehouse as "dlt + DuckDB" even though the dlt path is dead.

**Remaining `dlt` references in the working tree (39 hits):**
- `requirements.txt:14-15` — still lists `dlt[duckdb]` as a lakehouse dependency.
- `README.md:152,306` — still says lakehouse is dlt + DuckDB.
- `HACKATHON.md:140,328-336,465` — still describes dlt pipeline and the Data Engineer as "dlt/DuckDB check core".
- `backlog/WA-029.md`, `backlog/WA-030.md`, `backlog/BRIEF-2026-07-14.md` — historical backlog docs, less critical.
- `waspada/agents/data_engineer.py:11` — module docstring still says "Load the snapshot via the lakehouse layer (dlt + ..."). The implementation does not use dlt, but the docstring is misleading.
- `waspada/data/lakehouse.py:18-19` — docstring correctly says no dlt; OK.
- `waspada/agents/data_engineer.py:134,391` and `waspada/agents/data_analyst.py:532` — comments say "no dlt, no network"; OK.
- `tests/test_lakehouse.py:71,91-106` — the guard tests that keep it honest; OK.
- `backlog/WA-069.md:40` — references no dlt remnants; OK.

**OSS+DuckDB read path is still bulk-download, not pushdown.** `waspada/data/oss.py` downloads the whole object via `get_object().read()`, then `pq.read_table()`, then applies `limit` client-side. There is no `httpfs` / `read_parquet('s3://...')` anywhere in the code. This matches the WA-047 partial-shipped status, but the docs still claim a pipeline that is not implemented.

**Recommendation:**
1. Remove `dlt[duckdb]>=1.4` from `requirements.txt` (or move it to a `[duckdb]` extra if a future ticket wants it).
2. Update `README.md` and `HACKATHON.md` to stop describing the lakehouse as dlt-backed. The Data Engineer is a function-calling loop over DuckDB over an in-memory Arrow table from OSS bulk download.
3. Fix the `waspada/agents/data_engineer.py` module docstring to match the actual implementation.

---

## WA-024 · Expected loss end-to-end

### Verified
- `expected_loss` is computed as `p_default × 0.45 × outstanding_principal` on every work-list row.
- `total_expected_loss` is the portfolio sum and appears in `PortfolioHealth`.
- Both values are additive optional: if `outstanding_principal` is absent, the work-list rows omit `expected_loss` and health omits `total_expected_loss`.
- Older payloads without the field render correctly (the frontend `types.ts` marks them optional).
- The full pipeline (`IngestAgent → AnalyticsAgent → RiskModelAgent → rank/segment_health`) produces a JSON-serializable payload with both keys present.
- The API's `_build_demo_orchestrator` loads `load_policy(os.environ.get("WASPADA_POLICY_FILE"))`, so policy can override thresholds, but not EL computation (LGD is still hard-coded at 0.45).

### Exploit / adversarial finding
**No real exploit found.** The additive optionality is honored. However, `EXPECTED_LOSS_LGD = 0.45` is a module constant and is not configurable via `RiskPolicy`. The WA-032 ticket explicitly noted "LGD / Expected-Loss config — EL is not computed in the backend yet (WA-024 is frontend-label-only)" — but WA-024 actually added the backend computation. If the owner wants a configurable LGD, the policy should gain an `lgd` field. For now it is correctly labeled as an assumption in the UI.

### Risk / low-priority note
- `outstanding_principal` is reconstructed from `outstanding_ratio × amount`. Both are snapshot fields, so this is reasonable for amortizing installment loans, but it is not the actual `outstanding_principal` from `RawLoans`. If `RawLoans` later carries a more precise field, the reconstruction should be replaced.

---

## WA-032 · JSON decision matrix

### Verified
- `RiskPolicy.default()` exactly matches the hard-coded constants `ACTION_BY_BAND`, `DEFAULT_NPL_THRESHOLD`, `DEFAULT_VINTAGE_THRESHOLD`, `_NPL_BUCKETS`.
- The committed `waspada/policy/default_policy.json` matches `RiskPolicy.default()`.
- `load_policy` rejects out-of-vocabulary actions, unknown bands, out-of-range thresholds, missing files, empty `band_to_action`, non-object JSON, and lowercase band keys.
- Editing `band_to_action` changes `rank()` output without code changes.
- Editing thresholds/buckets changes `alerts()` / `segment_health()` output without code changes.
- `InsightAgent` applies the policy when wired via `Orchestrator(..., policy=load_policy(...))`.
- The API (`api/main.py`) uses `load_policy(os.environ.get("WASPADA_POLICY_FILE"))`, so the demo can be run with a custom policy file.

### Exploit / adversarial finding
**Malformed-value error messages are inconsistent.**
- Invalid actions, unknown bands, and out-of-range numeric thresholds all raise `ValueError` naming the offending field/value.
- However, if `npl_threshold` is a non-numeric string (e.g. `"high"`), the raw `float()` conversion error `"could not convert string to float: 'high'"` is surfaced **without** the field name. A human editing the JSON will not know which field is wrong.
- Similarly, `npl_buckets` values are coerced to strings via `str(b)`, so an integer bucket like `123` becomes `"123"` and is accepted silently. This is defensible (defense in depth), but the error message could be clearer.

**Recommendation:** wrap the threshold float coercion in `try/except` and raise `ValueError(f"RiskPolicy: npl_threshold={raw!r} is not a number")` (same for `vintage_threshold`). Also consider validating that `npl_buckets` values look like delinquency bucket strings, not arbitrary integers.

### Injection test
- Crafted action values like `"<script>alert(1)</script>"` are rejected because they are not in `VALID_ACTIONS`. The JSON is not rendered directly in the UI, so even if accepted it would not execute; the validation keeps the matrix vocabulary safe.

---

## Test log

```
$ cd /workspace && python3 -m pytest tests/test_wa050_introspection.py -q --tb=short
7 passed in 4.00s

$ python3 -m pytest tests/test_wa051_band_edges.py tests/test_policy.py -q --tb=short
18 passed in 2.90s

$ python3 -m pytest tests/test_lakehouse.py -q --tb=short
7 passed in 0.67s
```

Additional adversarial scripts executed successfully:
- `WA-050` synthetic known-coefficient decomposition round-trip.
- `WA-051` edge/threshold/NaN/constant probes.
- `WA-024` full pipeline EL computation and additive optionality.
- `WA-032` malformed JSON, injection, threshold-type probes.

---

## Recommended next actions

1. **WA-051:** Harden `_risk_level_bands` input sanitization (or document the precondition).
2. **WA-047:** Remove `dlt[duckdb]` from `requirements.txt` and clean up README/HACKATHON/Data Engineer docstring.
3. **WA-032:** Improve `load_policy` error messages for non-numeric thresholds.

None of these are user-facing bugs today; they are cleanliness and defensibility gaps that could bite as the surface is reused.
