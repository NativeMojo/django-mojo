---
name: maestro-auto
description: >-
  Scope and build one or more maestro board items in one mostly-autonomous run:
  parallel sub-agent scoping, a cross-item coherence pass, ONE consolidated
  authority assessment, then sub-agent builds — parallel across worktrees where the
  repo isolates checkouts and the items are independent, sequential otherwise —
  merged back with proportionate closing verification and valid evidence reuse.
  Validates the roster automatically and asks only for real missing decisions.
user-invocable: true
argument-hint: <item-ids, e.g. "431" or "431 432 438" (omit to pick from the board)>
maestro-skill-version: 36
---

# Maestro Auto — Scope + Build Within Existing Authority

Scope and build one or more items in one run. A clear request authorizes its
ordinary steps; validate scope and current rules, then proceed. Consolidate real
missing decisions instead of scheduling mandatory confirmation gates. Verify the
combined result proportionately and reuse valid unchanged evidence.

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

## Existing authority and routine decisions

A clear request authorizes its ordinary implementation steps within the caller's
actual permissions. Validate task identity, workspace, scope and current rules,
then proceed without repeat confirmation. Ask only for material ambiguity or
changes outside existing authority, an explicit wait, or a missing decision
required by an enabled workspace rule. Silence is not new authority.

Coordinate ownership and checkout conflicts with the holder, wait, or work on
independent authorized tasks. Never seize an active checkout, pretend to hold a
lease, or bypass an actual refusal. Resume parked/rejected work only with actual
resumption authority; a clear user instruction can provide it. Read the prior
reason and current scope first; an item ID alone does not cancel a hold.

Choose routine sequencing and proportionate verification, documenting why.
Reuse valid evidence bound to the source, commands, counts and covered behavior;
rerun affected checks after a relevant change, failure or uncertainty. Preserve
explicitly required checks and selected independent review. Never present a
claim, stale output or self-review as independent proof. Routine handoffs are
coordination, not extra human approval gates. Explicit waits, release holds,
release authority and native-host boundaries remain binding.

## Workspace planning rules

Read `approval_policy` from workspace context. `require_human_approval`,
`require_approach`, `require_challenge`, `require_verification_contract` and
`require_lead_review` are all **off by default**. Treat the planning steps below
as guidance unless the corresponding rule is explicitly enabled. Use agent
judgment on how much planning, challenge and review the work needs. A Lead's
presence does not enable any rule. Do not stop, demand a human answer or ask for
Lead review solely because a recommended artifact is absent. Honour explicit
workspace requirements and ask about material changes beyond existing authority.
Only change these rules on a workspace configuration request from its owner/admin.


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
- **Validate scope once.** Identity and phase-4 scope assessment are what
  make an unsupervised build safe; they do not require another reply when authority is already clear.

Choose `/maestro-scope` + `/maestro-build` instead when the user wants to read
the plan before it is pushed to the item, or to steer the build as it happens —
those skills also take one id or many (`/maestro-scope 431 432`,
`/maestro-build 431 432`) when only that half of the journey is wanted.
`/maestro-auto` executes an authorized batch, at any N.

**Ids that are the roster of an approved Plan belong to `/maestro-run`** — it
takes the Plan's own id, runs its waves in the order the board holds, and gates
on the current roster and actual decision authority, without a redundant yes. `/maestro-auto` is for **flat filed items with no Plan**: it
classifies them itself, derives its own build order, and asks only for real
missing decisions.

## Board Resolution

Same as `maestro-task`: a session-stated workspace in the first message comes
first, else read Maestro's `.claude/maestro.json` repo config; on any miss, resolve via
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
validation, acquire `purpose="scope"` before scoping an inbox item; after scope assessment, acquire/reacquire `purpose="build"` before its claim and
snapshot. Planned build-only items acquire build immediately before phase 5.
Use a fresh high-entropy idempotency key per acquisition generation.

A checkout conflict is coordination, not an automatic human decision. Read the
holder, purpose and expiry; coordinate with the holder, wait or continue other
independent authorized work. Never seize an active checkout or claim a lease you
do not hold. Proceed without an advisory lease only when non-conflicting work is
actually authorized; an actual permission/approval refusal is not a lease conflict.
Never bypass it or fabricate a Lead review. Resolve selected requirements through
the normal allowed route. Keep the token only in the orchestrating session.

