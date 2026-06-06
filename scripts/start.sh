#!/usr/bin/env bash
# Claim a planned item for building: move it from planning/confirmed/ to
# planning/in_progress/. Called automatically by /build. Idempotent — a file
# already in in_progress/ is a no-op "resume". Enforces WIP=1, refuses an
# unplanned item, and is portable (falls back to plain mv for untracked files).
# Usage: scripts/start.sh <item-file>   (run from repo root)
set -uo pipefail
src="${1:?usage: scripts/start.sh <item-file>}"
[ -d planning ] || { echo "error: run from the repo root (no ./planning here)" >&2; exit 1; }
[ -f "$src" ] || { echo "no such file: $src" >&2; exit 1; }

# Already in progress -> resume, no-op (lets /build re-enter a half-done item).
case "$src" in planning/in_progress/*) echo "resume -> $src"; exit 0;; esac

case "$src" in
  planning/confirmed/*) ;;
  *) echo "error: $src is not in planning/confirmed/ — scope it first" >&2; exit 2 ;;
esac

# Must be planned: no PLAN PENDING marker.
if grep -q 'PLAN PENDING' "$src"; then
  echo "error: $src is UNPLANNED (run /scope to finish the plan)" >&2; exit 3
fi

# WIP = 1: refuse if a different item is already in progress.
mkdir -p planning/in_progress
for f in planning/in_progress/*.md; do
  [ -e "$f" ] || continue
  echo "error: already in progress: $f — finish or close it before starting another" >&2
  exit 4
done

dest="planning/in_progress/$(basename "$src")"
# git mv only works on tracked files; fall back to plain mv otherwise.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
   && git ls-files --error-unmatch "$src" >/dev/null 2>&1; then
  git mv "$src" "$dest"
else
  mv "$src" "$dest"
fi
echo "started -> $dest"
