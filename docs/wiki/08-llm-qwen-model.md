# LLM / Qwen Model

> The reasoning brains that power the [debate](04-debate-mechanism.md). WASPADA runs
> on **Qwen** (via Qwen Cloud / Alibaba DashScope) in production and on a
> deterministic **mock brain** everywhere else — one interface, two backends.

## 1. One interface, two backends

Defined in [`waspada/agents/llm.py`](../../waspada/agents/llm.py):

| | `MockLLM` | `QwenLLM` |
|---|-----------|-----------|
| Backend | none (in-process) | DashScope OpenAI-compatible endpoint |
| Determinism | total | model-dependent |
| Native tool-calls | scripted | real OpenAI `tool_calls` |
| Selected by | default / tests | `WASPADA_LLM_PROVIDER=qwen` or `brain=qwen` |

`get_llm(provider)` picks the backend; tests inject a `MockLLM` directly and never
touch the network.

## 2. Qwen via DashScope (OpenAI-compatible)

`QwenLLM` wraps the official **`openai` SDK** pointed at DashScope's
compatible-mode `base_url` — Qwen Cloud's own documented quickstart path, and the
most practical route since agent tooling already assumes an OpenAI-shaped client.

- The SDK is imported **lazily inside `__init__`**, so the module imports cleanly
  with no SDK installed and a cold container start doesn't pay the import cost until
  a request actually reaches for Qwen.
- API key from `DASHSCOPE_API_KEY`. Absent it, `QwenLLM` raises a clear error
  pointing to the mock brain — never a bare 500.

## 3. Model tiering — cognitive load (`flash` / `plus` / `max`)

Different agents need different horsepower, so brains are **tiered by cognitive
load** and only pay for what each step needs:

| Tier | Default model | Agents | Why |
|------|--------------|--------|-----|
| **flash** | `qwen3.6-flash` | Data Engineer, **Skeptic** | cheap, one-shot structured challenge |
| **plus** | `qwen3.7-plus` | Data Analyst, **Actuary rebuttal** | mid — SQL exploration, defence |
| **max** | `qwen3.7-max` | **Arbiter ruling** | top tier for the final adjudication |

`with_model(tier)` returns a tier-specific clone sharing one HTTP client + key
(only the model id changes, so the audit log records the real brain per agent). The
tiers are env-overridable (`QWEN_MODEL_FLASH` / `_PLUS` / `_MAX`), defaulting to the
documented ids.

## 4. Native function calling (WA-041)

The `chat(prompt, *, tools, messages)` surface exposes real **OpenAI `tool_calls`**.
The Skeptic runs a bounded loop: Qwen decides *when* to call `portfolio_stats` /
`lookup_account`, the results are fed back as tool messages, and the final answer is
grounded in real evidence — not hard-wired Python. A capability flag
(`supports_native_tools`) lets an agent branch native-vs-legacy **without consuming a
scripted reply**, which keeps deterministic tests deterministic.

## 5. JSON-mode reliability

The debate protocol needs structured output. On Qwen, `response_format:
json_object` (JSON mode) plus tolerant parsing (extract the first `{…}` blob, fall
back gracefully) means an occasional prose wrapper never crashes a round — an
unparseable reply degrades to a skip, not a failure.

## 6. Egress control (WA-045)

A security rail: `QwenLLM` validates its `base_url` against an **allow-list**
(`dashscope.aliyuncs.com` / `dashscope-intl.aliyuncs.com`) *at construction*. A
prompt injection that flips `DASHSCOPE_BASE_URL` to an attacker-controlled host is
rejected before any client is built — **loan data only ever goes to DashScope.**

## 7. Cost & latency discipline

- The debate's **bounded round ceiling** (≤ K×6 LLM calls) caps spend deterministically.
- **Parallel audits** (WA-080) on the thread-safe Qwen client collapse wall-clock so
  a live debate finishes inside the FC timeout.
- Tiering means the cheap flash brain does the bulk (audits), and only the K final
  rulings hit the expensive max tier.

## 8. Why "brain" is swappable

Because the harness talks to one `LLM` interface, the same agent code runs on the
free deterministic mock (tests, offline demo, CI) and on live Qwen (the real
debate) — no branching in the agents. Selecting `brain=mock` vs `brain=qwen` on the
API is the only switch.

> The platform is **Qwen Cloud** (docs.qwencloud.com), backed by Alibaba Cloud
> DashScope / Model Studio infrastructure — the hackathon's designated stack.

**Related:** [Harness Architecture](03-harness-architecture.md) ·
[Debate Mechanism](04-debate-mechanism.md) · [Tech Stack](05-techstack.md)