After every acquire, re-fetch the authoritative work packet and latest comments
before starting. A material title/stage/description/contract change requires reassessment; ask only if the change exceeds existing authority. Every post-acquire sub-agent uses fresh isolated context and
receives the workspec but never the raw token or owning transcript. Tokens are
bearer capabilities visible in the owning MCP transcript by design; never put
them in domain data, logs, URLs, realtime, browser storage, messages, or
sub-agent context.

Renew by 40 minutes, immediately before and after long waits/commands, and
before accepting a sub-agent result. Keep waits short enough to heartbeat.
Lost/expired/replaced ownership means report that the advisory signal is gone
and do not pretend it remains held; it does not gate a claim or push.

**The orchestrator checks in (progress) on behalf of each running build.**
Sub-agents never hold tokens, so the renewal you already make per lease
carries the report: `activity` says what that item's build is doing right now
("sub-agent building", "merging branch", "closing test run"), and on a build
lease `criteria` + `criteria_count` tick the item's `### Acceptance Criteria`
ordinals as the sub-agent's results come back. Check in at natural boundaries
— dispatch, return, merge, blocker — still no later than 40 minutes apart; it
writes no trail note. After any reacquire the report starts empty, so re-post
the full criteria state on the first check-in of the new generation.

Check in (return)
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
   title, board, stage, priority) and choose within an explicit queue remit and report why; otherwise ask for the missing selection.
   Read the reply's `boards` sidecar first: a board whose `unmatched_stages`
   names a requested stage does not carry it at all — re-ask that board with a
   value from its own `stage_values` rather than reporting it empty. If
   `workspace_queue` is unavailable (older server), fall back to
   `get_board(board)` and say so.
2. `get_work_packet(item)` for each — one call per item covers the roster card
   below, the classification here, and the rules each item names. (Older server
   without the tool: `get_board_item(item)` plus one `list_boards(<workspace>)`
   for the names, and say you fell back.) Classify:
   - `inbox`, no `## Approach` (nor legacy `## Plan`) → **scope then build**
   - `planned`, has `## Approach` (or legacy `## Plan`) → **build only**
     (skip phases 1-2)
   - `stage.is_parked` or rejected → preserve the prior decision unless actual
     resumption authority covers it. Then resume through the normal writer,
     inspect the old approach and re-scope what is stale before dependent work.
   - already `building`, or owned by someone else (`people` names them) →
     coordinate with the holder. Wait or work on independent authorized items;
     do not seize a lease or manufacture a human decision from an owner label.
   - `resumed_from_parked` carrying a `warning` → assess approach freshness;
     route to scoping when the current work actually needs it.
3. **Cap the batch.** One item is a complete run — never pad it out or send the
   user away to another skill. At the other end, more than about six items in
   one run is an unreviewable diff, not autonomy: propose a first batch of the
   highest-priority items and say what you left for the next run.
4. Read each packet's unaddressed feedback and handle it within authority.
   Ordinary comments do not block work. Explicit waits remain binding; material
   changes need a decision. Use addresses when acknowledging actual handling.
   Require directive resolution before Done only when require_addressed_directives
   is on. Never claim unhandled feedback is complete.
5. Each packet already carries the rules its own workspec names (`rules.applied`,
   with unresolved slugs in `rules.missing` — say those out loud). Call
   `get_workspace_context(workspace)` once only for what the packets do not
   carry: workspace rules no item references, or the `challenge` doc the scoping
   sub-agents need.
6. **Validate the batch** — resolve identity and scope below before any
   agent spawns. Phase 4 assesses the resulting approaches and actual scope
   changes; neither step requires a reply when authority is clear.
7. **No baseline, at any tier** — a red closing run is judged against what is
   already recorded (see "Verification").

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

## Validate the Batch — identity check

Read `get_work_packet(item)` for each intended item before mutation. Match the title, board/workspace,
parent, stage, owner, current scope and verification contract to the request.
Name the linked task and briefly state the intended action; do not ask “Build
this one?” or “Scope this one?” when the request already supplies that authority.
A real identity mismatch or consequential ambiguity needs clarification; never
silently substitute another task. A sibling board or an old workspec calls for
judgment and a freshness check, not an automatic confirmation.

