---
name: maestro-build
description: >-
  Claim one or more planned maestro board items (owner + stage=building),
  execute each ## Approach (legacy ## Plan) inside this repo with full build
  discipline, keep every item's activity trail updated (commits, tests,
  blockers), and land them at review/done via the maestro MCP.
user-invocable: true
argument-hint: <item-ids, e.g. "431" or "431 432" (omit to pick from the board)>
maestro-skill-version: 50
---

# Maestro Build — Execute a Planned Item

You are a senior engineer executing a scoped item one task at a time: minimal,
correct, tested code matching the repo's existing patterns and conventions (read
`AGENTS.md` when present, plus `CLAUDE.md` and applicable project rules). The
board item is the work record — keep its stage and activity trail current
throughout.

## Task clarity

Every task states **Problem** and **Desired outcome** in its human summary.
Before implementation, record the **Proposed solution** in the separate
`solution` field: what will change and how it addresses the problem. The
Workspace Maestro is accountable; authorized builders may draft or repair it.
Resolve missing details within authority; ask a person only for a genuinely
missing decision. Before any human-facing approval ask, save all three so the
approval UI can show them. Missing summary fields alone do not block routine
stage changes or Done. Never invent an outcome or substitute technical working
notes for the explanation a person needs.

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

**Before you end.** Stop the processes this session started that outlive a command, such as a dev server, a browser or a watcher. Never stop a process you did not start. A test server `bin/run_tests` started is the runner's, so leave it. Never stop a shared Redis or Postgres, even one you started, because other checkouts use it. Remove each worktree this session created once its branch is merged into the integration branch, or for a verification worktree once its verdict is recorded.

With several ids, run the flow below per item, with these differences:

- **Validate the roster once** — resolve linked titles and scope before the first claim; ask only about a real mismatch or missing authority.
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
coordinates actual missing decisions without redundant gates.

## Board Resolution

Same as `maestro-task`: a session-stated workspace in the first message comes
first, else read Maestro's `.claude/maestro.json` repo config; on any miss, resolve via
`whoami()` / `list_workspaces()` / `list_boards()`, ask, offer to write the
file. Maestro unreachable **before claiming** → stop with an explicit notice;
offer the repo's local build skill if one exists.

Also from `maestro-task` — **step 5: name boards and items when you report them, never bare ids**: `board "Backlog" (Maestro workspace, id 8)`, `#586 "An agent cannot see what it deployed"`, with the item URL (link format under "Reporting Back"). That lookup is far cheaper for you than for the user.

**Step 7: pass `client=` (the client you are running in) and `model=` (your model id) on every `create_board_item`, `update_board_item` and `comment_on_item` call.** The server cannot observe either — a write that omits them lands in the trail under the default label, "via Claude", whoever wrote it.

## Checkout lifecycle

After identity validation, acquire before the claim or snapshot with
`acquire_board_item_checkout(item, purpose="build",
idempotency_key=<fresh high-entropy key>, expected_minutes=...)`. Keep the raw
`checkout_token` only in this orchestrating session. It is a bearer capability
and appears in the owning MCP transcript by design; never copy it into domain
data, comments, logs, URLs, realtime messages, durable browser storage, or a
sub-agent prompt/transcript.

A checkout conflict is coordination, not an automatic human decision. Read the
holder, purpose and expiry; coordinate with the holder, wait or continue other
independent authorized work. Never seize an active checkout or claim a lease you
do not hold. Proceed without an advisory lease only when non-conflicting work is
actually authorized; an actual permission/approval refusal is not a lease conflict.
Never bypass it or fabricate a Lead review. Resolve selected requirements through
the normal allowed route. Keep the token only in the orchestrating session.

After the acquire attempt,
re-fetch `get_work_packet(item)` and latest comments before the claim. If title,
stage, description, owner, or contract materially changed, reassess authority before continuing. Sub-agents
always start in fresh isolated context and never acquire or receive the token.

Renew no later than 40 minutes after acquire/last renewal, around long commands
and waits, and before consuming a sub-agent result. Keep waits short enough to
heartbeat. Lost, expired, or replaced ownership means report that the advisory
signal is gone and do not claim it is still held; it does not gate board writes.

