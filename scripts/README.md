# scripts/ — dev tooling

## `wt.sh` — per-agent git worktrees (multi-agent workflow)

**The problem it solves.** When two coding agents (or two of you) work in the
*same* directory on different tickets, their uncommitted changes collide: one
agent's `git stash` / branch-switch silently pulls the rug from under the other.
That is exactly how a finished ticket's files can vanish mid-session.

**The fix.** A [git worktree](https://git-scm.com/docs/git-worktree) is a second
working directory attached to the same repository, checked out on its own
branch. Each agent gets its own worktree, so concurrent work is fully isolated —
same history, separate files, no collisions.

### Layout

```
Developer/
├── waspada/                     ← main checkout (this repo, usually on `main`)
└── waspada-wt/                  ← worktrees live here (sibling, never tracked)
    ├── wa-031-el-heatmap/       ← agent A works here, branch wa-031-el-heatmap
    └── wa-023-sls-audit/        ← agent B works here, branch wa-023-sls-audit
```

### Commands

```bash
bash scripts/wt.sh new <ticket-slug> [base-branch]   # create branch + worktree (base = main)
bash scripts/wt.sh list                              # show every worktree + its branch
bash scripts/wt.sh rm  <ticket-slug>                 # remove the worktree (branch kept)
bash scripts/wt.sh prune                             # drop stale worktree metadata
```

### The workflow

1. **One agent per worktree.** Spin one up per ticket and point the agent at
   that directory: `bash scripts/wt.sh new wa-031-el-heatmap`.
2. Agents commit on their own branch inside their own worktree — they never see
   or disturb each other's files.
3. **Integrate in the main checkout.** Merge branches into `main` here (in
   `waspada/`), then run the full test suite once on the merged result — that is
   where cross-branch integration bugs surface (e.g. a new pipeline agent that a
   sibling branch's test didn't isolate).
4. **Clean up after merge:** `bash scripts/wt.sh rm wa-031-el-heatmap`, then
   `git branch -d wa-031-el-heatmap`.

### Rules of thumb

- **Never run two agents in the same working directory.** That is the whole
  point.
- Worktrees share one `.git`, so `git worktree remove` frees the disk; the
  branch and its commits stay in the repo.
- Push/fetch to `origin` is a **separate, explicit** step — `wt.sh` never
  touches the network, so it can't hang on auth.
- A branch can only be checked out in one worktree at a time (git enforces
  this — a second checkout of the same branch errors, which is a feature).
