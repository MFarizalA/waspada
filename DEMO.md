# WASPADA — demo video script (under 3:00)

Live app: (Function Compute URL lands with WA-018)

Total runtime target: **2:45**. Timestamps assume ~150 wpm; leave a few
seconds of slack, don't rush the debate beats.

---

## 0:00–0:15 — Hook (screen: title slide or blank)

> Multifinance lenders carry millions of installment loans. Risk analysts
> grind the whole book overnight in batch — by the time the work-list lands,
> it's already stale, and a pure-ML score gives no argument an analyst can
> defend to the collections head. WASPADA changes that: the riskiest calls
> arrive pre-argued — challenged, defended, ruled — by a society of AI
> agents, with a human holding the gate.

## 0:15–0:45 — What it is (screen: architecture diagram from HACKATHON.md)

> WASPADA is a two-tier system. A deterministic harness does the heavy work —
> fetch from Alibaba Cloud OSS, feature engineering, scoring, ranking. On top
> sits the agent society: a Risk Auditor that prosecutes the model's riskiest
> scores, an Actuary that defends them, and a Credit Arbiter that rules when
> they disagree. Every claim cites real evidence pulled live from the loan
> book via MCP. The whole debate runs under a fixed call budget, and nothing
> reaches a collector without a human sign-off.
>
> And the twist: WASPADA itself was built by a small AI company — Stefanie,
> a boss agent managing three worker agents — that planned, coded, tested,
> and shipped this system. Agents building agents, humans in control.

## 0:45–1:15 — The dashboard (screen: live URL, top half)

> This is the analyst's collections view. Twenty ranked accounts, each with a
> default probability, a risk band, and a recommended action. Portfolio NPL
> ratio, vintage default rates, breach alerts — all computed from the same
> pipeline the agents reason over. Nothing for the analyst to build by hand.

## 1:15–2:15 — The Agent Society panel (screen: scroll to debate — the hero beat)

> But here's what makes WASPADA different. The Risk Auditor — running on
> Qwen's cheapest tier, qwen3.6-flash — audits the top accounts independently
> and challenges the model where the story doesn't match the number.
>
> [Point at a dispute card] Look at this account. The Actuary scored it Very
> High, the riskiest level. The Risk Auditor disagreed — cited payment ratio, DTI,
> delinquency status — all pulled live via MCP tools. The Actuary defended its
> score on qwen3.7-plus, citing portfolio context. The Credit Arbiter on
> qwen3.7-max read both arguments and ruled: upheld. The human analyst sees
> the full transcript — who challenged, what evidence, who conceded — and
> approves a decision with reasons, not just a number.
>
> Every dispute has a cost ceiling: at most three rounds per account, bounded
> LLM calls, graceful degradation if a model fails to parse. No open-ended
> agent chatter.

## 2:15–2:35 — The efficiency story (screen: benchmark table — WA-017)

> We measured this honestly. The society scores 100% of the book with a
> classical model — zero LLM calls — then spends LLM budget only on the
> contested top-K. Against a single-agent baseline that calls the LLM once
> per account, WASPADA matches the recall on high-risk accounts at a fraction
> of the LLM calls. That's the measurable gain the track asks for. The honest
> boundary: multi-agent isn't worth it for cheap, low-stakes decisions —
> loan-risk qualifies because each decision is high-value and tool-grounded.

## 2:35–2:45 — Close (screen: title / live URL)

> WASPADA: a society of agents that argues about risk so a human analyst
> doesn't have to take a number on faith. Built on Alibaba Cloud — OSS,
> Function Compute, Qwen, and Simple Log Service for the full audit trail.
> Thanks for watching.

---

## Recording checklist

- [ ] Function Compute URL live and warm (hit `/` once before recording)
- [ ] Dashboard fixture loads with no console errors
- [ ] Agent Society panel populated (run `?brain=qwen` once before recording
      so the debate cards carry real Qwen transcripts, not just the fixture)
- [ ] Zoom browser to ~125% so table text reads on a recorded video
- [ ] Have the architecture diagram from HACKATHON.md ready to alt-tab into
- [ ] Benchmark table ready (WA-017 committed JSON) if it has landed
- [ ] Do one dry run with a stopwatch before the real take
