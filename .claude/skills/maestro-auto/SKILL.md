---
name: maestro-auto
description: >-
  Scope and build one or more maestro board items in one mostly-autonomous run:
  parallel sub-agent scoping, a cross-item coherence pass, ONE consolidated
  approval gate, then sub-agent builds — parallel across worktrees where the
  repo isolates checkouts and the items are independent, sequential otherwise —
  merged back and verified by ONE closing test run. Confirms the roster up
  front, then interrupts the user once for approval instead of twice per item,
  and only for decisions that change the shape of the work.
user-invocable: true
argument-hint: <item-ids, e.g. "431" or "431 432 438" (omit to pick from the board)>
maestro-skill-version: 15
---

# Maestro Auto — Scope + Build, One Gate

`/maestro-scope` then `/maestro-build` over **one or more** items as a single
run, same engineering discipline: the user approves **once**, at a single
consolidated gate, instead of twice per item, and **one** closing run verifies
everything built. (The pre-flight roster confirm is not a third gate — seconds,
before any work starts.)

You are the orchestrator, not the builder:

- **Sub-agents touch code.** Scoping, red-teaming and building each run in a
  fresh-context sub-agent (Opus, high effort). They read, plan, edit, test and
  commit; they never write to the board.
- **You own the board and the batch.** Every `update_board_item`,
  `comment_on_item`, stage flip and claim goes through you, so a sub-agent that
  dies mid-task never leaves a half-updated board.

Use it whenever the user hands you already-filed items to work with minimal
supervision — **one item or a dozen**. Not for anything unfiled
(`/maestro-task`), not for work too small to track (`/maestro-vibe`).

## One Item or Many

**A run of one is a normal run, not a misuse of the skill.** N changes which
phases have anything to do; it never changes whether a phase applies.

At N=1, and nothing else differs:

- **Phase 3 has no siblings to cross-check.** Record "single item — no
  cross-item pass" and move on. The tier review inside it still happens: an
  optimistic bound from phase 1 is yours to escalate at any N.
- **Build order is the item.** Nothing to sequence, nothing to halt behind it.
- **The batch tier is that item's tier**, and the closing run is its named
  modules.
- **Both gates still run.** The roster confirm and the phase-4 gate are what
  make an unsupervised build safe; they cost seconds and one reply.

Choose `/maestro-scope` + `/maestro-build` instead when the user wants to read
the plan before it is pushed to the item, or to steer the build as it happens —
those skills also take one id or many (`/maestro-scope 431 432`,
`/maestro-build 431 432`) when only that half of the journey is wanted.
`/maestro-auto` is for approve-once, read-the-result, at any N.

## Board Resolution