**Check in (progress) as you renew.** The renewal call takes `activity` (one
line, what you are doing right now — "running ingress tests") and `criteria` +
`criteria_count` (sparse per-criterion ticks keyed by 1-based ordinal into the
workspec's `### Acceptance Criteria` list, states `active`/`done`/`blocked`;
`criteria_count` is how many checkboxes you see and is required with every
criteria-bearing check-in). Check in at natural boundaries — a criterion
passed, a phase change, a blocker hit, a sub-agent dispatched or returned —
still no later than 40 minutes apart; one round trip both heartbeats and
reports, and it writes no trail note. After any reacquire (a lapse takeover), a
new generation starts with an empty report, so the first check-in re-posts the
full current criteria state. "Check in (progress)" is renew-with-report;
"return the checkout" is `check_in_board_item_checkout`, unchanged.

Check in (return the checkout) after the
done/review write or after the blocker comment, and on every other terminal
path. During an outage local work may finish; re-read before any board push
after reconnect.

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

### When to ask, and what a person approves

Beyond a person's check, ask a person only when a preemption would push a goal past its target date, a bug touches money, security or customer data, or two agents reached different severities on the same bug. A person approves only: a production release, spending, destructive data changes, changing a goal's criteria, and creating or dropping a goal or project. A production release still needs the existing release
authority described above. Selected workspace rules (`require_human_approval`,
a `mark_person_decides` hold, an explicit wait) add asks on top of these lists;
everything else is decide-and-log, and dashboards and work queues are the
review.

### Bugs, severity and preemption

- **File it plain.** A bug goes on its product's board with no project and
  no goal; never create a goal or project to hold bugs. Reproduce it, dedupe
  with `search_items(..., include_finished=true)`, link the monitoring finding
  (`link_finding_task`) or QA run (`qa_link_bug`).
- **Assess severity yourself** from impact, with the reporter's label as one
  input: Critical is `mark_board_item_critical(item, on, reason)`; high is
  `impact: high` on the bug plus a comment carrying the evidence links.
- **Attach a goal only when the bug blocks one** — it stops an open goal's
  criterion, or came out of that goal's own work. Unsure which goal a goal-less
  Plan serves? `manage_goal(action="propose")` rather than attaching it.
- **Critical or high preempts goal work.** Take it without asking and leave one
  line on the Plan you leave: `mark_plan_paused(item=<plan>, bug=<bug>,
  reason=...)`; `on=false` resumes it. A `reversed_before` refusal is a
  person's earlier answer (they lowered a bug that paused this Plan): keep
  working the Plan unless you have new evidence, then retry with
  `acknowledge=true` and say why.

## Validate the Item — identity check

Read `get_work_packet(item)` before mutation. Match the title, board/workspace,
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

## Pre-Flight

1. **Pick the item(s).** With item-id arguments, use them — one or several.
   Without any, call
   `workspace_queue(workspace, stage="planned")` — it spans **every** board in
   the workspace, where `get_board(board)` sees only the board
   `.claude/maestro.json` names and reports a short queue, no error, wherever a
   team routes work across two. Rows span boards, so list candidates with
   **their board name** (id, title, board, priority). Select work within an
   explicit queue remit and state the selection; otherwise ask which to build.
   The user may name more than one. Never claim silently.
   Read the reply's `boards` sidecar before presenting the list: a board whose
   `unmatched_stages` names `planned` does not carry that stage at all — re-ask
   it with a value from its own `stage_values` rather than reporting it empty.
   If `workspace_queue` is unavailable (older server), fall back to
   `get_board(board)` and say so.
2. `get_work_packet(item)` — one call for the whole pre-flight: the workspec,
   the names, the staleness facts, the contract, and the workspec's rules
   inlined under `rules.applied` (apply them; name any `rules.missing` slug out
   loud). `knowledge.retrieved` carries the workspace entries nearest this
   workspec — read them like `rules.applied`, and say when one changes the
   plan. `skills.pinned` carries the workspace skills the workspec pins, in
   full, and `skills.retrieved` names up to three more nearest it: read the
   pinned ones, and each retrieved one whose `why` fits
   (`list_workspace_docs(workspace, kind="skill", slug=<slug>)`), before your
   first edit. Prefer a `## Approach` — **or the legacy
   `## Plan` heading, which this skill reads forever**: ~300 shipped workspecs
   keep the old heading, there is no bulk rewrite, and this fallback is
   permanent — do not "clean it up". With neither heading, assess whether the
   existing request is sufficient; only an enabled `require_approach` rule makes
   the heading a prerequisite.
   **Heed the packet's `plan_gate`** (#2905) when the item is a sub-item of a
   Plan: `{parent_status, wave, blocked_by, warning}` says where this task
   sits in the parent's waves and which prerequisites are unfinished. It is
   advisory — building anyway is allowed — but say the warning out loud
   before proceeding past it.
   **Read the feedback.** Handle `unaddressed_comments` within existing authority.
   Ordinary comments do not stop work. Explicit waits remain binding; a material
   scope change needs a decision. Use `addresses=` when acknowledging work actually
   handled. Only `require_addressed_directives` makes unresolved directives block
   Done. Never mark feedback done just to clear a flag.

   **Read parked/rejected decisions.** Do not resume them from an ID alone.
   A clear authorized instruction to resume/reopen is sufficient; use the normal
   writers and record the reason. Otherwise skip and report the prior decision.
