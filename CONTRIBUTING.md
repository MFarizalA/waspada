# WASPADA Git Flow Policy

> **Enforced by:** Stefanie (EM) · **Applies to:** all workers + owner
> **Principle:** `main` is always green and always deployable. Everything else is a branch.

---

## 1. Branching

### Branch naming

```
wa-<ticket>-<short-description>
```

| Example | Ticket |
|---|---|
| `wa-041-native-tool-calls` | WA-041 |
| `wa-057-medallion-buckets` | WA-057 |
| `ci-pipeline` | Cross-cutting (no ticket) |
| `fix/css-token-bug` | Hotfix |

### Rules

- **One branch per ticket.** Don't bundle two tickets on one branch.
- **Branch off `main`** — always the latest. `git checkout main && git pull && git checkout -b wa-XXX-desc`.
- **Rebase before merge** if `main` has moved — keeps history linear and avoids merge commits piling up.

---

## 2. Committing

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
| `merge` | Merge commit (Steffi creates these) |

Examples:
```
feat(WA-041): native tools/tool_calls function calling for Risk Auditor
fix(WA-044): FC timeout 60s → 180s (live Qwen debate takes ~70s)
refactor: remove GeminiLLM — dead surface area
test: add live Qwen API smoke test
docs: update HACKATHON.md rubric status
```

### Rules

- **Commit early, commit often** on your branch — small commits are easier to review and revert.
- **Never commit to `main` directly** — always branch first. Only Stefanie merges to `main`.
- **Never force-push to `main`** — ever.
- **`.env` is never committed** — `.gitignore` blocks it, but check your `git add` before pushing.

---

## 3. Merging to `main`

### Who merges

**Only Stefanie (EM) merges to `main`.** Workers create branches, run tests, and report completion. Stefanie reviews, merges, and pushes.

### Merge checklist (Steffi's gate)

Before merging any branch to `main`:

1. ✅ **Tests green** — full suite passes (`pytest -q`), not just the ticket's tests
2. ✅ **No conflict markers** — rebase if needed
3. ✅ **Acceptance criteria met** — every item in the ticket's "Acceptance" section is verified
4. ✅ **No secrets** — no `.env`, no API keys, no connection strings in the diff
5. ✅ **Commit messages follow the format** — fix before merge if needed
6. ✅ **Owner signs off** — for P0/P1 tickets, Jal reviews before merge

### Merge style

```bash
git checkout main
git merge <branch> --no-ff -m "merge: <ticket> <short description>"
git push origin main
```

`--no-ff` preserves the branch history — you can see which tickets landed when.

---

## 4. Branch cleanup

After merge:

```bash
git branch -d <branch>           # local
git push origin --delete <branch>  # remote (if pushed)
```

Keep the branch list clean. Stale branches confuse everyone.

---

## 5. Worker subagent rules (critical)

These apply specifically to delegated subagents (Bimo, Reza, Kirana):

| Rule | Why |
|---|---|
| **Create your own branch** — never commit directly on `main` | Prevents accidental main pollution |
| **Never run `git reset --hard` on any branch you didn't create** | Destroyed Reza's WA-040 work this way — lesson learned |
| **Never run `git clean -fd` on `main`** | Wiped untracked backlog tickets and the runbook |
| **Never `git checkout` to another worker's branch mid-work** | Causes working-tree conflicts |
| **Never `git push` to `main`** — only Stefanie does that | Centralizes the review gate |
| **Run tests before committing** — `.venv/Scripts/python.exe -m pytest -q` | Catches regressions before they hit the branch |
| **Report your branch name + commit hash** in your summary | So Stefanie can verify and merge without guessing |

---

## 6. The owner (Jal)

| Rule | Why |
|---|---|
| Jal pushes directly to `main` for doc/ticket edits | He owns the repo, knows what he's doing |
| Jal coordinates cred uploads with Steffi | She handles the `.env` / `secrets.tfvars` placement |
| Jal is the final sign-off for P0/P1 merges | The gate is sacred — nothing ships without the owner |

---

## 7. Hotfix flow (emergency)

```
main ──────────────────────────────────── ▶
   \                                         /
    ── fix/hotfix-<desc> ── merge ──────────/
```

For a demo-breaking bug:

1. Branch off `main`: `git checkout -b fix/hotfix-description`
2. Fix, test, commit
3. Steffi merges immediately (skip the full review gate for hotfixes)
4. Push to `main`

---

## 8. Release / deploy tags

```bash
git tag -a v0.1.0 -m "Hackathon submission — 2026-07-20"
git push origin v0.1.0
```

Tag the submission commit so there's a permanent marker for the Devpost entry.

---

*Policy owner: Stefanie (EM) · Last updated: 2026-07-15*
