---
name: maestro-scope
description: >-
  Pull one or more maestro board items, scope each inside this repo with full
  investigation rigor, append a file-level ## Plan to every workspec, and push
  them back (stage=planned) via the maestro MCP.
user-invocable: true
argument-hint: <item-ids, e.g. "431" or "431 432" (omit to pick from the board)>
maestro-skill-version: 17
---

# Maestro Scope — Design the Plan on the Item

Scoping runs **inside the target repo** with full code access; only storage
differs — the plan goes back to the item's workspec, not a local planning file.

## One Item or Many

**Takes any number of ids** — `/maestro-scope 431`, `/maestro-scope 431 432
438`. Scoping is read-only, so it parallelizes cleanly.

- **One item** → run the workflow below inline, in this session.
- **Several** → run the wrong-id gate over the whole roster in one block, then
  **spawn one fresh-context sub-agent per item, all in one message**. Each runs
  workflow steps 3-6 for its item with read-only repo access and returns the
  complete updated description (human block + `## Spec` + `## Plan`), its
  files-touched list, its verification tier, schema/contract impact, and its
  premise check. Then, in this session:
  - **Cross-check the plans before pushing anything** — two plans changing the
    same function or contract in incompatible directions, two migrating the same
    table, the same helper added twice, one item another makes obsolete. Amend
    the affected plans, say what changed, and **escalate a tier a sibling plan
    invalidates** (a shared helper or a shared file bounds nothing per-item).
  - **Present once** for the set: the step-7 TL;DR per item, plus the cross-item
    findings and a build order where a dependency forces one.
  - **Push each item** per step 8, and report which landed and which did not.
- Cap it around six ids. Past that the presentation is a review nobody does —
  propose the highest-priority ones and say what you left.

**Never scope several items inline, one after another, in this session** — by
the fourth plan the session has forgotten the first item's code. Delegate, or
do them in separate sessions.

## Board Resolution

Same as `maestro-task`: read Maestro's `.claude/maestro.json` repo config; on any
miss, resolve via `whoami()` / `list_workspaces()` / `list_boards()`, ask, offer
to write the file. Unreachable or unauthenticated → stop with an explicit
notice; offer the repo's local scoping skill if one exists. Never fall back
silently.

Two of its rules apply here too:

- **Name boards and items when you report them — never bare ids**: `board
  "Backlog" (Maestro workspace, id 8)`, `#586 "An agent cannot see what it
  deployed"`, plus the item URL (link form under Rules).
- **Pass `client=` (the client you are running in) and `model=` (your model id)
  on every `create_board_item`, `update_board_item` and `comment_on_item`
  call.** The server cannot observe either, so a silent write lands in the trail
  under the default label — "via Claude", whoever wrote it.

## Checkout lifecycle

After the wrong-id confirmation and before deep work, call
`acquire_board_item_checkout(item, purpose="scope", idempotency_key=<fresh
high-entropy key>, expected_minutes=...)`. Keep the returned `checkout_token`
only in this orchestrating session. It is a bearer capability that necessarily
appears in this tool transcript; never put it in the workspec, comments,
metadata, logs, URLs, realtime messages, durable browser storage, or any
sub-agent prompt/transcript.

If acquire reports a conflict, show the human the holder, purpose, and expiry,
then ask whether to wait or continue without a checkout. A checkout is advisory:
the human may choose to continue. Never pass its token to ordinary board writes.
Immediately after the acquire attempt, re-fetch `get_work_packet(item)`
and the latest comments. If the title, stage, or description materially changed,
repeat the roster confirmation before continuing. Every post-acquire sub-agent
starts in fresh/isolated context and receives the workspec, never the token or
owning transcript.

Renew with `renew_board_item_checkout` no later than 40 minutes after acquire
or the last renewal, immediately before and after long commands/waits, and
before accepting a sub-agent result. Keep waits short enough to heartbeat. A
`checkout_lost`, `checkout_expired`, or `checkout_replaced` means the advisory
signal is gone: report it and do not pretend the lease is still held. It does
not authorize or block item writes, and the token is used only for renew and
check-in lifecycle calls.

On every terminal path—including success, refusal, blocker, exception, or user
cancellation—call `check_in_board_item_checkout`. If Maestro was unavailable,
local analysis may finish; re-read before any later board push.

## Confirm the Item — the wrong-id gate

**An item id is not self-describing.** Ids get transposed or read off a stale
list, and the failure is silent: scoping completes on the wrong item and
rewrites its workspec. This has happened repeatedly. Before any exploration —
before any scratch file is written or any sub-agent spawns — show the user what
**every** id resolved to and get a yes. Several ids: one card each, in one
block, one ask at the end; and say so explicitly when two of them are a digit
apart, which is the transposition this gate exists to catch.

