#!/usr/bin/env bash
# Report whether a confirmed item's depends_on are all satisfied (in planning/done/).
# Usage: scripts/ready.sh planning/confirmed/<file>.md   (run from repo root)
# Exit 0 = READY, 1 = BLOCKED, 2 = usage/error.
set -uo pipefail
src="${1:?usage: scripts/ready.sh <item-file>}"
[ -d planning ] || { echo "error: run from the repo root (no ./planning here)" >&2; exit 2; }
[ -f "$src" ] || { echo "no such file: $src" >&2; exit 2; }

# Per-project workflow config (id PREFIX etc.); fallback keeps
# config-less repos working unchanged.
[ -f planning/.config ] && . planning/.config
PREFIX="${PREFIX:-ITEM}"
# Guard against a typo'd config silently mis-numbering items.
case "$PREFIX" in
  [!A-Za-z]*|*[!A-Za-z0-9]*)
    echo "error: invalid PREFIX '$PREFIX' in planning/.config (want letters/digits, starting with a letter)" >&2; exit 2 ;;
esac

locate() {  # echo the stage folder holding id $1 (done first), else nothing
  local d
  for d in done in_progress confirmed inbox future rejected; do
    if grep -rlqE "^id:[[:space:]]*$1[[:space:]]*$" "planning/$d" 2>/dev/null; then
      echo "$d"; return
    fi
  done
}

# Canonicalize a local id's zero-padding (<PREFIX>-2 / <PREFIX>-02 -> <PREFIX>-002)
# so a dep resolves regardless of how many digits the author wrote. Only touch
# strictly numeric <PREFIX>-<digits>; leave anything else (e.g. cross-repo) verbatim.
norm() {
  local n="${1#"$PREFIX"-}"
  if [ "$1" != "$n" ] && [ -n "$n" ] && [ -z "${n//[0-9]/}" ]; then
    printf '%s-%03d' "$PREFIX" "$(( 10#$n ))"
  else
    printf '%s' "$1"
  fi
}

# Handle both inline (`depends_on: [ITEM-003, ITEM-007]`) and block style
# (`depends_on:` then `  - ITEM-003` lines), within the first frontmatter block.
deps="$(awk '
  /^---[[:space:]]*$/ { f++; next }
  f==1 && /^depends_on:/ { s=$0; sub(/^depends_on:[[:space:]]*/,"",s); print s; blk=1; next }
  f==1 && blk && /^[[:space:]]+-[[:space:]]*/ { s=$0; sub(/^[[:space:]]+-[[:space:]]*/,"",s); print s; next }
  f==1 && blk && /^[^[:space:]]/ { blk=0 }
' "$src" | tr -d '[]' | tr ',' '\n' \
        | sed -E "s/^[[:space:]]*//; s/[[:space:]]*$//; s/^[\"']//; s/[\"']$//" \
        | grep -v '^$' || true)"

ext=(); blockers=()
[ -z "$deps" ] || while IFS= read -r dep; do
  case "$dep" in
    *"#"*) ext+=("$dep") ;;                                  # cross-repo: can't check locally
    *) dep="$(norm "$dep")"
       where="$(locate "$dep")"
       [ "$where" = done ] || blockers+=("$dep (${where:-missing})") ;;
  esac
done <<< "$deps"

[ ${#ext[@]} -eq 0 ] || echo "note: external deps to verify manually: ${ext[*]}" >&2
if [ ${#blockers[@]} -eq 0 ]; then
  echo READY; exit 0
else
  printf 'BLOCKED by %s\n' "$(IFS=', '; echo "${blockers[*]}")"; exit 1
fi
