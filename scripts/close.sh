#!/usr/bin/env bash
# Move a work item to a terminal/parking folder.
#   done     (default) — completed: stamp the Resolution block (closed=today,
#                        branch + files changed from git) and move to planning/done/.
#   future             — park an idea: move to planning/future/ (no stamping).
#   rejected           — decline it: move to planning/rejected/ (no stamping;
#                        write the rationale in the file itself).
# Usage: scripts/close.sh <item-file> [done|future|rejected] [base-ref]   (from repo root)
# base-ref (default origin/main) is only used by `done`, to list changed files.
set -uo pipefail
src="${1:?usage: scripts/close.sh <item-file> [done|future|rejected] [base-ref]}"
[ -d planning ] || { echo "error: run from the repo root (no ./planning here)" >&2; exit 1; }
[ -f "$src" ] || { echo "no such file: $src" >&2; exit 1; }
stage="${2:-done}"
base="${3:-origin/main}"

case "$stage" in
  done|future|rejected) ;;
  *) echo "invalid destination: $stage (want done|future|rejected)" >&2; exit 2 ;;
esac

# Only `done` stamps a Resolution block — future/rejected are plain moves.
if [ "$stage" = done ]; then
  case "$src" in planning/confirmed/*) ;; *) echo "warning: $src is not in planning/confirmed/" >&2 ;; esac
  today="$(date +%F)"
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"
  changed="$(git diff --name-only "$base"...HEAD 2>/dev/null | paste -sd, - 2>/dev/null || true)"

  # Replace "- <key>: ..." in the Resolution block. \Q…\E quotes the key (so a key
  # with spaces like "files changed" is literal); key/value pass via env so shell
  # metacharacters in paths/branches stay literal.
  set_kv() {
    K="$1" V="$2" perl -i -pe 'BEGIN{($k,$v)=@ENV{qw/K V/}} s/^- \Q$k\E:.*/- $k: $v/' "$src"
  }
  grep -q '^- closed:' "$src" || echo "warning: no Resolution block in $src (nothing stamped)" >&2
  set_kv "closed" "$today"
  [ -z "$branch" ]  || set_kv "branch" "$branch"
  [ -z "$changed" ] || set_kv "files changed" "$changed"
fi

dest="planning/$stage/$(basename "$src")"
mkdir -p "planning/$stage"
# git mv only works on tracked files; fall back to plain mv otherwise.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
   && git ls-files --error-unmatch "$src" >/dev/null 2>&1; then
  git mv "$src" "$dest"
else
  mv "$src" "$dest"
fi
case "$stage" in done) verb=closed;; future) verb=parked;; rejected) verb=rejected;; esac
echo "$verb -> $dest"