Required whenever the id came from an argument, a note, or another session. Skip
only when the user just picked the item off a list you presented this session
(they have seen the title), or when they waive it outright.

Build the card from **one** `get_work_packet(item)`. It carries everything the
card needs — the workspec, the board and workspace **names**, who filed it and
when, how long it has sat in its stage, its parent, and the rules the workspec
references — so nothing here needs a second call. Older server without the tool:
`get_board_item(item)` plus one `list_boards(<the item's workspace>)` for the
names, and say you fell back.

    #586 "An agent cannot see what it deployed — add preview_site"
    board "Backlog" · workspace "Maestro" · stage inbox · must
    filed by Ian Starnes on 2026-07-29 (today)
    part of #516 "Sites + domains — release hardening (epic)"
    https://maestromojo.com/workspaces/#/board/8?item=586

    Agents ship sites they never see: deploy_site returns byte counts, not
    pixels, and every cheap substitute (the browser pane, headless Chrome)
    lies in a different direction. Wants an MCP render tool so an agent can
    look at what it deployed.

    Scope this one?

- **Title first, then where it lives** — `board_name` and `workspace_name`; an
  id cannot be recognised.
- **Filer and age**: `filed.by` and `filed.age`, plus `spec_updated` when the
  workspec has been touched since. Both ship the plain words already ("today",
  "3 weeks ago") — give them, not just the date; a bare date doesn't register as
  stale.
- **TL;DR in the requester's terms**, 2-3 sentences — the problem and what they
  want, not the codebase's framing, which is not what the user recognises. Quote
  the workspec's human block rather than re-deriving one.
- **Parent and sub-item count**, if it has them — an unexpected epic is the
  loudest wrong-id signal there is.

**Say these out loud before asking, whenever they are true:**

- The item's `board` is a **sibling board in the same workspace** — normal under
  a deliberate split, but confirm it is the board you meant; ids interleave
  across boards. A different `workspace` is almost always a wrong id — another
  repo's board.
- It already has a `## Plan` — it is scoped, and re-scoping overwrites it.
- Its stage is `building`, `review` or `done`, or `values.owner` is someone else
  — live work with a person attached. Name that person: the packet's `people`
  resolves owner ids to names, so "owned by [7]" is never the sentence.
  (`parked` is refused outright, per step 1.)
- You are not authenticated as the filer — `whoami()` is free, make the call.
  Not an error on a shared board, but say whose work it is: "the wrong person
  scoping it" is half of how the wrong item gets scoped.

None is a hard refusal except `parked` — but the user must see them **before**
saying yes, not after the workspec has been rewritten.

Then wait for a clear answer; an ambiguous one is not a yes. If it is the wrong
item, ask for the right id — never go hunting for the item they probably meant.

## Workflow

1. **Pick the item(s).** With item-id arguments, use them — one or several.
   Otherwise call
   `workspace_queue(workspace, stage="inbox")` — it spans **every** board in the
   workspace, where `get_board(board)` sees only the board
   `.claude/maestro.json` names and silently under-reports when work spans two.
   List candidates with **their board name** (id, title, board, priority) and
   ask which to scope — the user may name more than one. Never pick silently.
   Read the reply's `boards` sidecar first: a board whose `unmatched_stages`
   names `inbox` does not carry that stage at all — re-ask it with a value from
   its own `stage_values` rather than reporting it empty. If `workspace_queue`
   is unavailable (older server), fall back to `get_board(board)` and say so.
   **Skip `parked` items**: parking is a deliberate "not now", so a parked item
   is never a candidate unless the user names it outright.
2. **Pull, then confirm.** `get_work_packet(item)` → run the wrong-id gate and
   get a yes **before** anything else in this step. Only then write the packet's
   `description` verbatim to `planning/.cache/<item-id>.md` (create
   `planning/.cache/` if absent; make sure it is gitignored — offer to add the
   entry). That file is the session's working copy: edit it, not the item. Read
   the activity trail too — requester comments are scope input.
