---
# id is assigned by /scope on pickup — leave it blank
id: DM-041
type: chore
title: Config-driven item ID prefixes (DM-###) + canonical workflow spec update
priority: P2
effort: L              # XS | S | M | L | XL
owner: ian
opened: 2026-07-16
depends_on: []
related: []
links: []
---

# Config-driven item ID prefixes (DM-###) + canonical workflow spec update

## What & Why
The intake workflow names every work item `ITEM-###`, which carries no meaning
for the user and is ambiguous across projects — several repos share this
workflow, and "DM-027" could belong to any of them. Replace the generic
prefix with a short per-project prefix (`DM-041-my-request-subject.md` here),
keeping the numeric index and title slug unchanged.

**Key design decision (agreed with user 2026-07-16): the prefix comes from a
per-project config file, not from editing the scripts.** Add `planning/.config`
(shell-sourceable, e.g. `PREFIX=DM`) that all five workflow scripts source,
falling back to `ITEM` when absent (backwards compatible mid-rollout). The
script bodies then stay **byte-identical across every repo** — the only
per-repo difference is the config. Setup/migration creates the config with a
default; the user edits it to change the prefix.

**Canonical source of truth:** the workflow spec lives at
`/Users/ians/Projects/ai_project_setup.md` (verbatim script bodies all repos
copy) with `/Users/ians/Projects/migrate-agent-workflow.md` as the migration
playbook for existing repos. Both must be updated first so every future setup
and migration carries the config automatically. Note: the migration doc
currently says "never renumber existing items" — a prefix rename keeps the
number (`DM-038` → `DM-038`) so it's compatible in spirit, but the doc should
be amended to bless prefix renames explicitly.

**Rollout map (user wants ALL projects on the shared workflow):**

| Repo | State today | Action | Prefix |
|---|---|---|---|
| django-mojo | on workflow; 40 items, counter 41 | this item: config + scripts + rename | `DM` |
| web-mojo | on workflow; 13 items, counter 28 | same rollout, own planning item | `WM` |
| wmx_portal | on workflow; 40 items, counter 75; a few stray un-prefixed files | same rollout | `WP` (proposal) |
| wmx_api | on workflow; 12 ITEM files, counter 130; legacy `01-name.md` items in done/ | same rollout | `WA` (proposal) |
| hamp-backend, legacy-portal, reseaudent_api, android-mojo, wmx_test_client | older planning layout (requests/issues/mockups; no intake.sh) | full migration via `migrate-agent-workflow.md`, picking up config-driven naming in the same pass | TBD per repo |

This item owns: (1) the canonical spec/migration-doc updates, (2) the
django-mojo rollout. The other repos are executed in their own repos (each has
or gets its own planning pipeline); this item defines the pattern they follow.

Decisions already made with the user (2026-07-16):
- Per-project prefixes: `DM` here, `WM` for web-mojo (not `DW`).
- Keep 3-digit zero-padding (`DM-041`) for sort order.
- Rename **all existing items** in workflow repos (files + frontmatter `id:` +
  `depends_on:`/`related:` references) — no mixed board. Old IDs in git history
  stay stale; accepted.
- Counters stay separate per repo — the prefix makes IDs globally unique.
- Scripts must be identical everywhere; per-repo variance lives only in
  `planning/.config`.

## Acceptance Criteria
- [ ] `planning/.config` exists (e.g. `PREFIX=DM`), documented in the template
      or a comment header; scripts source it and fall back to `PREFIX=ITEM`
      when missing.
- [ ] All five scripts (`intake.sh`, `start.sh`, `board.sh`, `ready.sh`,
      `close.sh`) are prefix-agnostic: id allocation (`printf`), counter
      reconciliation greps (`^id:` frontmatter + `<PREFIX>-*-*.md` filenames),
      and `ready.sh`'s `norm()` all use `$PREFIX`. No hardcoded `ITEM-` left.
- [ ] Canonical spec `/Users/ians/Projects/ai_project_setup.md` updated: script
      bodies replaced with the config-driven versions, `planning/.config` added
      to the folder structure/bootstrap, prose examples use the prefix
      placeholder.
- [ ] Workflow-prose audit (before rollout, since we're rewriting the spec
      anyway): review the spec's and each repo's skill instructions for
      session-flow guidance and other drifted/wrong prose. Known instance:
      django-mojo's `/request` skill appends "(a fresh session is ideal)" to
      the /scope hand-off — the canonical spec has no such language, and it's
      wrong as a default. Correct rule: same-session continuation ("scope it")
      is the normal flow; the item FILE being self-contained is the invariant;
      recommend a fresh session only when the current one is already long.
      Standardize the hand-off wording in the spec and remove local additions.
- [ ] `/Users/ians/Projects/migrate-agent-workflow.md` updated: migration
      creates `planning/.config`, and the "never renumber" rule is amended to
      explicitly allow prefix renames that keep the number.
- [ ] django-mojo: all existing items in every stage folder (inbox, confirmed,
      in_progress, done, future, rejected) renamed `ITEM-NNN-*` → `DM-NNN-*`
      via `git mv`, frontmatter `id:` and cross-references updated; numeric
      parts unchanged; `planning/.next_id` untouched (bare integer).
- [ ] django-mojo docs sweep: `CLAUDE.md`, `planning/_template.md`, `AI_DEV.md`,
      `memory.md`, `.claude/skills/*`, `.claude/rules/*` references updated.
- [ ] Smoke test per the migration doc: `bash -n scripts/*.sh`; throwaway item
      through intake → ready → close; first real intake allocates the next id
      (41), not a skipped one; `board.sh` and `ready.sh` run clean (no BLOCKED
      caused by un-renamed references).
- [ ] Rollout items filed for web-mojo (`WM`), wmx_portal, wmx_api; migration
      of the five older repos noted as follow-up work using the updated
      migration doc.

## Repro — bugs only
n/a

## Plan

_Approved by user 2026-07-16 (with: rewrite memory.md refs; config named
`planning/.config`; whole-file id rewrite in item files)._

### Goal
Make the item-id prefix config-driven (`planning/.config`, `PREFIX=DM` here,
fallback `ITEM`) in both the canonical workflow spec and django-mojo, and
rename all existing items to the new prefix — numbers unchanged.

### Context — what exists
- **Canonical spec**: `/Users/ians/Projects/ai_project_setup.md` embeds the
  verbatim bodies of all five scripts (intake.sh ~L365-443, start.sh, board.sh,
  ready.sh ~L569-627, close.sh) plus the template and the request/scope/build
  skill texts. `/Users/ians/Projects/migrate-agent-workflow.md` is the playbook
  for migrating an existing repo onto the spec; §4 ("What to keep") currently
  says "Never renumber existing ids"; §5 has a smoke-test snippet using
  hardcoded `ITEM-` patterns.
- **django-mojo scripts** (recon 2026-07-16, line numbers verified):
  - `scripts/intake.sh` (80 lines) — the only script that mints ids.
    Load-bearing prefix lines: L28 `grep -rhoE '^id:[[:space:]]*ITEM-[0-9]+' planning`,
    L29 `find planning -type f -name 'ITEM-*-*.md'`,
    L30 `grep -oE 'ITEM-[0-9]+' | grep -oE '[0-9]+'`,
    L35 `id="$(printf 'ITEM-%03d' "$N")"`. Comments mentioning ITEM: L2, L22,
    L23. L69 builds the dest filename from `$id`, so it follows automatically.
  - `scripts/ready.sh` (57 lines) — `norm()` (L22-29) canonicalizes
    zero-padding: L23 `local n="${1#ITEM-}"`, L25 `printf 'ITEM-%03d' ...`.
    Comments: L19, L21, L31, L32. `locate()` is prefix-agnostic.
  - `scripts/start.sh`, `scripts/board.sh`, `scripts/close.sh` — **zero**
    prefix references; no changes needed.
  - No script sources any config today.
- **Items to rename**: 41 files — 4 in `planning/confirmed/` (DM-038..041),
  37 in `planning/done/` (inbox items are un-ID'd; in_progress/, future/,
  rejected/ are empty). NOTE: this item itself (DM-041) will be in
  `planning/in_progress/` while being built — include that folder in the
  rename globs.
- **Cross-references**: all inline `related:` lists (every `depends_on:` in the
  repo is `[]`, so nothing can go BLOCKED). ~20 item files carry local
  `ITEM-###` refs in `related:` and/or prose. One cross-repo ref must be
  preserved verbatim: `mverify_portal#ITEM-014` in `planning/done/DM-022-*:11`.
  Three inbox files reference ids: `phone-verify-dev-bypass-code-db-settable.md:10`,
  `member-perms-ignore-group-is-active.md:11`,
  `user-is-superuser-unguarded-on-non-user-identity.md:11`.
- **Docs with `ITEM-` references**: `CLAUDE.md:39`, `AI_DEV.md:37,78`,
  `planning/_template.md:10-11`, `.claude/skills/scope/SKILL.md:23,44,51,52`,
  `memory.md` (22 lines of historical prose refs — user approved rewriting).
  `.claude/skills/request/SKILL.md:61` carries the drifted "(a fresh session is
  ideal)" hand-off (not in the canonical spec — local addition to remove).
