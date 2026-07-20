# Team & Collaboration

> WASPADA was built by a small collaborating team working in **two lanes** on one
> codebase. This page documents who owns what, how the lanes stay out of each
> other's way, and the conventions that keep a shared repo sane.

## 1. Two lanes, one contract

The work splits along a natural seam — **production depth** vs **demo/presentation**
— joined by the [frozen data contract](01-data-architecture.md) so neither lane
blocks the other.

| | **Production lane** (Bimo) | **Demo / experience lane** (Stefanie's team) |
|---|---|---|
| Focus | real data, agents, model, cloud, governance | the demo, presentation, frontend polish, storytelling |
| Owns | `waspada/` (agents, model, data), `api/`, `deploy/iac/` | dashboard experience, demo scenarios, submission narrative |
| Priority | *"focus on real production"* since week one | a compelling, judge-ready walkthrough |
| Brain | Qwen (DashScope) | Kimi K3 (their own model quota) |

The **frozen contract** (`RawLoans → FeatureFrame → ScoredAccounts →
DashboardPayload`) is the interface between the lanes: the backend guarantees the
payload shape, the frontend consumes it, and either side can move independently as
long as the shape holds (additive-only). This is *why* the contract is frozen — it's
a team boundary as much as a technical one.

## 2. Stefanie's team — role & scope

Stefanie's team drives the **demo and experience lane**: the parts a judge actually
sees and the story that frames them. In practice that has meant:

- Owning the **demo scenarios** and the submission narrative.
- Driving **frontend/UX** direction and the dashboard's demo-readiness.
- Coordinating the **presentation** (deck, walkthrough, video framing).
- Running on **Kimi K3** for their own LLM-assisted work (separate quota from the
  production lane's Qwen usage), with availability windows around quota resets.

Because both lanes touch the same repo, the collaboration relies on clear ownership,
ticketed handoffs, and the isolation workflow below — not on locking files.

## 3. Coordination mechanics

### Tickets (`WA-NNN`)
Work is tracked as tickets in [`backlog/`](../../backlog/). Each has an owner, a
state, and a self-contained design so either lane can pick it up without a meeting.
Cross-lane handoffs are explicit: *"escalate to Stefanie"*, *"Stefanie dispatched
WA-071…"*, etc. Read the ticket file first — don't re-derive the design.

### Isolated git worktrees
Early on, **two coding agents sharing one working directory** caused a real
incident (a `git checkout` on one branch wiped another's uncommitted edits). The
fix is `scripts/wt.sh` — **per-worker git worktrees**, so each stream of work has
its own isolated checkout of the same repo. Rule of thumb: if two people/agents are
editing at once, they're in **separate worktrees**.

### Branch & merge discipline
- Feature branches off `develop`; PRs into `develop`; releases `develop → main`.
- **Full test suite after every merge** — because two lanes merging independently
  can interact, "green on my branch" isn't enough; the combined `develop` must be
  green (it has been: 500+ tests).
- The default branch (`main`) is protected; merges to it are deliberate releases.

## 4. Known collaboration hazards (and the guardrails)

| Hazard | Guardrail |
|--------|-----------|
| Shared-checkout collisions | isolated worktrees (`scripts/wt.sh`) |
| One lane's WIP clobbered by the other's branch switch | commit early; work in your own worktree |
| Demo-vs-real data drift | the frozen contract + fixtures that mirror the real payload shape |
| Independent merges interacting | full-suite-after-merge rule |
| Secrets leaking into a shared repo | the WA-020 secrets sweep + `.env*`/`secrets/` ignored |

## 5. Working norms

- **Contract-first**: if the payload shape needs to change, raise it — don't change
  it unilaterally (it's the other lane's dependency).
- **Additive-only**: new columns/keys are free; renames/removals are breaking and
  need coordination.
- **Offline-first**: keep the demo runnable with no credentials (fixtures + mock
  brain), so a presenter is never at the mercy of a live API.
- **Leave the audit honest**: record what actually happened (auto-approvals flagged,
  skipped steps noted) — the audit trail is a shared source of truth.

---

*This page documents roles and process, not org structure. Where pronouns for a
teammate aren't established, this wiki uses they/them.*

**Related:** [System Architecture](02-system-architecture.md) ·
[Data Architecture](01-data-architecture.md) · [Tech Stack](05-techstack.md)
