---
name: maestro-build
description: >-
  Claim one or more planned maestro board items (owner + stage=building),
  execute each ## Plan inside this repo with full build discipline, keep every
  item's activity trail updated (commits, tests, blockers), and land them at
  review/done via the maestro MCP.
user-invocable: true
argument-hint: <item-ids, e.g. "431" or "431 432" (omit to pick from the board)>
maestro-skill-version: 16
---

# Maestro Build — Execute a Planned Item

You are a senior engineer executing a scoped item one task at a time: minimal,
correct, tested code matching the repo's existing patterns and conventions (read
`AGENTS.md` when present, plus `CLAUDE.md` and applicable project rules). The
board item is the work record — keep its stage and activity trail current
throughout.

## One Item or Many

**Takes any number of ids** — `/maestro-build 431`, `/maestro-build 431 432
438`. **Sequential is the default.** In a repo whose suite targets a fixed port
and a shared database, and whose work commits to one branch in one working tree,
parallel builds collide on the port, corrupt the database and interleave
commits.

**Build in parallel only when both hold**, and say which:

1. **The repo isolates a checkout** — each worktree gets its own test database,
   port and cache namespace. Verify it; do not assume. The repo's git/testing
   rules say so explicitly when it does.
2. **The items are independent on disk** — no shared files, no migrations to the
   same app (two trees generate the same `000N_` and clash at merge), and
   neither consumes what the other creates.

Either one missing → sequential. Parallelism is a speed optimization; a merge
conflict inside someone else's build is not.

Parallel runs one worktree per item, each on its own branch, then merges every
branch back **one at a time** and verifies once in the primary tree — each build
proved itself in isolation, and the combined tree is what ships. Clean up every
worktree and branch you created. Follow the repo's own worktree lifecycle where
it documents one; it will know setup steps (dependency install, gitignored
config) that a bare `git worktree add` skips.

With several ids, run the flow below per item, with these differences:

- **Confirm the whole roster once** — one wrong-id card per item, in one block,
  one ask — before the first claim, and call out two ids a digit apart.
- **Partition the items before starting.** Which build in parallel, which are
  forced sequential and by what — a shared file, a migration to the same app, a
  plan consuming a helper another plan creates. State the partition and the
  reason for every sequential edge; a batch that all goes one lane should say
  why that was the right call.
- **Merge and clean up before verifying.** Every branch back into the primary
  tree one at a time, every worktree and branch removed, then the closing run.
- **Claim, snapshot, build and comment per item** exactly as below, then leave
  each at `building` — built, not verified.
- **Verify once, at the end** (see "Verification Across Several Items"), and
  only then flip every green item in one pass.
- **A failed item does not fail the run.** Blocker comment, leave it at
  `building` with its owner intact, continue with the remaining **independent**
  items, halt the ones that depended on it, and say which.
- **Report once** for the run, one block per item.

Past two or three items, delegating each build to a fresh-context sub-agent
keeps the later plans from being executed by a session that has forgotten the
first — that is what `/maestro-auto` does, and it also handles the scoping and
folds everything into one approval gate.

## Board Resolution

Same as `maestro-task`: read Maestro's `.claude/maestro.json` repo config; on any miss, resolve via
`whoami()` / `list_workspaces()` / `list_boards()`, ask, offer to write the
file. Maestro unreachable **before claiming** → stop with an explicit notice;
offer the repo's local build skill if one exists.

Also from `maestro-task` — **step 5: name boards and items when you report them, never bare ids**: `board "Backlog" (Maestro workspace, id 8)`, `#586 "An agent cannot see what it deployed"`, with the item URL (link format under "Reporting Back"). That lookup is far cheaper for you than for the user.

**Step 7: pass `client=` (the client you are running in) and `model=` (your model id) on every `create_board_item`, `update_board_item` and `comment_on_item` call.** The server cannot observe either — a write that omits them lands in the trail under the default label, "via Claude", whoever wrote it.

## Checkout lifecycle

After the wrong-id confirmation, acquire before the claim or snapshot with
`acquire_board_item_checkout(item, purpose="build",
idempotency_key=<fresh high-entropy key>, expected_minutes=...)`. Keep the raw
`checkout_token` only in this orchestrating session. It is a bearer capability
and appears in the owning MCP transcript by design; never copy it into domain
data, comments, logs, URLs, realtime messages, durable browser storage, or a
sub-agent prompt/transcript.

If acquire reports a conflict, show the human the holder, purpose, and expiry,
then ask whether to wait or continue without a checkout. This build cannot make
that decision silently. A checkout is advisory: continuing is allowed. Never
pass its token to ordinary board writes. After the acquire attempt,
re-fetch `get_work_packet(item)` and latest comments before the claim. If title,
stage, description, owner, or contract materially changed, reconfirm. Sub-agents
always start in fresh isolated context and never acquire or receive the token.

