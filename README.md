# WASPADA

**W**arning **&** **A**pproval **S**ystem for **P**ortfolio **A**nd **D**efault
**A**nalytics — an autonomous **multi-agent, GPU-accelerated risk decision-support
system** for a multifinance lender's data analyst.

Spans two lanes of the loan lifecycle on one shared risk engine:
- **Origination** — approve / reject / price new applications.
- **Collections / Early-Warning** — which existing accounts will roll into NPL,
  and how to prioritize limited collector capacity.

**Stack:** BigQuery (data) · cuDF + cuML / RAPIDS (GPU pipeline) · a coordinating
agent over specialized agents (ingest → analytics → risk-model → insight) with a
human on the sign-off gate.

See **[HACKATHON.md](HACKATHON.md)** for the full brief, architecture, and build plan.

> Built by an autonomous AI software company (Stefanie · Bimo · Kirana · Reza) —
> agents building agents, humans in control.