Same as `maestro-task`: read Maestro's `.claude/maestro.json` repo config; on any miss, resolve via
`whoami()` / `list_workspaces()` / `list_boards()`, ask, offer to write the
file. Maestro unreachable or unauthenticated → stop with an explicit notice;
never fall back silently. Call `get_board(board, items=False)` once and keep the
column schema; match stage/priority options **by value**. It returns columns,
roster, board name and `item_url_template` without the rows (~98% of a busy
board's reply). An older server drops the argument silently and returns the whole
board: no error, just no saving.

Also from `maestro-task` — **step 5: name boards and items when you report them, never bare ids.** `board "Backlog" (Maestro workspace, id 8)`, `#586 "An agent cannot see what it deployed"`, with the item URL (format: "Naming an Item"); that lookup is far cheaper for you than for the user.

**Step 7: pass `client=` (the client you are running in) and `model=` (your model id) on every `create_board_item`, `update_board_item` and `comment_on_item` call.** The server cannot observe either — a write that omits them lands in the trail under the default label, "via Claude", whoever wrote it.

## Checkout lifecycle

The orchestrator—not any sub-agent—owns each checkout token. After the roster
confirmation, acquire `purpose="scope"` before scoping an inbox item; after the
approval gate, acquire/reacquire `purpose="build"` before its claim and
snapshot. Planned build-only items acquire build immediately before phase 5.
Use a fresh high-entropy idempotency key per acquisition generation.

A checkout is advisory and its token is used only for renew/check-in. If a
scope acquire conflicts before the consolidated approval gate, include the
holder, purpose, expiry, and a wait-versus-continue recommendation in that one
gate; do not add an earlier interruption. A build acquire conflict requires a
human wait-versus-continue decision before that build, even when the batch gate
was already approved. Continuing without a checkout is allowed; never pass a
checkout token to ordinary board writes.

After every acquire, re-fetch the authoritative work packet and latest comments
before starting. A material title/stage/description/contract change requires
reconfirmation. Every post-acquire sub-agent uses fresh isolated context and
receives the workspec but never the raw token or owning transcript. Tokens are
bearer capabilities visible in the owning MCP transcript by design; never put
them in domain data, logs, URLs, realtime, browser storage, messages, or
sub-agent context.

Renew by 40 minutes, immediately before and after long waits/commands, and
before accepting a sub-agent result. Keep waits short enough to heartbeat.
Lost/expired/replaced ownership means report that the advisory signal is gone
and do not pretend it remains held; it does not gate a claim or push. Check in
each scope lease after its plan push and
each build lease after its final stage write or blocker comment, plus every
failure, cancellation, or dropped-item terminal path. After an outage, reread
before replaying queued board writes.

## Pre-Flight — admit the batch

1. **Resolve the items.** Ids from the arguments; with none, call
   `workspace_queue(workspace, stage=["inbox", "planned"])` — it spans **every**
   board in the workspace, where `get_board(board)` sees only the board
   `.claude/maestro.json` names and reports a short queue, no error, wherever a
   team routes work across two. List candidates with **their board name** (id,
   title, board, stage, priority) and ask which to run. Never pick silently.
   Read the reply's `boards` sidecar first: a board whose `unmatched_stages`
   names a requested stage does not carry it at all — re-ask that board with a
   value from its own `stage_values` rather than reporting it empty. If
   `workspace_queue` is unavailable (older server), fall back to
   `get_board(board)` and say so.
2. `get_work_packet(item)` for each — one call per item covers the roster card
   below, the classification here, and the rules each item names. (Older server
   without the tool: `get_board_item(item)` plus one `list_boards(<workspace>)`
   for the names, and say you fell back.) Classify:
   - `inbox`, no `## Plan` → **scope then build**
   - `planned`, has `## Plan` → **build only** (skip phases 1-2)
   - `stage.is_parked` → **reject**, always. Parking is a deliberate "not now"
     and its plan is presumed stale; it is resumed and re-scoped first.
   - already `building`, or owned by someone else (`people` names them) → ask
     before including it.
   - `resumed_from_parked` carrying a `warning` → route it to **scope then
     build** even at `planned`: the plan predates the resume.
3. **Cap the batch.** One item is a complete run — never pad it out or send the
   user away to another skill. At the other end, more than about six items in
   one run is an unreviewable diff, not autonomy: propose a first batch of the
   highest-priority items and say what you left for the next run.
4. Each packet already carries the rules its own workspec names (`rules.applied`,
   with unresolved slugs in `rules.missing` — say those out loud). Call
   `get_workspace_context(workspace)` once only for what the packets do not
   carry: workspace rules no item references, or the `challenge` doc the scoping
   sub-agents need.
5. **Admit the batch** — run the wrong-id gate below and get a yes, before any
   agent spawns. Not the phase-4 gate: this asks "are these the right items?",
   that asks "is this the right plan?".
6. **No baseline yet** — the tier that decides whether the run needs one arrives
   with the plans (phase 1), and scoping is read-only. See "Verification".

## Naming an Item

Name an item to the user — start notice, approval gate, final report — as a
markdown link, never a bare id:

```
[#<id> <title> (<stage>)](<url>)
```

`url` comes from the tool result: `get_work_packet` and `get_board_item` return
one; `get_board` and every board in `workspace_queue`'s `boards` sidecar return an
`item_url_template` with `{id}` to substitute (rows carry no url). Never
hand-assemble a host. In a repo with a GitHub remote the client auto-linkifies a
bare `#123` to that repo's issue 123 — a live link to the wrong system.

## Confirm the Batch — the wrong-id gate

**An item id is not self-describing**, and several multiply the mistake: ids
get transposed, copied from the wrong session, or read off a stale list. Phase 4
pushes plans to items **before** approval, so one bad id rewrites a workspec you
never meant to touch and that gate is too late. Confirm the roster before any
agent spawns — a run of one gets the same card, since there one wrong id is the
entire run.

Required whenever the ids came from arguments, notes, or another session. Skip
only if the user just picked them off a list you presented this session, or
waives it outright.

One compact block per item, built straight from the phase-1 `get_work_packet`
results — `board_name`, `workspace_name`, `filed.by`/`filed.age`, `parent_title`
and the stage are all already in hand, so the roster costs no calls of its own:

    #586 "An agent cannot see what it deployed — add preview_site"
    board "Backlog" · workspace "Maestro" · inbox · must · filed by Ian
    Starnes today · under #516 "Sites + domains — release hardening (epic)"
    → scope then build. Agents ship sites they never see; wants an MCP
      render tool so an agent can look at what it deployed.

    #583 "Sites design guidance — creative-director skill"
    board "Backlog" · workspace "Maestro" · planned · should · filed by Ian
    Starnes 3 weeks ago
    → build only (already scoped). Adds a skill that raises the odds the
      first draft of a site is any good.

    Run these 2 — 1 to scope, 1 straight to build, one suite run at the end?

With a single item the card is unchanged and the ask is `Run this one — scope
then build, one suite run at the end?`. (No size on this card — nothing is
scoped yet; size belongs at the phase-4 gate, where the plans exist.)

Every item gets: title; board + workspace by NAME; stage; who filed it and when
in plain words ("today", "3 weeks ago"); its parent, if any; the route you
classified it into (scope-then-build / build-only); and one line of what it is
**in the requester's terms**.

**Say these out loud before asking, whenever they are true:**

- An item's `board` is a **sibling board in the same workspace** — normal under a
  deliberate split; confirm it is the board you meant, since ids interleave
  across boards. A different `workspace` is almost always a wrong id — another
  repo's board.
- A `planned` item's plan is weeks old (`spec_updated.age`) — possibly stale,
  and the batch will build it without re-scoping. Same for any
  `resumed_from_parked` warning, which says so outright.
- An item is owned by someone else or already `building` (pre-flight step 2), or
  `whoami()` is not the filer.
- Two ids in the batch are one digit apart — say so explicitly; that is the
  transposition this gate exists to catch.

Wait for a clear answer; an ambiguous one is not a yes. If the user drops or
corrects an item, restate the final roster before starting — a batch that
silently changes shape builds the wrong item anyway.

## Phase 1 — Parallel Scoping

Spawn one sub-agent **per item, all in one message** so they run concurrently;
scoping is read-only, so it parallelizes cleanly.

Brief: the `maestro-scope` workflow for exactly one item — read every file the
workspec references, check existing patterns and helpers in the target app, fetch
framework docs when framework features are involved — and **return the complete
updated description as its result**: the human block, the `## Spec`, and the
`## Plan` it just wrote (objective, ordered steps with file paths, design
decisions with rationale, edge cases, testing plan, documentation plan), in
`maestro-scope`'s plan style — each fact once, conclusions not investigation.

**The whole description, not a plan fragment** — you push what the agent returns
and it is the only actor that read the item. So the agent also converts a
legacy-shape workspec (`# H1` + `## Description` / `## Context`) to the two-tier
shape, and refreshes the human block when its premise check moved the story.

Also require, returned separately:

- **Files it intends to create or modify** — the input to phase 3.
- **Verification tier** — `none` / `targeted` / `full`, with the named run
  commands and the one-line bound, exactly as `maestro-scope` defines it. This
  sizes the closing run; "run the tests" is not an answer. Returned in contract
  shape — `{"tier", "run", "evidence"}` — so phase 4 can push it as data
  alongside the prose.
- **Schema/migration impact** — models and tables touched, or "none".
- **Contract changes** — endpoints, signatures, or serialization shapes whose
  behavior changes for existing callers.
- **Premise check** — anything in the workspec that turned out false, already
  shipped, or impossible as written.

Give each agent the workspec, the workspace rule docs, and read access to the
repo. Forbid edits: phase 1 writes nothing but its own answer.

## Phase 2 — Challenge

Per plan, spawn one fresh-context red-team agent (in parallel) whose input is the
workspec, the draft plan, and the workspace `challenge` skill doc — or, lacking
it, its core rules: name untested assumptions, argue the strongest opposing case,
never invent a flaw to perform thoroughness. Its brief is to **refute the plan**.

Verify each objection yourself — you have repo access, the challenger may not run
code — and give every one a disposition in a `### Challenge` subsection of that
item's plan, as a table (`| # | Objection | Disposition | Evidence / what changed |`),
one row each: `amended` (say what changed) or `rebutted` (with evidence, not
"considered"). "No substantive challenge" is a valid verdict, recorded as that
one line. A disposition that changes what an item *is* also changes its human
block — fold that in before phase 4 pushes it.

**An objection you cannot dispose of is a gate item, not a footnote** — it goes
to the user in phase 4 as an open question.

## Phase 3 — Batch Coherence

**One item in the run** → nothing to cross-check. Record "single item — no
cross-item pass", re-read its tier against its own plan (the bullet on tiers
below still applies to a diff that grew past its bound), and go to phase 4.

