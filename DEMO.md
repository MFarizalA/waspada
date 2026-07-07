# WASPADA — demo video script (under 3:00)

Live app: https://waspada-351193906091.asia-southeast2.run.app

Total runtime target: **2:45**. Timestamps assume ~150 wpm; leave a few
seconds of slack, don't rush the dashboard beats.

---

## 0:00–0:15 — Hook (screen: title slide or blank)

> Multifinance lenders carry millions of installment loans. Risk analysts
> re-score the whole book overnight, in batch — by the time the work-list
> lands, it's already stale. WASPADA turns that into a same-day, GPU-accelerated
> decision, made by a team of AI agents, with a human holding the approval gate.

## 0:15–0:45 — What it is (screen: architecture diagram, slide 8 or README)

> WASPADA is an autonomous multi-agent pipeline. An Orchestrator plans the run
> and hands off to four specialist agents: Ingest pulls the portfolio from
> Google BigQuery, Analytics builds features on GPU with NVIDIA cuDF, the Risk
> Model scores default probability, and Insight ranks the work-list and raises
> alerts. Every recommendation stops at a human approval gate before it reaches
> a collector.
>
> And here's the twist: WASPADA itself was built by a small AI company — a
> boss agent and three worker agents — that planned, coded, tested, and shipped
> this pipeline. Agents building agents, humans in control.

## 0:45–1:30 — Live dashboard (screen: the live Cloud Run URL)

> This is the analyst's early-warning view, running live right now.
>
> [Point at Work-list] The top account — loan LN00573558, debt consolidation,
> Bali — scores 0.98 probability of default. Band Q5, the riskiest tier, action:
> call. Every one of these twenty ranked accounts comes with the same three
> things: a score, a band, and a next action — nothing for the analyst to
> figure out by hand.
>
> [Point at Portfolio Health] NPL ratio is 21.9 percent right now. Vintage
> default rate's been flat around 14 percent since 2021 — so this isn't a new
> problem, it's a standing one nobody's had a same-day view into until now.
>
> [Point at Alerts] And that 21.9 sits above the 20 percent threshold — so
> WASPADA's already raised one active breach, moderate severity, portfolio-wide,
> automatically. No one had to go looking for it.

## 1:30–2:00 — The GPU proof, honestly (screen: benchmark table / slide 9)

> We measured this honestly instead of cherry-picking a number. On the real
> pipeline — feature engineering at a million rows — cuDF on GPU is 1.2 times
> faster than CPU. That's a modest number, and that's the point: the hackathon
> brief itself warns that light operations show weak GPU speedup. On heavier
> portfolio analytics — groupby and full-book sorts — the same GPU is nearly
> four times faster. Weak on light ops, real on heavy ops, exactly as expected.
> The risk model is CPU today; GPU acceleration there is a wired drop-in, not a
> rewrite.

## 2:00–2:30 — Why it matters (screen: dashboard or architecture again)

> This isn't a black box. Every agent's step is logged and auditable, and
> nothing reaches a collector without a human sign-off. One engine, two
> lifecycle decisions — collections today, loan origination on the same core
> next. Same BigQuery source, same GPU path, no rewrite to extend it.

## 2:30–2:45 — Close (screen: title / live URL)

> WASPADA: an AI company building an AI system, so a human analyst spends less
> time hunting for risk and more time acting on it. Thanks for watching.

---

## Recording checklist

- [ ] Cloud Run service warm (hit `/` once before recording — cold start adds
      a few seconds)
- [ ] Dashboard fixture loads with no console errors (`/fixtures/sample-payload.json`
      → 200)
- [ ] Zoom browser to ~125% so table text reads on a recorded video
- [ ] Have slide 8 (architecture) and slide 9 (benchmark) ready to alt-tab into
- [ ] Do one dry run with a stopwatch before the real take