3. If `values.owner` names another agent, coordinate responsibility and actual checkout state before changing ownership. Do not seize active work. Escalate only an unresolved decision outside authority. A person on Owner is not a reason to stop: they decide the plan,
   and the claim leaves them there.
4. **Validate and proceed.** Resolve identity and authority as above; the clear request needs no additional confirmation.
5. **Claim** in one call:
   `update_board_item(item, values={"stage": "building", "owner": [<the ids already on Owner>, <your user id from whoami()>]})`.
   Append to Owner, never replace it: the person already there decides the
   plan, and removing them moves that decision. Add your user id from
   `whoami()` to Owner only when `whoami()` says `is_runner: false`. A runner's
   agent is already on Owner from the hand-out, so its claim is the stage alone.
6. Pull the description to `planning/.cache/<item-id>.md` (gitignored) — the
   temporary working copy for the session. Never commit generated planning
   snapshots; the board item and its activity trail are the durable record.
7. **Read the verification tier** and set up for it. The step-2 packet already
   carries `quality_contract` — `{"tier", "run", "evidence"}`, the decision
   scoping made as data — so this costs no extra call. (Older server without
   `get_work_packet`: `get_board_item` returns the same key.) Read it from
   there; the `### Verification` prose is the human copy of the same thing and
   the fallback for items scoped before contracts shipped.
   With several items, read them all now and take the **highest** as the run's
   tier; it sizes the one closing run.
   - **No tier takes a baseline.** Nothing runs before the first edit; what a
     red run is judged against is already recorded — see "Judging a Red Run".
     Start building.
   - Whatever `run` names still runs, at every tier — that is where a bug's
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
   that reproduces the bug and confirm it FAILS before touching the fix. A new
   bug you find on the way is filed per "Bugs, severity and preemption" above.
4. Implement one logical unit at a time, following the repo's conventions.
   Write/finish tests immediately after each unit, not at the end. Fix failures
   in your code, not the tests.
5. Commit each logical unit per the repo's git conventions (no push unless the
   repo's rules say otherwise).
6. **After each commit / test run / blocker**, post to the trail:
   `comment_on_item(item, ...)` — commit hash + one-line summary, test counts, or
   the blocker. If the plan changed during the build, push the updated scratch
   file back with `update_board_item(item, description=...)`.