Otherwise: each scoping agent saw one item; you see all of them. Cross-check the
plans for:

- **File collisions** — two plans editing the same file. Usually fine; flag it
  when they change the *same function or contract* in incompatible directions.
- **Migration collisions** — two items migrating the same model or table. Order
  these explicitly, never interleaved.
- **Dependency order** — item B builds on a file or helper item A creates.
  Produce an explicit build order and state the reasoning.
- **Redundant work** — two plans adding the same helper. Pick one owner and amend
  the other plan to consume it.
- **Obsolete items** — an item another plan (or already-shipped work) makes
  unnecessary. Propose dropping it rather than building it.
- **Tiers the batch invalidates.** Each agent judged its item's blast radius
  alone. Two plans editing the same file, or a helper one item creates and
  another consumes, is a bound that held per-item and does not hold for the batch
  — **escalate both items' tiers** and say so. This is the one verification
  judgement only the orchestrator can make, and it rewrites both items'
  contracts, not only the prose — phase 4 pushes the raised tier as data too.

Amend the affected plans and record what changed and why. The build order you
produce here is the order phase 5 runs in.

## Phase 4 — The One Gate

Push each finished plan to its item first, so the record exists before any
approval: `update_board_item(item, description=<the agent's complete updated
description>, values={"stage": "planned"}, contract=<that item's verification
contract, as phase 3 left it>)`, then `comment_on_item(item, <3-5 line plan
summary>)`. One call per item carries the plan, the stage and the tier — the
contract is what phase 5's builds and the closing run read back. Push failures
retry once, then keep the plan locally and report the exact manual sync call —
never lose a plan.