Renew no later than 40 minutes after acquire/last renewal, around long commands
and waits, and before consuming a sub-agent result. Keep waits short enough to
heartbeat. Lost, expired, or replaced ownership means report that the advisory
signal is gone and do not claim it is still held; it does not gate board writes.
Check in after the
done/review write or after the blocker comment, and on every other terminal
path. During an outage local work may finish; re-read before any board push
after reconnect.

## Confirm the Item — the wrong-id gate

**An item id is not self-describing.** Ids get transposed, copied out of the
wrong session, or read off a stale list, and a build compounds it fastest: the
claim stamps stage and owner on someone else's item and the commits land before
anybody notices. So the gate comes **before the claim** — the first mutation in
this skill.

Required whenever the id came from an argument, a note, or another session.
Skip only if the user just picked the item off a list you presented this
session (they have seen the title), or waives it outright.

Build the card from **one** `get_work_packet(item)`. It carries the workspec,
the board and workspace **names**, who filed it, when the workspec last changed,
how long the item has sat in its stage, whether it was resumed from parked, its
parent, the verification contract, and the rules the workspec references — the
whole card, and most of the pre-flight below, in one call. Older server without
the tool: `get_board_item(item)` plus one `list_boards(<the item's workspace>)`
for the names, and say you fell back.

    #586 "An agent cannot see what it deployed — add preview_site"
    board "Backlog" · workspace "Maestro" · stage planned · must
    filed by Ian Starnes 2026-07-12 (2 weeks ago), scoped 2026-07-26
    part of #516 "Sites + domains — release hardening (epic)"
    https://maestromojo.com/workspaces/#/board/8?item=586

    Agents ship sites they never see: deploy_site returns byte counts, not
    pixels. Wants an MCP render tool so an agent can look at what it
    deployed.

    Plan: new preview_site MCP tool over a server-side renderer, desktop +
    mobile presets, snapshot cached per revision. 3 commits, ~6 tests, both
    doc tracks.

    Build this one? (claiming it sets stage=building and owner=you)

- **Title first, then where it lives** — `board_name` and `workspace_name`; an
  id cannot be recognised.
- **Who filed it and when**: `filed.by` / `filed.age`, plus `spec_updated` —
  when the workspec last changed, which on a scoped item is when the plan was
  written. Give the plain words the packet already ships ("filed today",
  "spec updated 3 weeks ago"); a plan sitting for weeks may be stale. Note that
  any description save moves `spec_updated`, so it dates the workspec, not the
  scoping — you judge whether the change was a re-scope.
- **TL;DR in the requester's terms**, then **the plan in 2-3 lines** — what is
  about to be executed, and its size. On a build the plan is what is being
  confirmed; the problem statement just makes the item recognisable.
- **Name the parent** if it has one, and how many sub-items it has — an
  unexpected epic is the loudest wrong-id signal there is.

**Say these out loud before asking, whenever they are true:**

- The item's `board` is a **sibling board in the same workspace** — normal under
  a deliberate split; confirm it is the board you meant, since ids interleave
  across boards. A different `workspace` is almost always a wrong id, and would
  build another repo's work in this tree.
- Its stage is not `planned` — `inbox` means it was never scoped, `review` or
  `done` means it already shipped.
- `values.owner` is someone else (per step 3), or `whoami()` is not the filer.
  Not an error by itself, but the user should see whose work they are taking over
  before the claim writes their name on it — by NAME, which the packet's
  `people` map resolves from the owner id.
- `resumed_from_parked` is present. Say the `warning` verbatim when it carries
  one: the plan predates the resume and the item wants re-scoping, not building.
  When `spec_updated_after_resume` is true there is no warning — the workspec
  moved after the resume — but say the item was parked and resumed anyway.

Then wait for a clear answer; an ambiguous one is not a yes. If it is the wrong
item, ask for the right id — never go hunting the board for the item they
probably meant.

## Pre-Flight

1. **Pick the item(s).** With item-id arguments, use them — one or several.
   Without any, call
   `workspace_queue(workspace, stage="planned")` — it spans **every** board in
   the workspace, where `get_board(board)` sees only the board
   `.claude/maestro.json` names and reports a short queue, no error, wherever a
   team routes work across two. Rows span boards, so list candidates with
   **their board name** (id, title, board, priority) and ask which to build —
   the user may name more than one. Never claim silently.
   Read the reply's `boards` sidecar before presenting the list: a board whose
   `unmatched_stages` names `planned` does not carry that stage at all — re-ask
   it with a value from its own `stage_values` rather than reporting it empty.
   If `workspace_queue` is unavailable (older server), fall back to
   `get_board(board)` and say so.