Parked/rejected work retains its prior decision. Resume only when the user or
existing remit actually authorizes resumption, using the normal validated
writers and recording why. Do not invent a browser-only reopening requirement.
An active owner's work or checkout is coordinated, not taken over unilaterally.

## Phase 1 — Parallel Scoping

Spawn one sub-agent **per item, all in one message** so they run concurrently;
scoping is read-only, so it parallelizes cleanly.

Brief: the `maestro-scope` workflow for exactly one item — read every file the
workspec references, check existing patterns and helpers in the target app, fetch
framework docs when framework features are involved — and **return the complete
updated description as its result**: the human block, the `## Spec`, and the
`## Approach` it just wrote (objective, ordered steps with file paths, design
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
- **Durable findings** — decisions with a rejected alternative, conventions
  the code assumes, facts measured or discovered — as `{kind, slug, title,
  content}` rows. You write them at the phase-4 push (see phase 5 step 4 for
  the rule); a sub-agent holds no knowledge tool.

Give each agent the workspec, the workspace rule docs, the packet's
`knowledge.retrieved` entries (the workspace's memory nearest this item —
applied beside the rules, and the agent says when one changes the approach;
`knowledge.reason` says why the set is empty), and read access to the repo.
Forbid edits: phase 1 writes nothing but its own answer.

**Project stamp is INHERIT-ONLY in this run.** An unattended run stamps
what the parent already carries — the parent's Project column value goes
into the item's phase-4 push `values` — and NOTHING else: never
`manage_project(create)`, never ask mid-run, never derive one from a due
date (`maestro-scope`'s reuse-before-create rule is for interactive
sessions; unattended creation is both a silent write the human never asked
for and a duplicate-create race between parallel sub-agents scoping
siblings of one dated Plan). Items with no parent signal stay unstamped;
the phase-4 assessment lists each item's project (or "no project") so the
report the unset project; ask only if a real decision is needed.

**Horizon stamp is inherit-or-default-`next`, never a question.** On a
horizon-purpose board an item keeps the horizon value it has; one with none
is stamped `next` at the phase-4 push (only when the column defines it).
Never ask mid-run and never stamp `now` unattended — urgency is the
owner's call unless urgency authority was explicitly delegated.

## Phase 2 — Challenge

Size challenge to risk and selected workspace rules. An inline assessment may
suffice by default; an enabled requirement needs the actual required evidence.
The detailed challenger procedure below applies when that path is selected.

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

Assess unresolved objections against actual risk and authority. Resolve routine details; ask for a material decision when needed, not for every objection.

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
  alone. A helper or contract one item creates and another consumes is a bound
  that held per item and does not hold for the batch — **escalate both items to
  `full`** and say so. Two plans editing one file keep their tiers; the closing
  union runs both items' modules against the combined tree. This is the one
  verification judgement only the orchestrator can make, and it rewrites both
  items' contracts, not only the prose — phase 4 pushes the raised tier as data
  too.

Amend the affected plans and record what changed and why. The build order you
produce here is the order phase 5 runs in.

## Phase 4 — Validate Scope and Proceed

Push each finished plan to its item first, so the record exists before any
approval: `update_board_item(item, description=<the agent's complete updated
description>, solution=<the solution in plain words>, values={"stage":
"planned"}, contract=<that item's verification contract, as phase 3 left
it>)`, then `comment_on_item(item, <3-5 line plan summary>)`. `solution` is
what the person reads at the gate: two to four plain sentences saying what
will change, with no file names, code or commands. Human-facing approval asks
require Problem, Desired outcome and this solution; routine stages do not. One call per item carries the plan, the stage and the tier — the
contract is what phase 5's builds and the closing run read back. Push failures
retry once, then keep the plan locally and report the exact manual sync call —
never lose a plan.

**Where an agent superseded a premise, archive the original first.** If the item
no longer says what it said — the bug was not a bug, the cause was something else
— `comment_on_item(item, <the original description, verbatim>)` **before** the
update: the push replaces the description whole and the trail's "Description
updated" note carries no old text, so that comment is the only durable copy.

Present a concise consolidated scope summary: linked tasks and outcomes, build
order, verification and reused evidence, changed premises, conflicts and actual
missing decisions. Resolve routine choices and record why. Proceed within the
existing request/standing authority; stop only dependent work for a material
change, explicit wait or a missing decision required by enabled workspace policy.
Do not ask for a phase-4 yes merely because scoping finished. A partial approval
limits scope to the authorized part; silence grants nothing new.

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

For each item — concurrently within a parallel group, otherwise in order:

1. **Claim it** yourself: `update_board_item(item, values={"stage":
   "building", "owner": [<the ids already on Owner>, <the user's id from whoami()>]})`.
   Append to Owner, never replace it: the person already there decides the
   plan. Add your user id from
   `whoami()` to Owner only when `whoami()` says `is_runner: false`. A runner's
   agent is already on Owner from the hand-out, so its claim is the stage alone.
2. Cache the approved description at `planning/.cache/<item-id>.md` for the
   session. `planning/` is ignored; never commit generated workspec snapshots.
   The board item and its activity trail are the durable record.
3. **Spawn one build sub-agent** (Opus, high effort) with the approved plan, the
   repo's conventions, the packet's `knowledge.retrieved` entries (read beside
   the rules), and its batch context (build order, what earlier items in
   this run landed, any shared helper another item introduced). Brief: the
   `maestro-build` workflow for one item — read before editing, one logical unit
   at a time with tests written alongside the change, commit per the repo's git
   conventions, update docs and changelog. Returns commits (hash + one line), the
   tests it wrote, deviations from the plan, anything left open, and its
   durable findings as `{kind, slug, title, content}` rows. **It does not
   touch the board and does not run the default suite** (see below). In a parallel
   group, give each agent its own worktree and branch, set up the way the repo's
   rules say — a bare `git worktree add` skips steps (dependency install,
   gitignored config) that make tests work there. An agent teammate running
   this has no parallel group: its sub-agents build one at a time in its own
   one worktree, and parallel work goes to teammates.
4. **Post the commit trail yourself**: `comment_on_item(item, ...)` with the
   commits and any deviation from the plan. Leave the item at `building` — built,
   not verified. **Then record what the item taught** — the agent's durable
   findings and the deviations you accepted with a reason that binds the next
   build: `upsert_workspace_doc(workspace, kind, slug, title, content)` with kind `decision` / `convention` / `fact`, one entry per fact, a stable slug, and a body a stranger can act on. Read the reply's `similar` before the next write — a near match means update that slug, not add a twin. Not recorded: transcripts, file dumps, tool logs, anything already in the repo's own docs.
5. **Check the tree before moving on**: working tree clean, commits present,
   nothing staged from the next item. A build agent's report is a claim; the
   suite settles it, once, at the end.

**After a parallel group, before anything else**: merge each branch back into the
primary tree **one at a time**, resolving as you go, then remove every worktree
and branch you created and reclaim any checkout slot the repo allocates. Only
then continue. Nothing has tested the combined tree yet — that is what the
closing run is for, and it cannot run until the branches are in.

## Verification — One Closing Run, Sized to the Batch

The batch runs **one** closing test run — sized by the tiers phase 1 returned
and phase 3 amended. The closing run happens in the **primary tree, after every
branch has merged**: a green suite inside one worktree says that item works
alone, which is not the claim being made. On the integration branch that run is
the branch's recorded run; anywhere else use the runner's opt-out from reporting
(testit: `--no-maestro`). Runs never overlap in a repo that serializes tests;
where worktrees isolate, a build agent may still run a focused target in its own
tree without colliding.

**The batch's tier is the highest tier any item in it carries.**

- **Any item at `full`** → the repo's default suite once, after the last item
  lands — never a baseline. The tier never implies the runner's `--full`
  slow/extended option.
- **Otherwise (`targeted` / `none` only)** → the closing run is the union of every item's named modules, once, after the last item
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
- **Green** → flip every built item in one pass: `review` if it was pushed or
  went to a PR, `done` if it was merged to the integration branch or went
  straight to the main branch. First moment any item may be called done.
  **Building**: an engineer is writing it. **Review**: the code is pushed, and agents review, test and merge it, with no person. **Done**: reviewed, passing and merged, or finished with its outcome recorded and no change. A person is asked only when a task is marked for a person's check, and then once.
- **Red** → judge it under `maestro-build`'s "Judging a Red Run" (the recorded
  integration-branch run, the workspace's `known-flaky-tests` fact, then a
  single-file rerun). If only already-red or known-flaky isolated failures
  remain and the changes' required checks pass, items may go to **review**,
  never done under this exception. Report each failing test and what it
  matched; do not call the suite green or fully passing. For new failures,
  attribute each to the item owning the touched files (the per-item commits make
  this unambiguous), fix in place, re-run. One sweep per fix round, never per
  item. Fixed items flip normally; an item you cannot get green stays at
  `building` with a blocker comment naming the failing tests.