**Where an agent superseded a premise, archive the original first.** If the item
no longer says what it said — the bug was not a bug, the cause was something else
— `comment_on_item(item, <the original description, verbatim>)` **before** the
update: the push replaces the description whole and the trail's "Description
updated" note carries no old text, so that comment is the only durable copy.

Then present **one** consolidated brief and stop for approval — the user's single
decision point, so complete and short:

- **The batch** — one line per item: markdown link, plan in a sentence, size
  (commits / tests / docs), verification tier.
- **Build order** — the sequence, and any dependency that forced it.
- **How the batch gets verified** — the batch tier, what the closing run will be,
  whether a baseline is coming, and any tier phase 3 escalated with why. The user
  can relax a tier here; it is the only moment to.
- **What scoping changed** — only findings that change the shape of the work: a
  false workspec premise, a bug found on the way, an item now obsolete.
- **Cross-item issues** — collisions, shared helpers, migration ordering.
- **Open questions** — every undisposed challenge objection and every decision
  you judged the user's to make, each with your recommendation.
- **What you decided without asking** — a compact list, so a wrong call is
  catchable here rather than in the diff.

Roughly 20 lines for the whole batch, not per item. Then wait. Do not build until
the user approves; on a partial approval, run that part and say what you dropped.

## Phase 5 — Builds