3. **Context.** The packet already carries the rules the workspec's `Apply
   rules:` line names, inlined under `rules.applied` — apply them; that is the
   full text, not a summary. **Say any `rules.missing` slug out loud**: the
   workspec told you to apply a rule that resolves to nothing, and that is the
   user's to fix, not yours to skip silently. Call
   `get_workspace_context(workspace)` only for what the packet does not carry —
   a workspec naming no rules at all, or the workspace's `challenge` doc for
   step 6.
4. **Deep exploration.** Read every file the workspec references; check existing
   patterns and helpers in the target app; fetch framework docs when framework
   features are involved. Depth must match a local scoping session — the board
   changes storage, not rigor.
5. **Design.** Append a `## Plan` section to the scratch file:
   - Objective (exact outcome)
   - Ordered implementation steps with file paths
   - Design decisions (why this approach over alternatives)
   - Edge cases and error handling
   - Testing plan (what to add/update)
   - Documentation plan
   - A `### Verification` subsection (see below) — required.

   It must be complete enough to execute without re-exploring. Resolve open
   decisions; don't leave both options.

   **Write it for the build session, not for a reader following your
   reasoning** — it is read once, by a session that must not miss the one
   constraint it contains:
   - **State each fact once.** A constraint in three sections is three places to
     update.
   - **Conclusions, not the investigation.** "`_vector_leg` has no distance
     floor (`knowledge.py:333`)" — not how it was found; a measurement earns its
     place only when the *number* is load-bearing.
   - **Terse bullets carrying `file:line`.** The build session greps them.
   - **Full sentences, though.** Compress by removing repetition and narrative,
     never by abbreviating: a fragment that saves ten tokens and costs one
     misread constraint is a bad trade.

   Two duties on the workspec above the plan, settled at the push in step 8 —
   draft them here:
   - **Convert a legacy-shape workspec** (`# H1` + `## Description` /
     `## Context`, filed before the two-tier template) to the current shape in
     the scratch file: human block distilled from Description and Context,
     everything technical below the `---` under `## Spec`. Scoping is the
     migration path; an item that passes through and stays legacy-shaped never
     gets converted at all.
   - **Refresh the human block** when scoping changed the story — a false
     premise, a different defect than the one filed, scope that moved. A block
     still describing a problem that turned out not to exist is the failure this
     shape exists to prevent.
6. **Challenge — an independent red-team of the draft plan.** The author does
   not grade their own homework. Spawn ONE fresh-context agent (e.g.
   general-purpose, read access to the repo) with the workspec, the draft
   `## Plan`, and the workspace `challenge` skill doc (slug `challenge`, from
   step 3; if the workspace lacks it, brief the agent with its core rules: name
   untested assumptions, argue the strongest opposing case, never invent a flaw
   to perform thoroughness). Its brief: **refute the plan** — the untested
   assumption most likely to be wrong, the strongest failure scenario, the
   weakest design decision. Verify the objections yourself (measure, read,
   re-derive; the challenger may not run code), then give each a disposition in
   a `### Challenge` subsection of the plan — **a table, one row per
   objection**, not a narrative:

       | # | Objection | Disposition | Evidence / what changed |
       |---|-----------|-------------|-------------------------|
       | 1 | <the objection in one line> | **amended** | <what changed in the plan> |
       | 2 | <the objection in one line> | **rebutted** | <the evidence, file:line> |

   `amended` says what changed; `rebutted` carries evidence or reasoning, never
   "considered". "No substantive challenge" is a valid verdict on small items,
   recorded as that one line instead of a table. A plan with an undispositioned
   objection is not ready to present.
7. **Present — lead with a TL;DR**, since the user is running many sessions and
   will not remember what `<item-id>` refers to. Open with a short block, before
   any detail:
   - **What this item is** — the title, plus the original ask in one sentence in
     the requester's terms, not the codebase's.
   - **What scoping changed** — only findings that change the shape of the work:
     a workspec assumption that turned out false, a bug found on the way, an
     overlapping item that already shipped. Omit if nothing moved.
   - **The plan** — 2-4 lines.
   - **Challenged** — what the red-team objected to and what it changed;
     surviving rebuttals the user might overturn go under Decisions to confirm.
     "No substantive challenge" is reportable as-is.
   - **Decisions to confirm** — ones you resolved that the user might reverse;
     name the option you took and why.
   - **Size and verification** — commits, tests, docs touched, plus the
     verification tier and its one-line bound; the user may well overturn the
     tier, and it is far cheaper here than mid-build.

   Roughly 15 lines. The scratch file holds the full plan — link it, don't
   inline it. Then iterate until confirmed.
8. **Push.** Two checks first, in this order — the push replaces the whole
   description, so anything not done by now is gone:
   - **Re-read the human block against the final plan.** An `amended`
     disposition from step 6 can change what the item *is*, leaving the block
     step 5 drafted describing the old understanding. The block that ships must
     match the plan that ships.
   - **Archive a superseded premise before overwriting it.** When the item no
     longer says what it said — a bug that turned out not to be one, a cause
     that turned out to be something else — `comment_on_item(item, <the original
     description, verbatim>)` **first**. The trail records "Description updated"
     but never the old text, so this comment is the only durable copy. The new
     description keeps a ≤3-line summary of the corrected claim so a reader
     knows the record changed; the plan is the authority.

   Then `update_board_item(item, description=<full scratch file contents>,
   values={"stage": "planned"}, contract={"tier": …, "run": […],
   "evidence": […]})` — description replaces whole; `contract` carries the same
   verification decision as data (see below) — and
   `comment_on_item(item, <3-5 line plan summary>)`.