7. **Verify at the plan's tier — that tier, not a bigger one.**
   - **`none`** → no test run. Do the thing the plan's `Why:` line named in
     place of tests, and report both.
   - **`targeted`** → run the modules the plan named, and stop there. Running a
     full sweep to feel safe spends the user's minutes on your comfort.
   - **`full`** → run the repo's default suite **once, after the change**, and
     judge any failure under "Judging a Red Run". `full` is a
     verification-coverage tier; it does not imply the test runner's `--full`
     slow/extended option. Off the integration branch — an item worktree, a
     side branch — use the runner's opt-out from reporting to Maestro (testit:
     `--no-maestro`), so a branch's verdict never stands as the project's
     status.

   **Size verification to the actual change.** If the diff acquired something the plan
   did not anticipate — a migration, a shared helper or contract with callers
   across modules, a dependency bump — move up a tier, run it, and say in the
   report that you escalated and why. Rewrite the contract in the same breath:
   `update_board_item(item, contract={"tier": "full", "run": […],
   "evidence": […]})`. The server records the tier change on the trail itself,
   so the item's declared tier keeps matching what actually ran instead of
   drifting behind it. A proportionate tier adjustment within authority is recorded with its coverage rationale. Preserve explicitly mandated checks; never narrow to hide a failure or unresolved risk.
   **How to check, only for a person's check.** A task marked for a person's
   check needs a `## How to check` (where, live yet, steps, what you'll see);
   no other task does. Builder test results are evidence, never a person's
   verdict.

8. **The closing pass — review the change, then update its docs.** After the
   last commit and the tier's tests, in this order, both from `maestro-review`
   — that file is the one checklist and this is one of its two ways to run:
   - **Review your own diff** against the item: correctness, fit to the
     `## Approach`, the five security categories (permission gaps, data
     exposure, injection, auth bypass, secret leakage) and what the change
     leaves uncovered. Apply the repo's checklist under `.claude/agents/`
     when present, otherwise `maestro-review`'s lists. Use a fresh read-only
     helper or teammate when independent review is selected or risk warrants
     it; never call self-checking independent review.
   - **Then the docs, from the diff as it landed** — not from the plan, which
     is what was intended. Use the repo's docs checklist; delegate to its
     `docs-updater` when useful, otherwise update docs yourself, in the
     repo's own tracks and conventions. **"No docs needed, because …" is a
     complete answer**; silence is not.
   - Report both in the closing summary: the findings (with file and line),
     what you fixed, what you filed instead per the repo's findings rule, and
     what the docs step did. A pass that found nothing says so — it reads
     identically to a pass nobody ran otherwise.
   - Fix what the review finds **in this item** only when the item cannot be
     correct without it; anything else is an incidental finding and follows
     the repo's rule for those.
9. **Record what the build learned.** For each decision with a rejected
   alternative, rule the code now assumes, or measured/discovered truth:
   `upsert_workspace_doc(workspace, kind, slug, title, content)` with kind
   `decision` / `convention` / `fact`, one entry per fact, a stable slug, and
   a body a stranger can act on. Read the reply's `similar` before the next
   write — a near match means update that slug, not add a twin. Not recorded:
   transcripts, file dumps, tool logs, anything already in the repo's own
   docs.
10. **Close.** **Building**: an engineer is writing it. **Review**: the code is pushed, and agents review, test and merge it, with no person. **Done**: reviewed, passing and merged, or finished with its outcome recorded and no change. A person is asked only when a task is marked for a person's check, and then once.
   Pushed or PR opened → `update_board_item(item, values={"stage": "review"})`;
   merged to the integration branch, or committed straight to the main
   branch → `values={"stage": "done"}`. Final
   comment on the trail: what changed + how to validate. Then report back to the
   user TL;DR-first (see below). **In a multi-item run, steps 7-10 do not happen
   per item** — the item stays at `building` and the next one starts; the run's
   single closing pass verifies every item, reviews each item's own diff and
   flips them all.
   **Before the flip, record the result** with `record_work_result` (reach it
   through `invoke_tool`; read `get_board_item(item).results` first and pass
   its `head` as `expected_head`, with a fresh UUID `request_key` per write):
   a `delivery` of your branch tip (repo and the full 40-hex commit), then
   your own `verification` of that delivery (method `tests`, the result, the
   contract run's totals as `checks`, and `test_run` when the runner reported
   a run on that tip), then an `outcome` once the task is finished. The
   delivery is the builder's own tip: integration merges with `--no-ff`, so
   the combined suite's and the deploy's commits never equal it. The trail
   comment still says it in prose; the record is what results readers trust.
11. **On failure/blocker**: post a blocker comment, leave `stage=building` and
    the owner intact, and tell the user where it stands — in the same
    TL;DR-first shape. **Blocked on a human ANSWER rather than a failure?** In
    an interactive client chat (including the Agent Tool), ask the human in
    chat and wait there. Do not call `comment_on_item(question=...)` merely to
    mirror that interactive ask onto the board; the human is already present,
    and a second durable state can outlive the chat answer.

    For a real **async/background handoff**, use
    `ask_task_question(item, text, recipient, creation_key, blocking=true,
    client=..., model=...)` with the chosen eligible human and a fresh UUID.
    Read its exact answer with `get_task_question(question)` before resuming work;
    clarification, reading a bell and `addresses="all"` do not answer it.
    If the recipient is wrong, the requester/current recipient/manager can use
    `redirect_task_question` with the last read version and optional reason.
    Check in and stop while a blocking answer is pending.

    Only the authenticated current human recipient can answer with
    `answer_task_question`; an agent credential cannot impersonate them. A
    human-credential session may relay their explicit answer, visibly attributed
    via client/model. Never relay a different person's answer as your own.
    Managers must redirect to themselves before answering. When directed asks
    are not enabled, `comment_on_item(question=<text>)` remains the legacy
    unassigned adapter; its returned `resolve_question.arguments` names the
    exact asking note. Explicit addressing still checks answer authority.

## Verification Across Several Items

One closing run for the whole set, never one sweep per item.

- **The run's tier is the highest tier any item carries.** A helper or contract
  one item creates and another consumes is a bound that held per item and does
  not hold together — **escalate both to `full` and say so**. Two items editing
  one file keep their tiers: the closing union runs both items' modules against
  the combined tree. That judgement exists only at this level; no single plan
  can make it.
- **Any item at `full`** → the default suite once, after the last item lands.
  No baseline.
- **`targeted` / `none` only** → the closing run is the union of every item's
  named modules, run once, after the last item lands.
- **Every item at `none`** → no closing run either. Say plainly that no suite
  ran, and what stood in for it per item.
- A bug's regression test is the one exception at any tier: it runs alone,
  before and after that item's fix, because a regression test nobody saw fail
  proves nothing. Seconds.
- **Red** → judge it under "Judging a Red Run"; attribute new failures to the
  item owning the touched files (the per-item commits make this unambiguous),
  fix in place, re-run the closing set.
  One sweep per fix round. An item you cannot get green stays at `building` with
  a blocker comment naming the failing tests; the rest still flip.
- **Green** → flip every built item in one pass, `review` or `done` per step 10.

Nothing is `done` mid-run: until the closing verification is assessed, the truth
is "built".

## Judging a Red Run

No build takes a baseline. The one question a baseline answered — *was this
failure already there?* — is answered from what is already recorded, and only
for the tests that actually failed.

1. **The last recorded integration-branch run.** `test_status(project,
   branch=<the integration branch>)` — the project in the repo's
   `.claude/maestro.json`, the branch its work merges into before release as
   the repository's instructions or the workspace's rules name it — returns that branch's latest
   run per suite with its failing tests, whatever other branches pushed since.
   Read the row for the suite you ran (the runner names it; testit names it
   after the preset, so a default run and an all-tier run are different rows)
   and say which commit and date you compared against. A test in its `failing`
   list was already red there. A session whose grant refuses the tool, or a
   server that refuses or ignores `branch` (its reply carries no `branch` key),
   has no record: go to step 4. With no integration branch named there is no
   integration baseline; say so. Never judge against the item's own branch,
   which already holds the change.
2. **The workspace's `known-flaky-tests` fact**, when it keeps one
   (`search_knowledge` or `get_workspace_context`). A listed test failing with
   its recorded signature is a known flake: name it and its item, and do not
   re-run the suite to rediscover it. The same test failing with a different
   message is not covered by the list.
3. **Anything left is this change's until shown otherwise.** Read the failure
   first — most name the change outright. If it doesn't, run that one test file
   alone on the integration branch's current commit, in a worktree of it: red
   there means it predates you. On a tree holding nothing but this item's work,
   `git stash -u`, the same targeted run and an unstash settle it too. On a
   **shared tree** — another session's or another agent's uncommitted work
   beside yours — never `git stash` and never `git checkout` over it: the stash
   stack is shared and you would take away changes that are not yours (#4358
   left 39 uncommitted paths in one tree, and a stash there would have swept
   them).
4. **No usable record** — no row for that branch and suite, or a runner that
   does not report — say so, and settle each failure by step 3.

Then act on what each failure is:

- **Within the approved fix**: keep the failing test as regression evidence and
  proceed with the authorized repair.
- **Already red, or a known flake, and isolated**: record the test and what it
  matched (the recorded run's commit, or the fact's item) on the trail, file a
  newly found pre-existing failure under the repo's findings policy, tell the
  user and continue without asking for another approval. Do not widen the build
  to fix it, skip it, or weaken its assertions. The work may go to **review**
  with those failures explicit in the trail and final report; never describe the
  suite as green or fully passing, or mark the work done on this exception. A
  release or integration gate still applies independently.
- **New, and this change's**: fix it in place and re-run the closing set.
- **Cannot verify the proposed change**: repair routine environment problems
  within the existing authorization. Pause only affected work if failures remain
  unattributable, prevent meaningful verification, or require a scope or product
  decision; explain the concrete blocker and ask only for the missing decision.

<a id="when-a-persona-agent-built-it"></a>

## When an Agent Built It

An item may come back built by a named agent teammate — a Lead or a Software
Engineer — rather than by you. Its reply names a branch and commit hashes; that
is a claim, never evidence.

1. **Fetch the branch it named** (`agent/<item id>-<role>`):
   `git fetch origin agent/<item id>-<role>`. Read the diff before anything
   else — what it changed, what it did not, and whether the tests it says it
   added actually fail without the change.
2. **Inspect the evidence.** Check source/command/counts/logs and coverage.
   Reuse valid evidence for current or assessed unchanged code. Rerun affected
   checks after relevant changes, failures or uncertainty; preserve explicitly
   required contracts and report whose evidence was used. Prose alone is not
   proof, and independent review is never supplied by the author.
3. **Integrate verified work within existing authority.** No fresh human staging
   approval is needed. Require independent review when the workspace selects
   `require_merge_review`; otherwise use judgment. Production still needs
   existing release authority covering the scope, and explicit holds apply.
4. If the work is short of the item, say so on the item and ask for a rework
   round rather than finishing it silently under the agent's name.

The teammate's own procedure is the workspace skill "How this team ships work" —
the same branch and worktree rule, written in its terms.

## When an Agent Teammate Runs This

**A teammate session is one where `whoami()`'s `session.persona_agent` is
non-null.** Every other session is interactive and the skill above applies
unchanged — including a brokered owner session in a desktop composer, whose
`authority` reads `agent_session` but whose `session.persona_agent` is null.

The build is the same build: read before editing, one logical unit at a time,
tests with each unit, the plan's tier, the trail kept current. What changes is
every gate that waits for a person, and where it happens: a teammate builds,
and takes any clean baseline, in its ONE worktree of the repository
(`.maestro/<your folder>/worktree`, switched as its brief says), one item at a
time. Parallel builds above are a person's session; parallel work for a
teammate goes to other teammates.

| The gate above | As a teammate |
|---|---|
| Board Resolution — ask which workspace, offer to write `.claude/maestro.json` | The ask carried the item; resolve the rest from `get_work_packet(item)`. Never pick a board, never write a repository config file. Maestro unreachable: say so in your reply and stop. |
| Identity validation | Match title/workspace/scope to the request and proceed under existing authority. Clarify actual mismatches; parked/rejected decisions require resumption authority. |
| Pre-Flight 1 — select work | Resolve the named task or select within an explicit queue remit. Ask only when identity or selection authority is missing. |
| Another owner | Coordinate responsibility and actual activity. Do not seize active work; escalate only an unresolved decision outside authority. |
| Pre-Flight 5 — claim the item | Only if current permissions admit the item edit. Otherwise it was claimed for you — the Maestro made you its Owner, or the Lead holds the item and you work its piece under that checkout; take your own row's checkout when you are its Owner, and say which happened. |
| Checkout conflict | Coordinate, wait or work on independent authorized tasks. Never seize an active lease or bypass an actual refusal. |
| Approval authority | Use agent judgment under the workspace rule above; no human relay or fresh confirmation is required by default. |
| Step 1 — say what you mean to do differently | Same words, posted as `comment_on_item` before the first edit, not spoken to a room. It is not the turn's report: a turn ends with evidence, the wait you started, or a named blocker. |
| Verification sizing | Use proportionate checks and valid source-bound evidence. Record a reason for tier changes; preserve explicitly mandated checks and rerun for relevant deltas, failure or doubt. |
| Step 8 — the closing pass | Choose proportionate checks and review; use the Team routes below when independent review is selected. |
| Step 10 — close, and report TL;DR-first to the user | Post the same TL;DR as a comment. Use Review while required integration or acceptance is outstanding; complete when actual authority and evidence allow it. A builder label alone does not impose another handoff. |
| Step 11 — blocked on an answer: ask in chat and wait there | You are not sitting in a chat. `ask_person(question, item=…, why=…)` reaches who started your turn, else the task's person Owner, never an admin (`withdraw_chat_question` takes it back once it no longer matters); `ask_task_question(item, recipient=<user id>, …, blocking=true)` asks a named person, or makes a deliberate task-record handoff — then the waiting rule below. |
| Judging a Red Run — "ask only for the missing decision" | The same ask, then the waiting rule. Record the closing run (revision, command, counts, failing tests and what each matched) on the trail **first**, so the resumed turn does not pay for it twice. |
| "When an Agent Built It" — never merge without a person's yes | Use the authorized Team integration route; push your own branch and report it. Integrate verified work within existing authority, without fresh human approval; honor optional require_merge_review and explicit waits. Production needs existing release authority. |

**Choose review proportionately; require an independent pass only when require_merge_review is on.** Authors check their own work and may update its docs. They never call that checking independent. When independent review is needed:

- **When review is needed**, ask the Team reviewer with item, branch and exact
  commit. `require_merge_review` requires an independent passing verdict; otherwise
  choose review proportionately. Re-review changed files, not unaffected work.
  Resolve routine findings within authority. Escalate a material disagreement,
  not an arbitrary number of rework rounds. Never claim an unperformed review.
  After sending a review ask, end the turn; never wait for the verdict in a
  running turn. Follow the waiting rule below so the item resumes correctly.
- **When delegating docs**, use the Team's Docs Writer, found by Role through
  `list_teammates`, on a branch stacked on yours. With no Docs Writer on the
  Team, you write them yourself under `maestro-review`'s docs checklist and say
  so — "no docs needed, because …" counts, silence does not.
- Whoever wrote the change cannot provide its independent verdict. If you
  authored this item, ask another reviewer when independence is needed.

**One hand-off path when delegating review.** Ask a Maestro teammate with
`ask_agent` and record the actual verdict on the item. `ask_agent` may refuse for want of a live
acknowledged request (an owner console turn, or one your own background work woke after your request ended): then post on the item what is
ready and who should do it, and never claim a hand-off you did not make.

**The waiting rule — how a turn ends without stranding the item.** Before you
end a turn on an ask:

1. **Comment who you are waiting on and what for**, naming the teammate or the
   person, the question, and the commit if a review is what you asked for.
2. **`check_in_board_item_checkout`.** A lease runs an hour and an ask can take
   a day; holding it locks the item behind a session that has stopped thinking
   about it.
3. **End the turn** — no further post, no final text.

On resume: **re-acquire the checkout and re-read `get_work_packet(item)` before
anything else.** The packet is the only state this turn shares with the last,
and the branch, the verdict or the item may have moved while you waited. The
same on every rework round, however short.

**Before you end.** When your part is pushed, or you stop for good:

- Stop every process you started that outlives a command, such as a dev
  server, a browser or a watcher, by the pid you recorded or the command you
  used.
- Never stop a process you did not start.
- A test server `bin/run_tests` started is the runner's, so leave it. Never stop a shared Redis or Postgres, even one you started, because other checkouts use it.
- Remove any worktree you made beyond your one per repository, after its work
  is pushed, or for a verification worktree once its verdict is recorded. Keep
  your one worktree, and never remove another agent's.

The desktop app also stops what a session started and removes finished
worktrees. This step is a courtesy that keeps the computer usable until then.

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

- Working on an identity mismatch or outside actual authority; a clear request needs no redundant confirmation
- Ignoring an enabled workspace planning rule, or claiming over someone else's
  owner without authority
- Expanding scope beyond the item; touching files outside the plan without
  flagging it first
- Ignoring explicit waits, claiming unhandled feedback is addressed, or finishing
  past unresolved directives when `require_addressed_directives` is on
- Writing no tests for a behavior change — the tier decides which tests are
  *run*, never whether the change is covered
- Narrowing verification to hide a failure or unassessed risk, or omitting explicitly required checks
- Reporting a build as verified when the tier's runs did not happen, or quietly
  running a default suite the plan did not ask for
- Taking a baseline — any suite run before the change — at any tier, or
  re-running a whole suite to rediscover a failure the recorded run or the
  known-flaky list already explains
- Leaving the item's stage stale after the build ends
- Closing a build without the review-and-docs pass, or reporting a pass that
  did not run — "nothing found" and silence read identically, and only one of
  them is true
- Reviewing your own change and calling it independent
- Ending a turn on an ask without saying who it waits for and checking the
  checkout in
- Closing a build without stating what deviated from the plan and how
  verification actually went
- Building several items concurrently in a repo with a shared test port,
  database or working tree, or flipping any of them past `building` before the
  run's single closing sweep is green