**Sequential is the default**, in the phase-3 order. In a repo whose suite
targets a fixed port and a shared database, and whose work commits to one branch
in one working tree, concurrent builds collide on the port, corrupt the database
and interleave commits.

**Build in parallel only when both hold**, and say which:

1. **The repo isolates a checkout** — each worktree gets its own test database,
   port and cache namespace. Verify it against the repo's own git/testing rules;
   do not assume, and do not infer it from the presence of `git worktree`.
2. **The items are independent on disk** — no shared files, no migrations to the
   same app (two trees generate the same `000N_` and clash at merge), and
   neither consumes what the other creates.

Either one missing → sequential for those items. A batch commonly splits: three
independent items in parallel, two more behind them in order. **State the
partition and what forced every sequential edge** before starting.

Cap concurrency at what the repo's isolation actually supports — checkout slots
are usually a bounded machine-wide resource, and leaked ones count against it.
Past ~3 at once, the merge is where you spend what you saved.

**First, if the batch tier is `full`**, take the green baseline now: once, for the
whole run, before the first claim. Below `full` there is no baseline (see
"Verification").

Then, for each item — concurrently within a parallel group, otherwise in order:

1. **Claim it** yourself: `update_board_item(item, values={"stage":
   "building", "owner": [<the user's id from whoami()>]})`.
2. **Snapshot** the approved description to `planning/built/<item-id>.md`
   (create the directory if absent), first line: `<!-- generated from maestro
   item <id> — do not edit; the board item is the source of truth -->`. Commit
   it as the build-start marker.
3. **Spawn one build sub-agent** (Opus, high effort) with the approved plan, the
   repo's conventions, and its batch context (build order, what earlier items in
   this run landed, any shared helper another item introduced). Brief: the
   `maestro-build` workflow for one item — read before editing, one logical unit
   at a time with tests written alongside the change, commit per the repo's git
   conventions, update docs and changelog. Returns commits (hash + one line), the
   tests it wrote, deviations from the plan, anything left open. **It does not
   touch the board and does not run the full suite** (see below). In a parallel
   group, give each agent its own worktree and branch, set up the way the repo's
   rules say — a bare `git worktree add` skips steps (dependency install,
   gitignored config) that make tests work there.
4. **Post the commit trail yourself**: `comment_on_item(item, ...)` with the
   commits and any deviation from the plan. Leave the item at `building` — built,
   not verified.
5. **Check the tree before moving on**: working tree clean, commits present,
   nothing staged from the next item. A build agent's report is a claim; the
   suite settles it, once, at the end.

**After a parallel group, before anything else**: merge each branch back into the
primary tree **one at a time**, resolving as you go, then remove every worktree
and branch you created and reclaim any checkout slot the repo allocates. Only
then continue. Nothing has tested the combined tree yet — that is what the
closing run is for, and it cannot run until the branches are in.

## Verification — One Closing Run, Sized to the Batch

The batch runs tests **twice at most**, often once — sized by the tiers phase 1
returned and phase 3 amended. The closing run happens in the **primary tree,
after every branch has merged**: a green suite inside one worktree says that
item works alone, which is not the claim being made. Runs never overlap in a
repo that serializes tests; where worktrees isolate, a build agent may still run
a focused target in its own tree without colliding.

**The batch's tier is the highest tier any item in it carries.**

- **Any item at `full`** → the repo's green baseline **before phase 5** (not in
  pre-flight — the tier isn't known until scoping is done), then the full suite
  once after the last item lands. Red baseline → stop and tell the user; never
  build a batch on red.
- **Otherwise (`targeted` / `none` only)** → **no baseline at all.** The closing
  run is the union of every item's named modules, once, after the last item
  lands. The union is sound because each item's bound covers the files it
  touched, and the case where it wouldn't — a shared helper, a cross-item
  collision — is what phase 3 escalates to `full`.
