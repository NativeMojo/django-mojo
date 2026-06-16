#!/usr/bin/env bash
# Allocate the next ITEM id, stamp it into the item's frontmatter, move the file
# from planning/inbox/ to planning/confirmed/, and bump the counter — atomically.
# Usage: scripts/intake.sh planning/inbox/<file>.md   (run from repo root)
set -euo pipefail

src="${1:?usage: scripts/intake.sh <inbox-file>}"
[ -d planning ] || { echo "error: run from the repo root (no ./planning here)" >&2; exit 1; }
[ -f "$src" ] || { echo "no such file: $src" >&2; exit 1; }
case "$src" in planning/inbox/*) ;; *) echo "warning: $src is not in planning/inbox/" >&2 ;; esac
counter="planning/.next_id"

# Refuse if the item already has a non-empty id (don't consume a number).
# Strip any trailing `# ...` comment from the value first, so the template's
# commented-but-blank `id:` line (e.g. `id:   # leave blank`) reads as empty
# rather than being mistaken for an assigned id.
have="$(awk -F': *' '/^---/{f++;next} f==1&&$1=="id"{v=$2; sub(/[[:space:]]*#.*/,"",v); gsub(/[[:space:]]/,"",v); print v; exit}' "$src")"
[ -z "${have:-}" ] || { echo "already has id ($have); not consuming a number" >&2; exit 2; }

# N = max(counter, highest ASSIGNED id + 1). Reconciling against the actual tree
# means a stale or merged-back counter can never hand out a duplicate id.
# Only count real assignments — `id:` frontmatter lines and `ITEM-###-` filenames
# — NOT every ITEM-### mention, so example ids in _template.md / depends_on / prose
# don't advance the counter. `10#` forces base-10 (a zero-padded id isn't octal).
# Trailing `|| true`: with `set -euo pipefail`, an empty match makes grep exit 1
# and would abort the script — so a repo with no assigned ids yet (every fresh
# project's first intake) must not fail here; empty hi → N falls back to 1.
hi="$( { grep -rhoE '^id:[[:space:]]*ITEM-[0-9]+' planning 2>/dev/null
         find planning -type f -name 'ITEM-*-*.md' 2>/dev/null; } \
       | grep -oE 'ITEM-[0-9]+' | grep -oE '[0-9]+' | sort -n | tail -1 || true)"
ctr=0; [ -f "$counter" ] && ctr="$(tr -dc 0-9 < "$counter")"
floor=$(( 10#${hi:-0} + 1 ))
cur=$(( 10#${ctr:-0} ))
N=$(( cur > floor ? cur : floor ))
id="$(printf 'ITEM-%03d' "$N")"

# Slug from title:, else filename; never empty.
title="$(awk -F'title:' '/^---/{f++;next} f==1&&/^title:/{print $2;exit}' "$src")"
[ -n "${title// /}" ] || title="$(basename "$src" .md)"
slug="$(printf '%s' "$title" | tr '[:upper:]' '[:lower:]' \
        | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g' | cut -c1-50)"
[ -n "$slug" ] || slug="item"

# Ensure the first frontmatter block carries `id: <id>` (fill an empty id:, else
# insert one, else synthesize a block). awk → temp file is portable; the END
# guard aborts (exit 3) on malformed frontmatter so we never move an id-less
# item or consume a number.
tmp="$(mktemp)"
if awk -v id="$id" '
  BEGIN { seen_open=0; infm=0; done=0 }
  NR==1 && $0 ~ /^---[[:space:]]*$/ { print; seen_open=1; infm=1; next }
  seen_open==0 {
    if (!done) { print "---"; print "id: " id; print "---"; print ""; done=1 }
    print; next
  }
  infm==1 && $0 ~ /^id:[[:space:]]*/ { print "id: " id; done=1; next }
  infm==1 && $0 ~ /^---[[:space:]]*$/ {
    if (!done) { print "id: " id; done=1 }
    print; infm=0; next
  }
  { print }
  END { exit(done ? 0 : 3) }
' "$src" > "$tmp"; then
  mv "$tmp" "$src"
else
  rm -f "$tmp"; echo "could not stamp id into frontmatter: $src" >&2; exit 3
fi

dest="planning/confirmed/${id}-${slug}.md"
mkdir -p planning/confirmed
# git mv only works on tracked files; a freshly-dropped inbox item may be
# untracked, so fall back to plain mv (git picks up the rename/add either way).
if git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
   && git ls-files --error-unmatch "$src" >/dev/null 2>&1; then
  git mv "$src" "$dest"
else
  mv "$src" "$dest"
fi
echo $(( N + 1 )) > "$counter"
echo "$id $dest"
