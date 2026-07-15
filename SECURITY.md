# WASPADA Security · Trust Boundary & Egress Controls (WA-045)

This document defines the **trust boundary** for the WASPADA Agent Society —
which external endpoints each agent may call, how loan data is contained, and
the defense-in-depth guardrails enforced in code.

---

## 1. Endpoint allowlist per agent

| Agent | Brain (LLM) | Allowed egress endpoint | Rationale |
|---|---|---|---|
| **Data Engineer** | `qwen3.6-flash` (QwenLLM) or MockLLM | `dashscope.aliyuncs.com` / `dashscope-intl.aliyuncs.com` | Lightweight triage; reads raw snapshot only |
| **Data Analyst** | `qwen3.7-plus` (QwenLLM) or MockLLM | `dashscope.aliyuncs.com` / `dashscope-intl.aliyuncs.com` | Runs DuckDB explorations; builds FeatureFrame |
| **Risk Auditor** | `qwen3.7-max` (QwenLLM) or MockLLM | `dashscope.aliyuncs.com` / `dashscope-intl.aliyuncs.com` | Adjudicates risk; reads analyst aggregates |
| **Actuary** | `qwen3.7-plus` (QwenLLM) or MockLLM | `dashscope.aliyuncs.com` / `dashscope-intl.aliyuncs.com` | Pricing / reserve reasoning |
| **Credit Arbiter** | `qwen3.7-max` (QwenLLM) or MockLLM | `dashscope.aliyuncs.com` / `dashscope-intl.aliyuncs.com` | Final ruling |
| **MockLLM** (offline/tests) | — | **None** (no network) | Deterministic, network-free; used in all CI/test runs |

**No agent in the society is allowed to call any endpoint other than the
DashScope (Alibaba Cloud) API.** The `base_url` is validated at `QwenLLM`
construction time — see §3 below.

---

## 2. Loan data containment

**Loan data never goes anywhere except DashScope.** Specifically:

- **Raw loan records** (RawLoans: amounts, rates, grades, DTI, regions,
  payment totals) are loaded into an in-memory DuckDB Lakehouse and queried
  locally. They leave the process **only** inside prompts sent to the Qwen
  LLM at `dashscope(-intl).aliyuncs.com`.
- **Feature values** (FeatureFrame: payment_ratio, outstanding_ratio,
  loan_age, etc.) are derived locally by `build_features()` and likewise
  only appear in LLM prompts sent to DashScope.
- **No other outbound channel exists.** There is no webhook, no callback
  URL, no email gateway, no third-party analytics SDK. The only network
  client in the codebase is the `openai.OpenAI` SDK pointed at DashScope.
- **Object storage** (OSS) is used for pipeline artifacts (Arrow/Parquet
  snapshots) within the same Alibaba Cloud region — this is internal data
  plane traffic, not external egress.

---

## 3. Defense-in-depth egress controls (enforced in code)

### 3a. LLM `base_url` allowlist — `waspada/agents/llm.py`

`QwenLLM.__init__` validates that the resolved `base_url` (whether passed
explicitly or read from `DASHSCOPE_BASE_URL`) contains one of the allowed
DashScope domains:

```python
_ALLOWED_BASE_DOMAINS = (
    "dashscope.aliyuncs.com",       # CN endpoint
    "dashscope-intl.aliyuncs.com",  # international endpoint
)
```

Any other value raises `ValueError` at construction time — **before** the
OpenAI client is created, so no data can leave the process. This blocks an
attacker who flips `DASHSCOPE_BASE_URL` via environment injection from
redirecting loan data to an arbitrary host.

`MockLLM` has no network path at all — it is the default brain and is used
in every test / offline run.

### 3b. DuckDB SQL column allowlist — `waspada/agents/data_analyst.py`

The Data Analyst's `_safe_sql_check` enforces three layers before any
LLM-composed SQL reaches DuckDB:

1. **SELECT-only** — rejects `DROP`, `INSERT`, `UPDATE`, `COPY`, etc.
2. **No chained statements** — rejects `SELECT ...; DROP TABLE ...`.
3. **Column allowlist** (`_check_column_allowlist`) — every identifier in
   the query must be either a SQL keyword/function, a known table name
   (`raw_loans`, `feature_frame`), or a column from the frozen
   RawLoans/FeatureFrame contract. This stops a prompt-injection payload
   from composing a query that reads or aliases arbitrary column names
   (e.g., `SELECT secret_api_key FROM ...`).

Column aliases defined via `AS` and subquery table aliases are recognised
as locally-scoped names so legitimate analytic queries are not blocked.

### 3c. MCP server — local stdio only

The MCP (Model Context Protocol) server used by the Risk Auditor for
evidence-base lookups runs as a **local subprocess over stdio**
(`StdioClient`). There are **no external MCP server connections** — no
TCP, no WebSocket, no remote MCP URL. The subprocess arguments are
hard-coded and not controllable by user input or LLM output. This is
out of scope for network egress: it never leaves the host.

---

## 4. What is NOT in scope

| Item | Why |
|---|---|
| Network-level egress firewall rules (VPC/FC config) | Separate infra ticket (IaC / OpenTofu) |
| Prompt injection detection at the LLM layer | Research-grade; the SQL column allowlist is the pragmatic mitigation |
| MCP-over-network | WASPADA uses stdio only — no external MCP connections in the demo |

---

## 5. Verification

The egress controls are tested in `tests/test_egress_controls.py` (18 tests):

- `TestQwenBaseUrlAllowlist` — blocked URLs raise `ValueError`; valid
  DashScope endpoints (intl + CN) are accepted.
- `TestSqlColumnAllowlist` — unknown columns blocked; valid contract
  columns, aggregates, aliases, and subqueries pass; non-SELECT and
  chained statements still blocked.
- `TestNormalOperationUnaffected` — the full Data Analyst function-calling
  loop still works end-to-end; a blocked column degrades gracefully.

Run: `.venv/Scripts/python.exe -m pytest tests/test_egress_controls.py -v`