2. `get_work_packet(item)` — one call for the whole pre-flight: the workspec,
   the names, the staleness facts, the contract, and the workspec's rules
   inlined under `rules.applied` (apply them; name any `rules.missing` slug out
   loud). The description must contain a `## Plan` — if not, stop and point at
   `/maestro-scope <item-id>`.
   **Refuse a `parked` item** — `stage.is_parked` is the check: parking is a
   deliberate "not now" and its plan is presumed stale. Say so and stop, naming
   the stage it was parked from (`stage.parked.prior_label`) — it is resumed
   from the board (or the drawer's Resume button) and re-scoped first.
   `stage.age` is how long it has sat where it is; a plan that has waited
   months deserves a sentence before you build on it.
3. If `values.owner` is already set to someone else, stop and ask before taking
   it over.
4. **Confirm.** Run the wrong-id gate above and get a yes. Nothing below this
   line is reversible for free: the claim writes to the item and the snapshot
   commit writes to the repo.
5. **Claim** in one call:
   `update_board_item(item, values={"stage": "building", "owner": [<your user id from whoami()>]})`.
6. **Snapshot.** Write the pulled description to `planning/built/<item-id>.md`
   (create the directory if absent), first line: `<!-- generated from maestro
   item <id> — do not edit; the board item is the source of truth -->`. Commit
   it as the build-start marker. (`planning/` is deliberately **not** indexed
   into the knowledge base — the snapshot is a git provenance record, not
   knowledge. See item 317.)
7. Pull the description to `planning/.cache/<item-id>.md` (gitignored) — the
   working copy for the session.
8. **Read the verification tier** and set up for it. The step-2 packet already
   carries `quality_contract` — `{"tier", "run", "evidence"}`, the decision
   scoping made as data — so this costs no extra call. (Older server without
   `get_work_packet`: `get_board_item` returns the same key.) Read it from
   there; the `### Verification` prose is the human copy of the same thing and
   the fallback for items scoped before contracts shipped.
   With several items, read them all now and take the **highest** as the run's
   tier — the baseline decision below is made once, for the run, before the
   first claim:
   - **`full`** → establish the repo's green baseline now, per its own test
     conventions. Red baseline → stop and tell the user; don't build on red
     without their say-so.
   - **`targeted`** or **`none`** → **no baseline run.** Start building.
     Whatever `run` names still runs, at every tier — that is where a bug's
     regression test rides on a `none` item.
   - **Neither a `quality_contract` nor a `### Verification` block** (a plan
     scoped before tiers existed, or one that skipped it) → pick the tier
     yourself from the taxonomy in `maestro-scope`, and say which one you picked
     and why in workflow step 1. Never default to `full` because the plan is
     silent — that is the cost this mechanism exists to remove.

## Workflow

1. **Say what you're about to do differently.** The gate already showed what the
   item is; don't repeat it. It did not cover intent: before the first edit,
   state anything in the plan you already mean to deviate from, and re-name the
   item as a markdown link (see "Reporting Back") as the build's anchor. Then
   build.
2. Read every file the plan touches before editing — no blind edits.
3. **If the workspec says `Kind: bug`** — on the `## Spec` meta line, or in the
   header bullets of an item filed before that shape — write a regression test
   that reproduces the bug and confirm it FAILS before touching the fix.
4. Implement one logical unit at a time, following the repo's conventions.
   Write/finish tests immediately after each unit, not at the end. Fix failures
   in your code, not the tests.
5. Commit each logical unit per the repo's git conventions (no push unless the
   repo's rules say otherwise).
6. **After each commit / test run / blocker**, post to the trail:
   `comment_on_item(item, ...)` — commit hash + one-line summary, test counts, or
   the blocker. If the plan changed during the build, push the updated scratch
   file back with `update_board_item(item, description=...)`.
7. Update the repo's docs and changelog per its conventions.
8. **Verify at the plan's tier — that tier, not a bigger one.**
   - **`none`** → no test run. Do the thing the plan's `Why:` line named in
     place of tests, and report both.
   - **`targeted`** → run the modules the plan named, and stop there. Running a
     full sweep to feel safe spends the user's minutes on your comfort.
   - **`full`** → run the repo's full suite and report it against the pre-flight
     baseline.

   **Escalate freely, downgrade never.** If the diff acquired something the plan
   did not anticipate — a migration, a shared helper, a changed contract, a
   second app touched — move up a tier, run it, and say in the report that you
   escalated and why. Rewrite the contract in the same breath:
   `update_board_item(item, contract={"tier": "full", "run": […],
   "evidence": […]})`. The server records the tier change on the trail itself,
   so the item's declared tier keeps matching what actually ran instead of
   drifting behind it. Moving *down* a tier is the user's call, not yours.
9. **Close.** PR opened → `update_board_item(item, values={"stage": "review"})`;
   committed straight to the main branch → `values={"stage": "done"}`. Final
   comment on the trail: what changed + how to validate. Then report back to the
   user TL;DR-first (see below). **In a multi-item run, steps 8-9 do not happen
   per item** — the item stays at `building` and the next one starts; the run's
   single closing pass verifies and flips them all.
10. **On failure/blocker**: post a blocker comment, leave `stage=building` and
    the owner intact, and tell the user where it stands — in the same
    TL;DR-first shape.

## Verification Across Several Items

One closing run for the whole set, never one sweep per item.

- **The run's tier is the highest tier any item carries.** Two items editing the
  same file, or a helper one creates and another consumes, is a bound that held
  per-item and does not hold together — **escalate both and say so**. That
  judgement exists only at this level; no single plan can make it.
- **Any item at `full`** → one green baseline before the first claim, the full
  suite once after the last item lands. Red baseline → stop and tell the user;
  never build on red.
- **`targeted` / `none` only** → no baseline. The closing run is the union of
  every item's named modules, run once, after the last item lands.
- **Every item at `none`** → no closing run either. Say plainly that no suite
  ran, and what stood in for it per item.
- A bug's regression test is the one exception at any tier: it runs alone,
  before and after that item's fix, because a regression test nobody saw fail
  proves nothing. Seconds.
- **Red** → attribute each failure to the item owning the touched files (the
  per-item commits make this unambiguous), fix in place, re-run the closing set.
  One sweep per fix round. An item you cannot get green stays at `building` with
  a blocker comment naming the failing tests; the rest still flip.
- **Green** → flip every built item in one pass, `review` or `done` per step 9.

Nothing is `done` mid-run: until that sweep is green, the truth is "built".

## Attributing a Red Test Without a Baseline

A baseline answers exactly one question: *was this failure already there?* Buy
that answer when something actually fails, not up front on every build. When a
run comes back red below `full`:

1. **Read the failure first.** Most name your change unambiguously — the file,
   the assertion, the symbol you just touched.
2. If it doesn't, stash the working tree (`git stash -u`), re-run **that same
   targeted test**, and unstash. Seconds, and it settles the question where the
   doubt is.
3. A failure that predates you gets **reported, not silently fixed** — it is not
   yours and not part of this item. Say so in the summary.

`full` keeps its up-front baseline because attributing a red *suite* after the
fact is neither cheap nor unambiguous.

## Reporting Back

Both the step-1 orientation and the closing summary name the item as a **markdown
link**, never a bare id:

```
[#<id> <title> (<stage>)](<url>)
```

`url` comes from the tool result (`get_work_packet` and `get_board_item` both
return it) — never
hand-assemble a host. In a repo with a GitHub remote the client auto-linkifies a
bare `#123` to that repo's issue 123 — a live link to the wrong system.

The closing summary opens with a short block, before any detail:

- **What shipped** — the item title plus the original ask in one sentence, in
  the requester's terms, not the codebase's.
- **What changed** — the commits, one line each.
- **What deviated from the plan** — anything done differently and why, or
  "followed the plan" if nothing moved. Never let a deviation surface only in a
  diff.
- **Verification** — the tier, the commands run, and their result, stated
  plainly, and whether that was the planned tier or an escalation, with why. At
  `none`, say outright that no tests were run and what stood in for them — a
  reader must never infer that from silence. A test that failed, was skipped, or
  could not run goes here, never omitted.
- **Stage** — where the item landed, and what remains if it is not `done`.

Keep it to roughly 15 lines. Detail lives in the commits and the item's activity
trail — point at them, don't inline them. A multi-item run reports **once**: the
same five headings, one line per item under each, with Verification stated for
the run (its tier, the single closing sweep, and any escalation) rather than per
item — roughly 20 lines for the whole run.

A blocker report follows the same shape: name the item, what stopped it, what is
committed so far, and what you need to proceed.

## Outage Mid-Build

Never block the build on maestro. Finish locally against the scratch copy;
collect the stage flip and pending comments in your final summary as exact tool
calls for the user (or next session) to replay. Retry each push once before
queueing it.

## Forbidden

- Claiming, snapshotting or building an item the user has not confirmed — the
  gate is the only thing between a fat-fingered id and someone else's item
- Building an item with no `## Plan`, or claiming over someone else's owner
  without asking
- Expanding scope beyond the item; touching files outside the plan without
  flagging it first
- Writing no tests for a behavior change — the tier decides which tests are
  *run*, never whether the change is covered
- Downgrading the plan's verification tier without asking. Escalating is always
  yours to do; relaxing is the user's
- Reporting a build as verified when the tier's runs did not happen, or quietly
  running a full suite the plan did not ask for
- Leaving the item's stage stale after the build ends
- Closing a build without stating what deviated from the plan and how
  verification actually went
- Building several items concurrently in a repo with a shared test port,
  database or working tree, or flipping any of them past `building` before the
  run's single closing sweep is green
