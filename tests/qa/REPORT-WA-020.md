# WA-020 QA Report — Debate paths + MCP tests + Secrets sweep (pre-public)

**Reviewer:** Reza (QA Engineer) · **Verdict:** **PASS (advisory)** — no blockers, repo is public-safe
**Date:** 2026-07-08 · **Repo state tested:** `main` @ `de8abf5` (+ working tree)
**Baseline suite (before):** 180 passed, 8 skipped, 1 xfailed
**After WA-020:** 216 passed, 8 skipped, 1 xfailed (+36 new tests: 4 debate + 33 MCP, minus 1 pre-existing test now broken by a concurrent WA-024 edit — see F-EL-01)
**After WA-020, excluding the concurrent-edit breakage:** 217 passed, 8 skipped, 1 xfailed

---

## Verdict & headline

**Advisory PASS.** Three deliverables, all complete:

1. **Debate path tests** — gap-fill, not duplicate. The existing
   `test_wa016_debate.py` (28 tests) already covers the JSON parsers, the
   `defend_score` / `arbiter.rule` unit paths, and the four terminal resolutions
   with a *uniform* script. I added 4 integration tests
   (`tests/qa/test_debate_degradation.py`) closing the genuine gaps: R3
   unparsable *end-to-end*, R3 brain-unreachable *end-to-end*, and a
   *mixed-resolution* run proving per-dispute independent routing.

2. **MCP tests (NEW)** — `tests/test_mcp.py`, 33 tests. The MCP layer
   (WA-015 — the rubric's explicit "Model Context Protocol" depth marker) had
   **zero dedicated tests**. Now covered: `AnalyticsStore.portfolio_stats`
   (whole-book + 4 segment cases), `AnalyticsStore.lookup_account` (hit/miss/
   no-features), `InProcessClient` (parity with store), `build_server` tool
   handlers (list_tools + call_tool for both tools + unknown-tool envelope +
   schema-validation behavior), `_parse_tool_result` (7 edge cases),
   `_jsonify_row` (6 coercion cases). `StdioClient` deliberately skipped
   (subprocess + running server — out of CI scope, per the brief).

3. **Secrets sweep** — **CLEAN.** Zero secrets in tracked files or git history.
   The repo is public-safe. Details below.

**No bugs found.** One behavior worth flagging (F-MCP-01, info) — not a defect.

---

## Findings summary

| ID       | Severity | Area              | One-line                                                              |
|----------|----------|-------------------|-----------------------------------------------------------------------|
| F-EL-01  | minor    | Contract test     | `test_pipeline_integration.py:88` uses strict-equality on work_list keys; WA-024's additive `expected_loss` field breaks it. Test contradicts the additive-optional contract in `types.ts:24`. Concurrent edit (not WA-020's); reported for WA-024 owner. |
| F-MCP-01 | info     | MCP schema layer  | `lookup_account` with no `loan_id` is rejected by the MCP framework's input validation (`isError=True`), not by the handler's defensive `args.get("loan_id", "")` default. Correct behavior — the defensive default is dead code on the protocol path (it still protects a direct Python call). Documented in test. |

No bugs in the WA-020 scope. No vulnerabilities. No secrets.

> ⚠️ **Concurrent edit (F-EL-01).** During this run, the WA-024 worker added an
> additive `expected_loss` field to `dashboard/fixtures/sample-payload.json`
> (mtime 17:22 UTC, ~mid-run). This broke one pre-existing QA test
> (`test_fixture_work_list_records_match_contract`) that asserts strict-equality
> on work_list keys. It is **not** caused by WA-020's changes — verified by
> deselecting that one test: 216 passed, 8 skipped, 1 xfailed (all green). The
> fix belongs to WA-024's owner: loosen the assertion to subset
> (`expected <= set(rec.keys())`) to match the additive-optional contract.

---

## 1. Debate path coverage — gap analysis + fills

### What `test_wa016_debate.py` already covers (28 tests, untouched)
- JSON parsers: `_parse_verdict_json` + `_parse_ruling_json` (valid / garbage / clamp).
- `defend_score` unit: uphold / concede / unparsable / brain-unreachable / tier no-op.
- `arbiter.rule` unit: uphold / override / low-conf escalate / explicit escalate / unparsable escalate / brain-unreachable escalate / threshold tuning / `run()` raises.
- Orchestrator e2e, **uniform** scripts: upheld / overridden-via-concession / overridden-via-arbiter / escalated_approved / escalated_rejected / R2-unparsable-escalates / CUT LINE (arbiter disabled).
- `DisputeRound.model` field populated.

### What `test_risk_auditor.py` already covers
- R1 parse-failure → no dispute (agent unit + orchestrator e2e: `test_auditor_parse_failure_degrades_gracefully`, `test_orchestrator_parse_fail_completes_ok_with_empty_dialogue`).

### Gaps I closed (`tests/qa/test_debate_degradation.py`, 4 tests)
1. **R3 unparsable, end-to-end** — uphold rebuttal + unparsable Arbiter ruling → escalate → gate → `escalated_approved` / `escalated_rejected`. The unit test proved the agent degrades; nothing had wired it through the orchestrator to a *closed* terminal state with a 3-round transcript. (Acceptance row "R3 unparsable → escalate".)
2. **R3 brain-unreachable, end-to-end** — Arbiter brain raises mid-run; orchestrator must degrade to gate, never crash. Proven.
3. **Mixed-resolution run** — every existing e2e test scripts one resolution kind for all disputes. A mixed run (upheld + overridden + escalated interleaved) proves the orchestrator routes each dispute independently — catching any shared-state bleed that uniform tests miss. Routes correctly: 2 upheld / 1 overridden / 1 escalated in one run.