- **Counter**: `planning/.next_id` = `42`. Highest assigned id = DM-041.
  `planning/.config` does not exist yet.

### Changes — what to do

**Phase 1 — canonical spec (files live OUTSIDE this repo, in ~/Projects/)**
1. `/Users/ians/Projects/ai_project_setup.md`:
   - Folder-structure section: add `planning/.config` with content
     ```
     # Workflow config — sourced by scripts/*.sh (shell syntax).
     # PREFIX: the project id prefix for work items (e.g. DM -> DM-041-slug.md).
     PREFIX=ITEM
     ```
     Bootstrap sets a real per-project value (short, uppercase, unique across
     the user's repos).
   - Embedded `intake.sh` body: after the `counter=` line, insert
     ```bash
     # Per-project workflow config (id PREFIX etc.); fallback keeps
     # config-less repos working unchanged.
     [ -f planning/.config ] && . planning/.config
     PREFIX="${PREFIX:-ITEM}"
     ```
     and parameterize: reconcile greps →
     `grep -rhoE "^id:[[:space:]]*${PREFIX}-[0-9]+" planning`,
     `find planning -type f -name "${PREFIX}-*-*.md"`,
     `grep -oE "${PREFIX}-[0-9]+"`; mint → `id="$(printf '%s-%03d' "$PREFIX" "$N")"`.
     Update the adjacent comments.
   - Embedded `ready.sh` body: same two config lines after the usage check;
     `norm()` → `local n="${1#"$PREFIX"-}"` and
     `printf '%s-%03d' "$PREFIX" "$(( 10#$n ))"`. Update comments.
   - Prose: "One ID space" bullets say ids are `<PREFIX>-###` (default `ITEM`,
     set in `planning/.config`); keep `DM-001`-style examples but note the
     prefix is per-repo.
   - Request-skill section, hand-off step: add "Same-session continuation is
     the normal flow — the item file carries everything `/scope` needs; suggest
     a fresh session only if the current one is already long."
2. `/Users/ians/Projects/migrate-agent-workflow.md`:
   - Step 2.2 (One ID space): also create `planning/.config` with the chosen
     `PREFIX`; new items get `<PREFIX>-###`.
   - §4 "What to keep": amend "never renumber" — prefix-only renames that keep
     the number (e.g. an `ITEM`-item becoming a `DM`-item, same number) are
     allowed when adopting config-driven prefixes, provided frontmatter `id:`
     and local references are updated in the same pass.
   - §5 smoke test: use the repo's `PREFIX` in the example patterns.

**Phase 2 — django-mojo scripts + config**
3. Create `planning/.config` containing (comment header as in the spec) and
   `PREFIX=DM`.
4. Replace `scripts/intake.sh` and `scripts/ready.sh` with the updated spec
   bodies verbatim (migration-doc rule: copy, don't hand-improve).
   `start.sh`/`board.sh`/`close.sh` untouched.

**Phase 3 — rename the 41 items + rewrite ids**
5. Rename (guard each glob with `[ -e "$f" ]`; run from repo root):
   ```bash
   for f in planning/confirmed/ITEM-*.md planning/done/ITEM-*.md \
            planning/in_progress/ITEM-*.md; do
     [ -e "$f" ] || continue
     b="$(basename "$f")"
     git mv "$f" "$(dirname "$f")/DM-${b#ITEM-}"
   done
   ```
6. Rewrite ids inside files — whole-file, digits required, skip anything
   preceded by `#` (preserves `mverify_portal#ITEM-014`-style cross-repo refs):
   ```bash
   perl -i -pe 's/(?<!#)\bITEM-(\d+)/DM-$1/g' \
     planning/confirmed/*.md planning/done/*.md planning/in_progress/*.md \
     planning/inbox/*.md memory.md
   ```
   (Globs exclude `planning/_template.md` by construction — it's edited by
   hand in Phase 4. This also rewrites each renamed file's frontmatter
   `id:` line; no separate step needed.) Eyeball `git diff` afterwards for
   accidental hits.
7. `planning/.next_id` stays `42`.

**Phase 4 — docs sweep (this repo)**
8. `CLAUDE.md:39` — "Every item gets `ITEM-###`…" → ids are `DM-###` here;
   prefix comes from `planning/.config` (fallback `ITEM`).
9. `AI_DEV.md:37,78` — same substitution in prose/diagram.
10. `planning/_template.md:10-11` — example refs →
    `[DM-003, wmwx/wmx_api#WA-007]` and `[DM-009]`.
11. `.claude/skills/scope/SKILL.md:23,44,51,52` — prose "`ITEM-###`" →
    "`DM-###` (prefix from `planning/.config`)"; example `id: DM-014`; example
    refs as in the template.
12. `.claude/skills/request/SKILL.md:61` — replace "(a fresh session is
    ideal)." with "(same session is fine — the item file carries everything;
    start fresh if this one is already long)."

**Phase 5 — verify + close**
13. See Tests below. Close with
    `scripts/close.sh planning/in_progress/DM-041-*.md` (the item's own file
    was renamed in Phase 3 — use the new path).

### Design decisions
- **Config is shell-sourced** (`. planning/.config`) — simplest thing that
  works in bash scripts; the file is repo-owned so sourcing it is safe.
  Rejected: parsing a YAML/INI (needless machinery).
- **PREFIX-only, no pad-width knob** — KISS; `%03d` stays hardcoded. Another
  knob can be added later if ever needed.
- **Fallback `PREFIX=ITEM` when config absent** — repos not yet rolled out
  keep working unchanged; rollout order across repos doesn't matter.
- **Only intake.sh + ready.sh change** — the other three scripts are already
  prefix-agnostic (verified by recon); minimal diff, still byte-identical
  across repos once synced.
- **Whole-file perl rewrite with `(?<!#)\bITEM-(\d+)`** — digits required, so
  `ITEM-###` prose placeholders are untouched; `#`-lookbehind preserves
  cross-repo `repo#ITEM-NNN` refs. Rejected: rewriting only `related:` lines
  (leaves stale ids in prose that future sessions would grep for and miss).
- **Rename first or config first — order is safe** — intake reconciles
  `N = max(counter, highest-assigned+1)` and counter is already 42, so no
  duplicate can be minted mid-transition.
- **memory.md refs rewritten too** (user-approved) — future sessions grep
  memory for item ids; stale `ITEM-` refs would dangle after the rename.

### Edge cases & risks
- **This item renames itself** — DM-041 will sit in `planning/in_progress/`
  during the build; the Phase 5 close must target `DM-041-*`. board/ready/
  close operate on basenames+frontmatter, so the renamed file is handled fine.
- **Cross-repo ref `mverify_portal#ITEM-014`** — protected by the `(?<!#)`
  lookbehind; verify it survived in the diff.
- **Counter poisoning** — after the rename, intake's reconcile greps look for
  `DM-` ids; highest is DM-041 → floor 42, counter 42 → next id DM-042. The
  smoke test asserts exactly this.
- **`set -u` + sourcing** — the config sets `PREFIX` before first use;
  `PREFIX="${PREFIX:-ITEM}"` guards the config-less case, so `set -u` in
  intake.sh can't trip.
- **Spec files are outside the repo** — ~/Projects/*.md are not in any git
  repo visible here; edits are plain file edits (no commit). Note this in the
  close-out summary so the user knows those changes aren't version-controlled.
- **Other repos temporarily inconsistent** — expected; fallback keeps them
  working until their own rollout items land.

### Tests
No framework code is touched, so the Python suite is unaffected — but per
`.claude/rules/build-baseline.md`, capture `bin/run_tests --agent` before any
edit and re-run after; both must match (read `var/test_failures.json`).
Workflow verification (the real test — from the migration doc's procedure):
1. `bash -n scripts/*.sh` — all parse.
2. Smoke test, end-to-end, then clean up:
   ```bash
   printf -- '---\nid:\ntype: chore\ntitle: zzz smoke\npriority: P3\nopened: 2026-07-16\n---\n# zzz smoke\n' \
     > planning/inbox/zzz-smoke.md
   scripts/intake.sh planning/inbox/zzz-smoke.md
   # MUST print: DM-042 planning/confirmed/DM-042-zzz-smoke.md (no skip, no ITEM-)
   scripts/ready.sh planning/confirmed/DM-042-zzz-smoke.md   # READY
   scripts/board.sh                                          # renders; DM ids; no BLOCKED
   rm planning/confirmed/DM-042-zzz-smoke.md
   echo 42 > planning/.next_id
   ```
3. `scripts/ready.sh` on `planning/done/DM-022-*.md` — cross-repo ref note
   still appears on stderr, exit behavior unchanged.
4. `git diff` review: no `ITEM-` + digits left in planning item files except
   the preserved `mverify_portal#ITEM-014`; `grep -rn 'ITEM-[0-9]' planning/ memory.md`
   should return only that line.

### Docs
- This repo: covered in Phase 4 (CLAUDE.md, AI_DEV.md, template, two skills,
  memory.md). `docs/django_developer/` / `docs/web_developer/` / CHANGELOG.md:
  no changes — the planning workflow is repo-internal, not framework behavior.
- Canonical spec + migration doc: covered in Phase 1.

### Open questions
- None blocking this item. Deferred to the per-repo rollout items: wmx
  prefixes (`WP`/`WA` proposed), handling of wmx legacy non-ITEM files
  (recommend: leave `done/` history alone per the migration doc), prefixes for
  the five older repos, and filing web-mojo's `WM` rollout item.

## Notes
Baseline (2026-07-16, `bin/run_tests --agent`): GREEN — 2769 total / 2388
passed / 0 failed / 381 skipped (skips = opt-in modules test_incident,
test_security + a few env-dependent). User directive: no tests required for
this item — no project code is changed (shell scripts + markdown only); the
post-build test-runner agent is skipped accordingly.

Recon (2026-07-16): `ITEM-` format coupling in this repo — `scripts/intake.sh`
(id allocation `printf 'ITEM-%03d'`, dest filename, counter reconciliation grep
for `^id:` frontmatter and `ITEM-*-*.md` filenames), `scripts/ready.sh`
(`norm()` zero-padding canonicalizer assumes `ITEM-` prefix),
`planning/_template.md` (comment examples), `CLAUDE.md` (workflow docs).
`board.sh`/`close.sh`/`start.sh` operate on basenames/frontmatter generically.

Repo survey (2026-07-16): four repos share the intake workflow (django-mojo,
web-mojo, wmx_portal, wmx_api — counters 41/28/75/130); five have older
planning layouts and need the full `migrate-agent-workflow.md` treatment
(hamp-backend, legacy-portal, reseaudent_api, android-mojo, wmx_test_client).

## Resolution
- closed: 2026-07-16
- branch: main
- files changed: .claude/skills/request/SKILL.md,.claude/skills/scope/SKILL.md,AI_DEV.md,CHANGELOG.md,CLAUDE.md,docs/django_developer/account/api_keys.md,docs/django_developer/core/permissions.md,docs/web_developer/account/api_keys.md,memory.md,mojo/apps/account/models/api_key.py,mojo/decorators/auth.py,mojo/models/rest.py,planning/.config,planning/.next_id,planning/_template.md,planning/confirmed/DM-038-rest-batch-save-ignores-can-update-can-create-flag.md,planning/confirmed/DM-039-get-api-group-pk-member-resolves-touches-any-group.md,planning/confirmed/DM-040-incident-maestroboard-push-link-tickets-into-a-rem.md,planning/done/DM-001-render-allowlisted-extra-registration-fields-promo.md,planning/done/DM-002-step-up-recent-authentication-gate-for-sensitive-o.md,planning/done/DM-003-register-page-enter-on-phone-otp-field-fires-step-.md,planning/done/DM-004-sign-in-alternate-method-button-row-overflows-clip.md,planning/done/DM-005-phone-register-one-wrong-sms-code-burns-the-sessio.md,planning/done/DM-006-sms-sign-in-with-an-unrecognized-number-dead-ends-.md,planning/done/DM-007-full-test-suite-is-flaky-content-guard-false-posit.md,planning/done/DM-008-phone-signup-may-fail-to-sign-in-an-existing-accou.md,planning/done/DM-009-get-remote-ip-trusts-client-supplied-x-forwarded-f.md,planning/done/DM-010-websocket-ip-resolver-trusts-client-spoofable-sour.md,planning/done/DM-011-ip-storage-fields-assume-ipv4-non-null-ipv6-trunca.md,planning/done/DM-012-auth-middleware-500s-on-a-malformed-authorization-.md,planning/done/DM-013-management-command-to-create-initial-users-admins.md,planning/done/DM-014-var-dev-server-conf-overrides-config-dev-server-co.md,planning/done/DM-015-configurable-outbound-webhook-signature-header-use.md,planning/done/DM-016-group-user-has-permission-crashes-on-apikey-identi.md,planning/done/DM-017-geofence-config-evidence-plane-editable-system-rul.md,planning/done/DM-018-audit-requires-perms-group-fallback-on-global-effe.md,planning/done/DM-019-self-minted-group-apikey-with-arbitrary-permission.md,planning/done/DM-020-geofence-evidence-metrics-dual-write-group-scoped-.md,planning/done/DM-021-geofence-hardening-opt-in-strict-compliance-enforc.md,planning/done/DM-022-member-readable-geofence-policy-events-group-scope.md,planning/done/DM-023-geofence-adjacent-settings-bypass-write-time-valid.md,planning/done/DM-024-same-key-in-query-string-json-body-merges-to-a-lis.md,planning/done/DM-025-dispatcher-numeric-group-resolution-skips-is-activ.md,planning/done/DM-026-github-oauth-login-on-the-bouncer-hosted-auth-page.md,planning/done/DM-027-group-rest-save-collapses-to-the-view-check-any-ac.md,planning/done/DM-028-post-api-group-member-invite-returns-a-raw-500-typ.md,planning/done/DM-029-add-explicit-auth-gates-to-the-permission-check-si.md,planning/done/DM-030-jsonfield-replace-bypasses-protected-json-perms-ma.md,planning/done/DM-031-geofence-test-override-mojo-test-mode-are-db-redis.md,planning/done/DM-032-rest-batch-save-skips-instance-level-permission-ch.md,planning/done/DM-033-fileman-initiated-uploads-can-t-be-completed-or-fk.md,planning/done/DM-034-oauth-login-drops-the-redirect-param-user-lands-on.md,planning/done/DM-035-field-action-level-permission-gates-omit-the-base-.md,planning/done/DM-036-apikey-set-permissions-silently-discards-non-dict-.md,planning/done/DM-037-apikey-validate-token-grants-group-context-without.md,planning/inbox/apikey-group-context-ignores-group-is-active.md,planning/inbox/batch-ignores-can-update-can-create-flags.md,planning/inbox/group-me-member-endpoint-oracle-touch.md,planning/inbox/member-perms-ignore-group-is-active.md,planning/inbox/phone-verify-dev-bypass-code-db-settable.md,planning/inbox/test-security-full-suite-red.md,planning/inbox/user-is-superuser-unguarded-on-non-user-identity.md,scripts/intake.sh,scripts/ready.sh,tests/test_global_perms/apikey_group_inactive.py,uv.lock
- tests added: none (user directive — no project code changed; verified via smoke test: bash -n all scripts, throwaway intake minted DM-042, board/ready clean, baseline suite green pre-change)
