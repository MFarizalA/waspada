#!/usr/bin/env bash
#
# wt.sh — per-agent git worktree helper for WASPADA.
#
# Why: multiple coding agents sharing ONE working directory collide (uncommitted
# work from one gets stashed/reverted under another). A git worktree gives each
# agent its own isolated checkout on its own branch, backed by the same repo —
# so two agents can build two tickets at once with zero interference.
#
# Worktrees live OUTSIDE the repo, in a sibling dir  ../waspada-wt/<slug>  , so
# they are never tracked and never nest inside the main checkout.
#
# Usage:
#   bash scripts/wt.sh new <ticket-slug> [base-branch]   # create branch + worktree (base defaults to main)
#   bash scripts/wt.sh list                              # show all worktrees
#   bash scripts/wt.sh rm  <ticket-slug>                 # remove a worktree (branch is kept)
#   bash scripts/wt.sh prune                             # clean up stale worktree metadata
#
# Example — dispatch two agents in parallel:
#   bash scripts/wt.sh new wa-031-el-heatmap
#   bash scripts/wt.sh new wa-023-sls-audit
#   # -> point agent A at ../waspada-wt/wa-031-el-heatmap
#   # -> point agent B at ../waspada-wt/wa-023-sls-audit
#
# After the branch is merged:
#   bash scripts/wt.sh rm wa-031-el-heatmap
#
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
WT_DIR="$(dirname "$ROOT")/waspada-wt"

cmd="${1:-help}"
case "$cmd" in
  new)
    slug="${2:?usage: wt.sh new <ticket-slug> [base-branch]}"
    base="${3:-main}"
    dest="$WT_DIR/$slug"
    if [ -e "$dest" ]; then
      echo "error: $dest already exists — pick another slug or 'wt.sh rm $slug' first." >&2
      exit 1
    fi
    # No network: branch off the LOCAL base (origin fetch/push is a separate,
    # explicit step so this helper never blocks on auth).
    git -C "$ROOT" worktree add -b "$slug" "$dest" "$base"
    echo ""
    echo "  worktree ready → $dest   (branch '$slug' off '$base')"
    echo "  point one agent at that directory; it works there in isolation."
    ;;
  list)
    git -C "$ROOT" worktree list
    ;;
  rm)
    slug="${2:?usage: wt.sh rm <ticket-slug>}"
    git -C "$ROOT" worktree remove "$WT_DIR/$slug"
    echo "removed worktree '$slug' (branch '$slug' kept — delete with: git branch -d $slug)"
    ;;
  prune)
    git -C "$ROOT" worktree prune -v
    ;;
  *)
    sed -n '3,30p' "$0"
    ;;
esac