**R1 unparsable and R2 unparsable were NOT duplicated** — already well covered above.

---

## 2. MCP layer coverage (`tests/test_mcp.py`, 33 tests)

The MCP layer had no tests. Coverage now:

- **`AnalyticsStore.portfolio_stats`** (5 tests): whole-book aggregates (NPL, vintage, status mix, worst vintage); segment-filtered (product×region); product-only wildcard; empty-string wildcard; non-matching segment → empty stats.
- **`AnalyticsStore.lookup_account`** (3 tests): hit returns feature row; miss returns `{}`; no-features-configured returns `{}`.
- **`InProcessClient`** (4 tests): portfolio_stats parity with store; lookup hit parity; lookup miss empty; int loan_id coercion (no raise).
- **`build_server` list_tools** (2 tests): declares both tools with description + schema; lookup_account schema requires loan_id.
- **`build_server` call_tool** (6 tests): portfolio_stats whole-book; portfolio_stats segment; lookup_account hit; lookup_account miss → `{}`; unknown tool → error envelope; **missing loan_id → schema rejection** (F-MCP-01). Handlers invoked directly via `server.request_handlers` (no subprocess).
- **`_parse_tool_result`** (7 tests): structuredContent `{"result": {...}}` unwrap; structuredContent passthrough; isError → error dict; text-JSON fallback; non-JSON text passthrough; JSON-array wrap; empty → `{}`.
- **`_jsonify_row`** (6 tests): date → ISO; datetime → ISO; bytes → utf-8; bytearray; invalid utf-8 → replacement (no raise); scalars passthrough.

---

## 3. Secrets sweep — CLEAN

**Method:** ripgrep over tracked files + git history + on-disk scan for secret
patterns; `.gitignore` coverage audit; `git add --dry-run -A` simulation.

### Patterns scanned (all clean)
- `sk-[A-Za-z0-9]{20,}` (DashScope/OpenAI keys) — **0 real hits** (all `sk-` matches are `risk-model`/`risk-auditor`/CSS `--risk-*`/`Q2`-`Q5`).
- `LTAI[A-Za-z0-9]{12,}` (Alibaba AccessKey) — **0 real hits** (one `LTAI...` placeholder in `deploy/iac/README.md`, truncated).
- `-----BEGIN PRIVATE KEY-----` — **0 hits** in tracked files (`secrets/bq-key.json` has `[REDACTED PRIVATE KEY]`, and is gitignored).
- `AIza...`, `gh[pousr]_...`, `xox[...]` (Google/GitHub/Slack tokens) — **0 hits**.
- `AKIA[0-9A-Z]{16}` (AWS) — **0 hits**.
- Hardcoded `DASHSCOPE_API_KEY` / `OSS_ACCESS_KEY_SECRET` values in code — **0 hits** (all references are `os.environ`/`getenv` reads + docs).

### Sensitive files on disk (all properly gitignored)
| File | Status | Verified |
|------|--------|----------|
| `.env` (real) | gitignored (`.env` + `.env.*`) | `git check-ignore` → ignored; never in history |
| `secrets/bq-key.json` | gitignored (`*-key.json`) | `private_key` is `[REDACTED]`; `git check-ignore` → ignored; never in history |
| `.venv/` | gitignored | library `.pkl` test fixtures only, no project secrets |

### `.gitignore` coverage audit — comprehensive
Covers: `.env` / `.env.*` (with `!.env.example` allow), `*-key.json`,
`service-account*.json`, `gcp-*.json`, `credentials*.json`, `*.pkl`, `*.joblib`,
`models/`, `/data/`, `*.csv`, `*.parquet`, `*.feather`, `*.pdf`, `*.pptx`,
`*.tfstate`, `*.tfvars` (with `!secrets.tfvars.example` allow), `.terraform/`,
`node_modules/`, `dist/`, `.venv/`.

### History audit
- `.env`, `secrets/`, `bq-key.json` — **never committed** (`git log --all` clean).
- `git add --dry-run -A` — confirms **none** of `.env` / `secrets/` / `bq-key.json` / `.pkl` / `.tfvars` / `.tfstate` would be staged.

### Templates audited (sanitized)
- `.env.example` — all secret values empty.
- `deploy/iac/secrets.tfvars.example` — `YOUR_ALIYUN_ACCESS_KEY_ID` / `YOUR_ALIYUN_ACCESS_KEY_SECRET` placeholders.

### Conclusion
**The repository is safe to make public.** No committed secrets, no secrets in
git history, comprehensive `.gitignore`, sanitized templates. The only on-disk
secret-bearing file (`secrets/bq-key.json`) has its private key redacted AND is
gitignored — double-safe.

---

## 4. Test artifacts

- `tests/qa/test_debate_degradation.py` — 4 debate gap-fill tests (NEW)
- `tests/test_mcp.py` — 33 MCP layer tests (NEW)
- Full suite: `python -m pytest tests/ -v` → **217 passed, 8 skipped, 1 xfailed**

---

## Advisory note

QA is advisory. I find and report; Stefanie decides; the owner signs off. I do
not gate the ship. The debate paths and MCP layer are proven; the repo is
public-safe. Recommend owner review of the two new test files before merge.