9. Hand off: "run `/maestro-build <item-id>` to build it" — name the item title
   alongside the id; that line is often read back in a later session. Several
   items scoped in this session hand off as one line carrying every id
   (`/maestro-build 431 432 438`, or `/maestro-auto` for the unsupervised
   route), in the build order the cross-check produced.

## Verification Tier — how much testing this change actually needs

**A scoping decision, and it belongs here.** By step 4 you know the change's
blast radius better than the build session ever will — and without a call from
you it defaults to the expensive option.

**Every tier below describes what the *build* will do — scoping itself runs no
tests.** The suite serializes on one port and one test database, so a baseline
taken here collides with any build in flight, and parallel scoping sub-agents
collide with each other. It would also be stale by the time the build merges
`origin/main`. Reproducing a bug you are scoping is the one exception: run that
one test, not a suite, and never while a build is running.

Append a `### Verification` subsection naming exactly one tier:

- **`none`** — a test run would prove nothing. Docs, changelog, comments, skill
  or prose files; or a change verified directly and more convincingly by other
  means — loading the config and reading back the resolved value, running the
  one command the change affects. **No test run, before or after.** The `Why:`
  line must say what stands in for the tests.
- **`targeted`** — the default, where most items land. The behavior change is
  contained inside test modules you can **name**. No baseline run; the build
  runs the named modules after the change. Name them explicitly — "the relevant
  tests" is not a plan, it is the build session guessing with your authority.
- **`full`** — the blast radius cannot be bounded by reading. Migrations or
  schema changes, a shared helper or contract with callers across modules, a
  dependency or framework bump, settings and auth surfaces many modules read, or
  a bug whose cause is still unknown. The build takes a green baseline before
  its first edit and runs the full suite after — you take neither.

Format:

    ### Verification

    - **Tier:** targeted
    - **Run:** the repo's test command for the `sites` module only
    - **Why:** the change is confined to one renderer service and the module
      that covers it — no migration, no shared helper, no contract another
      module reads.

**Push the same decision as data.** The block above is the human form and
stays. Step 8's `update_board_item` also carries it as `contract={"tier":
"targeted", "run": ["<the commands, verbatim>"], "evidence": ["tests"]}` — one
push, no extra ceremony. A build then reads the tier instead of parsing your
prose, and every later change to it is recorded on the item's trail, so a
downgrade cannot happen quietly.

- **`run`** — the commands, verbatim. Required at `targeted`, where they *are*
  the tier. At `none` and `full` they name riders that run anyway: the bug
  regression test below, or a `full` item's extra check.
- **`evidence`** — what proof this item owes: `tests` / `visual` / `manual` /
  `command`. Suggested vocabulary so items stay comparable, never enforced;
  empty is legal when the runs are the whole story.
- Unknown keys are refused. The contract is three fields until something
  actually reads a fourth — don't invent one.

Pick the **lowest** tier that would actually catch this change breaking. Doubt
between two tiers resolves upward, and the bound goes in the `Why:` line — a
tier with no stated bound is a guess wearing a label. A bug's regression test
runs at every tier, on its own, before and after the fix; that is targeted by
nature and costs seconds.

## Push Failures

Retry once. If it still fails, keep the scratch file and tell the user exactly
which item to sync manually (`planning/.cache/<item-id>.md` →
`update_board_item(<id>, description=...)`). Never lose the plan.

## Rules

- Do NOT implement. Planning and documentation only.
- Never explore, plan or push against an item the user has not confirmed — run
  the wrong-id gate above.
- The challenge exchange (step 6) travels WITH the plan — it is part of the
  workspec pushed to the board, so the build session and the user see what was
  objected to and how it was answered.
- Every summary — the step-7 presentation and any later recap — names the item
  as a markdown link, `[#<id> <title> (<stage>)](<url>)`, taking `url` from the
  tool result (`get_work_packet` and `get_board_item` both return it) rather
  than assembling a host. A
  bare id is not placeable by a user juggling parallel sessions, and in a repo
  with a GitHub remote a bare `#123` is auto-linkified to that repo's issue 123
  — a live link to the wrong system.
- Every endpoint designed must have fail-closed permissions.
- **Never assign `none` to a change that alters runtime behavior with nothing
  named in its place.** The tier decides how the build spends its time; an
  optimistic one spends it on nothing at all.
- Keep repo dumps out of the workspec — reference file paths.
- If the item is already `building` or has an owner, ask before re-scoping.