- Report the real numbers, and **which tier produced them** (see "Final Report"
  for what a run must never omit).

Nothing is `done` mid-run: until the sweep runs, the truth is "built".

## Failure Policy

- **A failed item does not fail the batch.** Post a blocker comment, leave it at
  `building` with its owner intact, continue with the remaining **independent**
  items.
- **Halt items that depended on the failure**, and say which, rather than building
  on a missing foundation.
- **Halt the whole run** only when a verification blocker or invalidated plan
  affects every remaining item, or the next step requires a scope/product
  decision beyond the user's authorization. Isolated already-red or known-flaky
  failures follow `maestro-build`'s "Judging a Red Run" and do not halt the
  batch.
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

Resolve ordinary design, shared-file/migration sequencing, appropriate checks
and objections within authority, recording the reason. Ask only when the actual
resolution materially changes the authorized outcome, costs or risk, needs a
reserved workspace decision, or conflicts with an explicit wait. A category
such as migration, auth or a false premise does not alone require a human.
Never conceal unresolved material uncertainty or fabricate evidence.

## Approval authority and agent judgment

The workspace rule **Require human approval** is **off by default**.
`approve_board_item_plan` and `request_board_item_rescope` are available to
human and agent full members. Use the authority and intent already given:
reason about the work and record your own decision with client/model
attribution. Do not ask a person to approve routine implementation details,
test fixes, wording changes, or a version refresh that preserves their intent.
Ask only for a material change, consequential ambiguity, or when the workspace
explicitly opted into requiring human approval. A quoted person's decision
may still use verified relay_message; your own decision needs no relay.
Read the current approval_version, reject blind stale writes, reassess any
revision and verify the stored outcome. This does not authorize unrelated
work, production releases, cross-workspace access or answering a person's
elicitation dialog yourself.


