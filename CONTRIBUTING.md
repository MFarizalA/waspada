# WASPADA Git Flow Policy

> **Enforced by:** Stefanie (EM) · **Applies to:** all workers + owner
> **Principle:** `main` is always green and always deployable. `develop` is the integration branch. Everything else is a feature/hotfix branch.

---

## Branching model

```
main   ─────────────────────────●─────────────────────────▶ (deployable)
              \                /          \
               \              /            \
develop ────────●────●────●───●─────●───────●──────────────▶ (integration)
                /         \         /         \
feature  ─────●/           ●───────/           ●───────────▶ (ticket work)
```

| Branch | Purpose | Merges into | Who merges |
|---|---|---|---|
| `main` | Production — always deployable | — | Stefanie or Rizal only |
| `develop` | Integration — all features land here first | `main` | Stefanie or Rizal only |
| `feature/*` | Ticket work | `develop` | Stefanie or Rizal |
| `fix/*` | Hotfixes | `main` (emergency) or `develop` (normal) | Stefanie or Rizal |

## Branch naming

```
feature/wa-<ticket>-<short-description>
```

| Example | Ticket |
|---|---|
| `feature/wa-056-live-analyst-mcp` | WA-056 |
| `feature/wa-057-medallion-buckets` | WA-057 |
| `feature/ci-pipeline` | Cross-cutting (no ticket) |
| `fix/wa-044-css-token-bug` | WA-044 bugfix |
| `hotfix/deploy-timeout` | Emergency fix → merges straight to main |

### Rules

- **Feature branches branch off `develop`** — `git checkout develop && git pull && git checkout -b feature/wa-XXX-desc`.
- **`develop` branches off `main`** — kept in sync, merged to `main` on release.
- **One branch per ticket.** Don't bundle two tickets on one branch.
- **Rebase before merge** if the target branch has moved.

## Committing

### Commit message format

```
<type>(<ticket>): <one-line description>

<optional body — what changed, why>
```

| Type | When |
|---|---|
| `feat` | New feature or capability |
| `fix` | Bug fix |
| `refactor` | Code restructuring, no behavior change |
| `test` | Test-only changes |
| `docs` | Documentation only |
| `chore` | Tooling, deps, config |
| `merge` | Merge commit (Stefanie or Rizal create these) |

Examples:
```
feat(WA-041): native tools/tool_calls function calling for Risk Auditor
fix(WA-044): FC timeout 60s → 180s (live Qwen debate takes ~70s)
refactor: remove GeminiLLM — dead surface area
```

### Rules

- **Commit early, commit often** on your branch.
- **Never commit to `main` or `develop` directly** — always branch first.
- **Never force-push to `main` or `develop`** — ever.
- **`.env` is never committed** — `.gitignore` blocks it.

## Merging

### Feature → develop (Stefanie or Rizal's gate)

Before merging any feature branch to `develop`:

1. ✅ **Tests green** — full suite passes (`pytest -q`)
2. ✅ **No conflict markers** — rebase if needed
3. ✅ **Acceptance criteria met** — every item in the ticket verified
4. ✅ **No secrets** — no `.env`, no API keys in the diff
5. ✅ **Commit messages follow the format**
6. ✅ **Owner signs off** — for P0/P1 tickets, Jal reviews

### develop → main (release gate)

`main` only updates when we're ready to deploy or tag a submission. Stefanie or Rizal merges `develop` → `main` on Jal's go-ahead.

```bash
git checkout main
git merge develop --no-ff -m "release: merge develop → main (<date>)"
git push origin main
```

## Branch cleanup

After merge to `develop`:

```bash
git branch -d feature/wa-XXX-desc           # local
git push origin --delete feature/wa-XXX-desc  # remote
```

## Worker subagent rules (critical)

| Rule | Why |
|---|---|
| **Create your own branch off `develop`** | Prevents accidental main/develop pollution |
| **Never run `git reset --hard` on any branch you didn't create** | Destroyed Reza's WA-040 work — lesson learned |
| **Never run `git clean -fd`** on any branch | Wiped untracked backlog tickets and the runbook |
| **Never `git checkout` to another worker's branch mid-work** | Causes working-tree conflicts |
| **Never `git push` to `main` or `develop`** — only Stefanie or Rizal does that | Centralizes the review gate |
| **Run tests before committing** — `.venv/Scripts/python.exe -m pytest -q` | Catches regressions before they hit the branch |
| **Report your branch name + commit hash** in your summary | So Stefanie or Rizal can verify and merge without guessing |

## The owner (Jal)

| Rule | Why |
|---|---|
| Jal pushes directly to `develop` for doc/ticket edits | He owns the repo, knows what he's doing |
| Jal coordinates cred uploads with Stefanie | She handles the `.env` / `secrets.tfvars` placement |
| Jal is the final sign-off for develop → main releases | The gate is sacred — nothing ships without the owner |

## Hotfix flow (emergency)

```
main ──────────────────────────────────── ▶
   \                                         /
    ── hotfix/<desc> ── merge to main ──────/
         \
          └── merge back to develop ────────── develop
```

1. Branch off `main`: `git checkout -b hotfix/description`
2. Fix, test, commit
3. Stefanie or Rizal merges to `main` immediately
4. Stefanie or Rizal merges `main` back to `develop` to keep them in sync

## Release / deploy tags

```bash
git tag -a v0.1.0 -m "Hackathon submission — 2026-07-20"
git push origin v0.1.0
```

Tag the submission commit on `main` so there's a permanent marker for the Devpost entry.

---

*Policy owner: Stefanie (EM) · Last updated: 2026-07-15*