- **Every item at `none`** → no closing run either. Report per item what stood in
  for the tests, and say plainly that no suite ran.

Then:

- Build agents run **no** suite of their own — they write tests as part of the
  change; verification is deferred to the closing run.
- One exception: a bug's regression test proves nothing unless it is seen to
  fail, so the agent runs **that test alone** before the fix and after. Seconds,
  at any tier.
- **Green** → flip every built item in one pass: `review` if it went to a PR,
  `done` if it went straight to the main branch. First moment any item may be
  called done.
- **Red** → attribute each failure to the item owning the touched files (the
  per-item commits make this unambiguous), fix in place, re-run. One sweep per
  fix round, never per item. Fixed items flip normally; an item you cannot get
  green stays at `building` with a blocker comment naming the failing tests. With
  no baseline, attribute the way `maestro-build` does — read the failure, and
  stash-and-rerun that one test if reading doesn't settle it.
- Report the real numbers, and **which tier produced them** (see "Final Report"
  for what a run must never omit).

Nothing is `done` mid-run: until the sweep runs, the truth is "built".

## Failure Policy

- **A failed item does not fail the batch.** Post a blocker comment, leave it at
  `building` with its owner intact, continue with the remaining **independent**
  items.
- **Halt items that depended on the failure**, and say which, rather than building
  on a missing foundation.
- **Halt the whole run** on: a red baseline, a failure that invalidates a later
  item's approved plan, or a needed change that goes beyond what the user
  approved at the gate.
- A build agent that fails outright (crash, unusable result) is retried once
  before the item is blocked — its item is the only one affected.
- Mid-run maestro outage: never block a build on it. Finish locally, retry each
  push once, then queue the pending stage flips and comments in the final report
  as exact tool calls to replay.

## Autonomy Contract

Decide these yourself, log it at the gate or in the final report, move on: naming,
file placement, test organization, which existing helper to reuse, step order
within an item, doc wording — anything the repo's conventions, the workspec, or
the code already answer.

Raise it — at the gate if you know before building, immediately if you find it
mid-run:

- Two plans changing the same contract in incompatible directions
- Two items migrating the same table or model
- A workspec premise that turned out false, or an item now obsolete
- Fail-closed permission design, or any new auth surface
- An item materially bigger than filed (a multi-session build)
- A red-team objection you could not dispose of
- Any deviation from the approved plan that changes what the user agreed to

Never stop to ask a question the repo, its rules, or the workspec answers. And
never suppress a question because the run was supposed to be autonomous — an
unasked question becomes a wrong diff.

## Final Report

TL;DR-first, once, for the batch. Every item a markdown link (see "Naming an
Item").

- **What shipped** — one line per item: title, the original ask in the
  requester's terms, where it landed.
- **What deviated** — per item, what was done differently than the approved plan
  and why. Never let a deviation surface only in a diff.
- **Verification** — the batch tier, what the closing run actually was, its
  result (against the baseline if one was taken), how many fix rounds it took,
  and any tier escalation phase 3 or a build forced. Failures, skips and anything
  that could not run go here, never omitted.
- **Not done** — blocked, halted, or dropped items, with what each needs.
- **Queued board updates** — any push that never landed, as replayable calls.

Roughly 20 lines. Detail lives in the commits and each item's activity trail —
point at them.

## Forbidden

- Spawning a scoping agent against a roster the user has not confirmed — phase 4
  pushes plans to items before approval
- Building any item the user did not approve at the gate
- Building an item with no `## Plan`, a `parked` item, or one owned by someone
  else without asking
- Concurrent builds in a repo with a shared test port, database, or working tree
- Letting sub-agents write to the board, or reporting a sub-agent's claimed test
  result as verified fact without checking it
- Flipping any item to `review`/`done` before the batch's closing run
- Trading the closing run away for speed, or reporting a batch as verified at a
  tier below the one approved at the gate
- Building a `full`-tier batch without a baseline, or taking a baseline for a
  batch that has no `full` item in it
- Skipping the challenge phase, the coherence pass, or the gate to "save time"
- Leaving any item's stage stale when the run ends