## Final Report

TL;DR-first, once, for the batch. Every item a markdown link (see "Naming an
Item").

- **What shipped** — one line per item: title, the original ask in the
  requester's terms, where it landed.
- **What deviated** — per item, what was done differently than the approved plan
  and why. Never let a deviation surface only in a diff.
- **Verification** — the batch tier, what the closing run actually was, its
  result (and what each failure matched: the recorded integration-branch run,
  the known-flaky list, or nothing), how many fix rounds it took,
  and any tier escalation phase 3 or a build forced. Failures, skips and anything
  that could not run go here, never omitted.
- **Not done** — blocked, halted, or dropped items, with what each needs.
- **What the workspace learned** — the knowledge entries this run wrote, by
  slug; "none" is a fine answer when nothing durable surfaced.
- **Queued board updates** — any push that never landed, as replayable calls.

Roughly 20 lines. Detail lives in the commits and each item's activity trail —
point at them.

**Before you end.** Stop the processes this session started that outlive a command, such as a dev server, a browser or a watcher. Never stop a process you did not start. A test server `bin/run_tests` started is the runner's, so leave it. Never stop a shared Redis or Postgres, even one you started, because other checkouts use it. Remove each worktree this session created once its branch is merged into the integration branch, or for a verification worktree once its verdict is recorded.

## Forbidden

- Scoping or building work outside current authority, or silently substituting a different task
- Ignoring an enabled workspace planning rule, or restarting a parked item or
  taking someone else's work without authority
- Concurrent builds in a repo with a shared test port, database, or working tree
- Letting sub-agents write to the board, or reporting a sub-agent's claimed test
  result as verified fact without checking it
- Flipping any item to `review`/`done` before the batch's closing run
- Trading the closing run away for speed, or reporting a batch as verified at a
  tier below the one approved at the gate
- Taking a baseline — any suite run before the builds — at any tier
- Treating routine detail as a fresh human decision, or approving work outside existing authority
- Ignoring a selected challenge/review requirement or unassessed cross-item conflict
- Leaving any item's stage stale when the run ends
