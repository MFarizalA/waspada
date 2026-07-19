---
id: uiux-research
state: research
owner: Bimo (product) · researched by Claude 2026-07-19
feeds: WA-091 (flow view), WA-095 (param matrix), demo video; companion to branding-research.md
---

# WASPADA · UI/UX research + audit

## 1. Method
Two research buckets (risk-analyst dashboard UX; agentic-AI transparency UX), then an audit of our
actual screens (`App`, `WorkList`, `PortfolioHealth`, `Alerts`, `AgentDialogue`+`DebateFlow`,
`AccountDrawer`, `AuthScreen`) against them. Recommendations are mapped to existing tickets where
one exists.

## 2. External findings that matter for us

### Risk-analyst dashboards (SOC/compliance patterns)
- **The four-stage loop:** analyst tools converge on *Triage → Investigation → Response →
  Reporting*. The UI should make the current stage obvious and the next action one click away.
- **Reason-code surfacing:** when the analyst must make a *defensible* call, the alert **and the
  reasons it fired must live in the same view** — matched fields, trigger, confidence — without
  navigating away.
- **Glanceable zone + progressive disclosure:** compress KPIs/context/next-action up top; drill on
  demand. **Red only if something must be fixed now** (semantic color discipline).
- **Workspace, not report:** analysts triangulate non-linearly; support exploration (filter,
  compare, pivot), not just pre-packaged views.

### Agentic-AI transparency (2026 patterns)
- Four principles: **reasoning transparency at decision points · user override at any stage ·
  proactive status during processing · structured error recovery.**
- **Progressive disclosure of reasoning beats both black-box and full-transparency** — a "Why?"
  affordance with expandable logic + audit trail, not a wall of chain-of-thought.
- **Categorical confidence signaling:** green = confident, amber = review-worthy, red = the agent
  itself is uncertain. Numeric confidence alone doesn't land.
- Distrust comes from **opacity, unpredictability, and poor error communication** — trust is a
  designed output.

## 3. Audit — where WASPADA already matches the research
| Pattern | Our implementation | Verdict |
|---|---|---|
| Triage queue, priority-first | `WorkList` sorted by p_default, capped top-N, action per row | ✅ strong |
| Semantic color discipline | risk ramp + severity tokens separate from brand; "call" stays red | ✅ (post-re-theme) |
| Progressive disclosure | row → `AccountDrawer`; debate → per-dispute cards; anchor strip "N contested" | ✅ |
| Reasoning transparency | full round-by-round transcript with cited evidence + model names | ✅ rare & strong — most products show nothing |
| Proactive status | run spinner + "debating" placeholder; SSE rounds stream in live | ✅ |
| Decision visualization | WA-091 flow view (society spine + debate branch) | ✅ new |
| Structured error recovery | run/stream error alerts with message; 503 gate | ✅ basic |

**Honest framing: our transparency story is *above* industry pattern** — a cited, multi-agent
debate transcript is exactly what the research says builds trust, and almost nobody ships it.

## 4. Gaps (prioritized)

### G1 · Reason-codes in the work-list row  — highest value
The row shows band + action but **not why**. The model's own drivers (WA-050 `explain()`) exist but
only surface inside the debate. Add a compact "top driver" chip per row (e.g. `dti 31.2 ↑`), so the
defensible-call rule (reason next to alert, no navigation) is met at the triage surface itself.

### G2 · Categorical confidence, not just numbers
Rounds/resolutions carry numeric confidence. Map to the research's 3-tier signal (≥0.75 solid,
0.5–0.75 hollow/amber, <0.5 flagged) on debate cards + flow nodes. Cheap: a dot style per tier.

### G3 · The human gate has no UI  — the "override at any stage" principle
Escalated disputes end as a badge; the `ApprovalGate` decision happens outside the dashboard. An
**escalation inbox** (list of `escalated_*` disputes with approve/reject + rationale field) closes
the loop and is a demo-day differentiator ("the society defers to the human — here"). Pairs with
WA-095 (policy before the run; inbox during/after).

### G4 · Reporting stage is invisible
Runs log to SLS + step log, but the UI has no "what happened this run" surface (run id, model tiers
used, K, disputes opened/resolved, memory short-circuits). A small collapsible **run summary strip**
under the debate panel covers the audit story the judges score. (Ties WA-093's monitoring record.)

### G5 · Exploration is thin
`WorkList` has no filter/sort controls (band, action, segment) — the "workspace not report" gap.
Post-hackathon unless trivial to add.

### G6 · Small polish
- `AccountDrawer`: cite which debate (if any) touched this account — link row ↔ dispute both ways
  (anchor exists one way).
- Empty/edge states: "0 disputes" currently just text — say *why that's good news*.
- Focus order after "Watch live" starts (screen-reader users lose place) — verify.

## 5. Recommendations mapped
| Rec | Effort | Ticket |
|---|---|---|
| G1 driver chip in work-list | S | new — WA-096 candidate |
| G2 confidence tiers in debate/flow | S | fold into WA-091 follow-up |
| G3 escalation inbox (human gate UI) | M | new — WA-097 candidate; pairs WA-095 |
| G4 run summary strip | S–M | pairs WA-093 |
| G5 work-list filters | M | post-hackathon |
| G6 polish items | S | as-you-go |

**Demo-day priority: G1 + G2 (small, visible), then G3 if time — it's the strongest live moment
after the debate itself.**

## Sources
- Dashboard/triage: pencilandpaper.io (dashboard UX patterns), aufaitux.com (cybersecurity
  dashboards), digiwagon.com (compliance analyst workflows), muz.li risk-intelligence dashboard
  guide, lazarev.agency, uxpilot.ai.
- Agentic transparency: smashingmagazine.com (Feb 2026, agentic control/consent/accountability),
  agentic-design.ai (trust & transparency patterns), opennash.com (agent UX trust), fuselabcreative
  (agent UI 2026), xcapit.com ("nobody wants to see the prompt"), screamingbox.net.
