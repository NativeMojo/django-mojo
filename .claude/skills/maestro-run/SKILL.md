---
name: maestro-run
description: >-
  Validate and execute a Plan within existing authority, scope unknowns,
  coordinate work and assess source-bound verification evidence. Ask only for
  material missing decisions or selected workspace requirements; reconcile
  the actual roster and outcomes at close-out.
user-invocable: true
argument-hint: <one Plan item id, e.g. "2900">
maestro-skill-version: 21
---

# Maestro Run — Execute an Approved Plan

A Plan is a board item whose sub-items form its roster. A clear request to run
it authorizes ordinary execution within existing scope and current permissions;
an already-approved roster needs no second “Run this roster?” confirmation.
Validate what the request covers, record real decisions and honor selected
workspace rules. Material scope changes or missing authority need a decision;
ordinary sequencing and scoping do not manufacture a new human gate.

You are the orchestrator, not the builder:

- **Sub-agents touch code.** Scoping, red-teaming and building each run in a
  fresh-context sub-agent. They read, plan, edit, test and commit; they never
  write to the board and never hold a lease.
- **You own the board, the leases and every decision.** Every
  `update_board_item`, `comment_on_item`, stage flip, checkout and question goes
  through you, so a sub-agent that dies mid-task never leaves a half-updated
  board.
- **You verify the evidence.** Inspect source, logs and coverage. Reuse valid evidence; rerun after relevant changes, failures or uncertainty.

### Two kinds of helpers

A run may have two very different helpers in flight, and they are not
interchangeable:

- **Fresh sub-agents** — this skill's own, spawned with the Agent tool. They
  read, edit, test and commit in this checkout (or a worktree of it), and they
  have no Maestro identity at all: they never write to the board, never hold a
  lease, and never see a checkout token.
- **Named teammates** — named agents (a Lead, a Software Engineer, a QA
  Engineer) a person or a Lead asks with `ask_agent`. They work in their
  OWN worktrees on `agent/<item id>-<role>` branches, push those branches, and
  write to the board under their own role's ceiling and their owner's
  supervision. A teammate reports to the Lead that asked it; the Lead reports
  to you. Their procedure is the workspace skill "How this team ships work" —
  read it if you need to know what they were told.

Either helper's prose is a claim, not proof. Inspect its source-bound evidence
and rerun on a relevant change, failure or uncertainty; valid unchanged evidence
can be reused. Helpers never supply authority their caller lacks. No **sub-agent** ever writes to the board or holds a checkout token; named teammates use their own authenticated identity.

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


## When to Use It

**One Plan item id**, whatever its roster size — `/maestro-run 2900`.

- **`/maestro-auto`** for flat filed items that have no Plan: it scopes them,
  builds them within existing authority and asks for actual missing decisions.
- **`/maestro-scope` + `/maestro-build`** when the plan should be read before it
  is pushed to the item, or the build steered as it happens.
- **`/maestro-vibe`** for work too small to track.

A childless Plan (`progress.total == 0`) is the plan-of-one: legal, approvable,
and run as a single task — the Plan item itself.

Given several ids, ask which one is the Plan; this skill takes exactly one.
Given a roster task's id instead of the Plan's, name the parent and offer to run
that.

## Board Resolution

Same as `maestro-task`: a session-stated workspace in the first message comes
first, else read Maestro's `.claude/maestro.json` repo config; on any
miss, resolve via `whoami()` / `list_workspaces()` / `list_boards()`, ask, offer
to write the file. Maestro unreachable or unauthenticated → stop with an explicit
notice; never fall back silently. This run cannot read a roster approval it
cannot reach, and one that assumes an approval it never saw is exactly the
failure the gate exists to prevent. Call `get_board(board, items=False)` once and
keep the column schema; match stage options **by value**.

Also from `maestro-task` — **step 5: name boards and items when you report them,
never bare ids.** `board "Backlog" (Maestro workspace, id 8)`, `#586 "An agent
cannot see what it deployed"`, with the item URL (format below); that lookup is
far cheaper for you than for the user.

**Step 7: pass `client=` (the client you are running in) and `model=` (your model
id) on every `update_board_item`, `comment_on_item` and checkout call.** The
server cannot observe either — a write that omits them lands in the trail under
the default label, "via Claude", whoever wrote it.

## Naming an Item

Name the Plan and every roster task to the user — the gate card, checkpoints,
the final report — as a markdown link, never a bare id:

```
[#<id> <title> (<stage>)](<url>)
```

`url` comes from the tool result: `get_work_packet` and `get_board_item` return
one; `get_board` returns an `item_url_template` with `{id}` to substitute. Never
hand-assemble a host. In a repo with a GitHub remote the client auto-linkifies a
bare `#123` to that repo's issue 123 — a live link to the wrong system.

## Pre-Flight — validate scope and authority

1. Read the Plan and every intended task with `get_work_packet`. Validate title,
   workspace, roster, current scope, comments and rules. A sub-item is not the
   Plan: resolve the requested target rather than silently choosing another.
2. Read `plan.status` (`approved`, `unapproved`, `roster_changed`), `approval_version`, `tasks_changed`, `roster_drift`,
   `can_decide`, `waiting_on`, `needs_owner` and `progress`. Check the actual
   delta. Reuse a valid approval. A clear run instruction needs no repeat yes.
   Changed scope within authority may be decided and recorded by the agent;
   material expansion or an enabled human-only rule with a missing decision
   needs the appropriate person. Never fabricate their approval or bypass a
   refusal.
3. If a decision is needed and is within authority, call
   `approve_board_item_plan(item, expected_version=plan.approval_version,
   client=..., model=...)`, then read back the actual result. Use the normal
   writer; never edit approval metadata directly. On refusal, read its real
   requirement and resolve it through the allowed route.
4. Size scoping to what is unknown. Complete explicitly selected planning
   requirements before dispatch; missing recommendations alone are advisory.
   Scope material unknowns first and compare the result with existing authority.
5. An approved roster without waves is executable within scope. Choose and
   report a safe order for its `unscheduled` tasks, using actual shared files,
   dependencies and capacity; record waves when needed by selected policy.
   Do not ask the user to choose routine scheduling. Honor explicit exclusions;
   do not silently add tasks outside the intended roster.
6. Read feedback, explicit waits, `plan_gate.blocked_by` and actual advisory prerequisite facts. Ordinary comments
   do not block work. Only `require_addressed_directives` makes unresolved
   directives block Done; acknowledge only feedback actually handled.
7. Report the intended roster, opening `progress`, verification plan and material
   decisions concisely. Proceed under existing authority. Ask one concrete
   question only if a real missing decision prevents the dependent work.

## The Scope Checkpoint — assess what changed

Scope unknown work before building what depends on it. Use `maestro-scope` and
normal validated board writes, including the plain-words `solution` required by
the current writer. Honor selected Approach, Challenge, contract and Lead-review
rules; recommendations alone are not mandatory gates.

Compare the discovered scope with the request and recorded authority. Resolve
routine design, sequencing and verification choices, recording their reasons.
Correct false premises, duplicated work and file/migration collisions through
normal writers. Ask only about a material change outside authority or a decision
reserved by workspace policy; continue independent authorized work meanwhile.
Read back any roster decision through the current versioned approval writer.
No blanket scope-digest confirmation or second roster yes is required.

## Checkout lifecycle

The orchestrator — not any sub-agent — owns every checkout token.

Take one lease on the **PLAN item** for the whole run:
`acquire_board_item_checkout(<plan>, purpose="build", idempotency_key=<fresh
high-entropy key>, expected_minutes=…)`. `purpose` accepts only `"scope"` or
`"build"`; there is no `"run"`, and `build` is the honest one for a session that
is shipping code. Then take a per-task lease before that task's scope or build,
and check it in on its terminal path — the stage write, the blocker comment, a
cancellation, a dropped task. Use a fresh high-entropy idempotency key per
acquisition generation.

Renew every lease no later than 40 minutes after the acquire or last renewal,
immediately before and after long waits and commands, and before accepting a
sub-agent's result. Keep waits short enough to heartbeat.

A checkout conflict is coordination, not an automatic human decision. Read the
holder, purpose and expiry; coordinate with the holder, wait or continue other
independent authorized work. Never seize an active checkout or claim a lease you
do not hold. Proceed without an advisory lease only when non-conflicting work is
actually authorized; an actual permission/approval refusal is not a lease conflict.
Never bypass it or fabricate a Lead review. Resolve selected requirements through
the normal allowed route. Keep the token only in the orchestrating session.

Never pass a checkout token to ordinary board writes. It is a bearer capability,
visible in the owning MCP transcript by design and nowhere else: never copy it
into a workspec, comment, log, URL, realtime message, durable browser storage, or
a sub-agent's prompt. Every post-acquire sub-agent starts in fresh isolated
context and receives the workspec, never the raw token or the owning transcript.

After every acquire, re-read the packet and latest comments before starting. A
material title, stage, description or contract change since the gate needs reassessment within existing authority.

## Live Progress on the Plan

The Plan lease is how mission control watches this run. `renew_board_item_checkout`
carries the report and writes no trail note, so one round trip both heartbeats
and reports:

- **`activity`** is the live sentence — "wave 2 of 3 — building #2941",
  "re-running the closing suite", "blocked: waiting on a human answer".
- **`criteria` + `criteria_count`** tick the PLAN workspec's
  `### Acceptance Criteria` ordinals as tasks land. Send them **only when the
  Plan's workspec actually has that checkbox list**. The list is a filing
  convention, not a server guarantee, and `criteria_count` must be at least 1 —
  on a Plan without one, send `activity` alone.

**A rejected report rejects the renewal with it.** Criteria ticked against a Plan
that has no criteria list do not degrade to an activity-only check-in; the whole
call fails and the heartbeat is silently lost. On any check-in error, re-renew
bare immediately, then fix the payload.

After a reacquire the report starts empty, so the first check-in of the new
generation re-posts the full current state rather than a delta.

Check in at wave boundaries, at each dispatch and return, and on every blocker —
still never more than 40 minutes apart. Task-level progress needs no extra
writes: mission control reads each task's stage straight out of the Plan payload.

## The Run Loop

**Building**: an engineer is writing it. **Review**: the code is pushed, and agents review, test and merge it, with no person. **Done**: reviewed, passing and merged, or finished with its outcome recorded and no change. A person is asked only when a task is marked for a person's check, and then once.

**Use the Plan's facts and the selected policy.** Re-read the packet before
work; reassess scope changes under existing authority. `enforce_plan_dependencies`
controls strict wave/merge ordering. Otherwise use `waiting_on`, `unmerged` and
advisory prerequisites to assess real readiness and report sequencing choices.
An empty blocker list does not prove a missing foundation exists; a review-stage
item is not proof its code merged. Resolve those facts, not a rote human gate.
Record actual integration and completion evidence. Person checks still go to the
named person only when their decision is required.

**Within a wave, sequence by shared files.** Two tasks whose plans touch the same
file, or migrate the same app, run in order. The rest parallelize only where the
repo's own rules say a checkout is isolated — its own test database, port and
cache namespace. Verify that against the repo's rules; do not infer it from the
existence of worktrees.

**Route each task from its own packet:**

- `inbox` or missing recommended artifacts → assess unknowns and selected rules;
  scope what is needed, then proceed within authority.
- `planned` → build when actual scope, access and readiness permit.
- parked/rejected → preserve the prior decision unless resumption is authorized;
  otherwise skip and report.
- already `done`, or `review` with its merge recorded → count as shipped, do
  not re-run.

**Verify the combined change once where needed.** Honor explicit mandatory
contracts; a required full suite runs once at the appropriate integration point.
Otherwise choose affected combined checks and reuse valid unchanged evidence.
No automatic full suite follows merely from a wave label. A red result is
resolved or attributed honestly before dependent work relies on it.

**Record cross-plan seams before the builds that consume them.** Where two roster
tasks share a contract, helper, file or migration, post the seam on each task's
trail with `comment_on_item` naming the other item and the shape both sides must
honor, then hand those note ids to both briefs. A seam agreed in your head and
not on the trail is a seam the second agent never sees.

## Per-Task Agent Briefs

Every brief carries, without exception:

- **The item id and its work packet** — the workspec, its rules, its contract.
- **The binding trail note ids** it must honor, quoted by id: seams recorded
  above, human directives, deviations already accepted in this run. "Honor the
  trail" is not a brief; the ids are.
- **The as-built shapes of roster siblings already landed in this run** — actual
  names and signatures as built, not as planned. The plan is what was intended;
  the agent has to compile against what exists.
- **The repo's Maestro workflow** — its own rules files, git conventions, test
  commands and doc tracks.
- **That task's verification contract**, tier and named runs included.
- **The packet's `knowledge.retrieved` entries** — the workspace's memory
  nearest this task, applied beside the rules; the agent says when one
  changes the approach.

**Scoping agents are read-only.** They return the complete updated description
(human block, `## Spec`, the `## Approach` they just wrote) plus, separately: the
files they intend to touch, the verification contract in `{"tier", "run",
"evidence"}` shape, schema and migration impact, contract changes for existing
callers, a premise check naming anything in the workspec that turned out
false or already shipped, and their durable findings as `{kind, slug, title,
content}` rows — decisions with a rejected alternative, conventions the code
assumes, facts measured or discovered — which you write at the scope
checkpoint, because no agent holds a knowledge tool.

**Build agents** edit and commit per the repo's git conventions, run **no** suite
— they write tests as part of the change and verification is yours — never `git
push`, and never write to the board. The one exception is a bug's regression
test, which the agent runs alone before the fix and after, because a regression
test nobody saw fail proves nothing. They return durable findings the same way
scoping agents do; you write them at reconciliation.

No **sub-agent** ever receives a checkout token or calls a board tool; a
named teammate's board writes are its own role's, under its owner's
supervision.

## Review — evidence and changed behavior

Inspect source, commands, counts, logs and coverage. Reuse valid current or
assessed unchanged evidence. Rerun affected checks for a relevant delta, failure,
missing proof or doubt. A conflict-resolved merge may need new runtime checks.
Preserve explicit mandatory contracts and selected independent review; never
call self-review independent. Report whose evidence was reused and what you ran.
A sub-agent's prose alone, zero collection, unmatched/wholly skipped targets,
import errors and early exits are not passing evidence. Check the actual commits,
claimed files and unrelated staged work. Record and resolve failures honestly.
Choose combined closing verification proportionate to the change; do not repeat
unchanged checks merely because another agent ran them.

## Human Checkpoints — plain English, on the board

Decisions that are the human's ride the board, not just the transcript:

- In an **interactive client chat** (including the Agent Tool), ask the human
  there and wait. Do not call `comment_on_item(question=...)` merely to mirror
  that ask onto the board; the human is already present, and the durable alert
  can outlive their chat answer.
- For a true **async/background handoff**, use
  `ask_task_question(item, text, recipient, creation_key, blocking=true,
  client=..., model=...)`; choose an eligible human and a fresh UUID. Read the
  exact answer through `get_task_question(question)` before resuming work. Clarify
  does not resolve; `addresses="all"` excludes question roots.
- Only the current human recipient may use `answer_task_question` with the
  current version. Agents cannot answer for another person. A human-credential
  MCP relay must reflect that human's explicit answer and retain client/model
  attribution. Use `redirect_task_question` to correct the recipient first.
- If directed asks are disabled, the legacy unassigned
  `comment_on_item(question=<text>)` adapter and its explicit
  `resolve_question.arguments` remain available. Never infer resolution from
  transcript prose or notification read state.
- A human's blocking instruction that you are relaying is `intent="directive"`.
- Anything you act on is acked with `addresses=[<note ids>]`, which is what
  actually clears the board's flag. Prose alone does not.

Write them in plain language, each with your recommendation. **Batch the
non-blocking ones to wave boundaries** and ask them together; a blocking one asks
immediately and halts that branch only.

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

Beyond a person's check, ask a person only when a preemption would push a goal past its target date, a bug touches money, security or customer data, or two agents reached different severities on the same bug. A person approves only: a production release, spending, destructive data changes, changing a goal's criteria, and creating or dropping a goal or project. A production release still needs the existing release
authority. Selected workspace rules (`require_human_approval`, a
`mark_person_decides` hold, an explicit wait) add asks on top; everything else
is decide-and-log. A task that leaves goal work for a critical or high bug
records `mark_plan_paused` on the Plan it left (see `maestro-build`, "Bugs,
severity and preemption"); a paused Plan's rows carry `paused_for`.

**An elicitation dialog posed by the maestro server is addressed to the human —
never answer one, and never treat a pending dialog as the run's to resolve.**
Surface it, say what it is asking, and wait.

Stop and ask whenever a browser or credentials block the work: a login, a
payment, a captcha, an OAuth consent, or a change that needs eyes on real pixels.
Never enter credentials and never accept terms on someone's behalf.

## Stop Conditions

**Stop the affected work** for a material scope change outside authority, a
missing policy-required decision, explicit wait, actual permission refusal,
unexplained failure or invalidated prerequisite. A changed roster/Approach is
reassessed and decided within current authority; it does not automatically
require a human. Continue independent authorized work. Never claim a stopped
or failed task is complete.

**A single task failing does not stop the run.** Post a blocker comment, leave it
at `building` with its owner intact, halt only its dependents and say which, and
continue the independent tasks. A build agent that fails outright — crash,
unusable result — is retried once before its task is blocked.

**Mid-run maestro outage**: never block a build on it. Finish locally, retry each
push once, then queue the remaining board writes in the final report as exact,
replayable tool calls. Re-read before replaying after reconnect.

## Reconciliation — the Plan closes itself

**The Plan's stage is server-derived, not run-driven.** The moment every active
child sits in a finished stage — done, parked or rejected, and parked counts —
the server flips the Plan to done by itself, inside the last child's write
transaction. Two consequences to front-run:

- **With require_addressed_directives on, an unaddressed directive on the PLAN makes that rollup refuse**, and the
  refusal surfaces as an error on the **last task's** stage write, naming the
  Plan's blocking notes. Under that selected strict rule, every Plan directive is addressed before the final
  task flips, not at close-out. This is why Pre-Flight step 6 comes first.
- **A roster finishing {done, done, parked} auto-closes the Plan anyway**, with
  undelivered work inside it. Say so in the report rather than letting a `done`
  Plan stand as "all shipped".

Before calling the run complete, re-read `get_work_packet(<plan id>)` and walk
the **full** roster, `unscheduled` included. One line per task: the link, what
shipped, its stage now, the verification result **with the real numbers**, and
any deviation accepted. Then:

- Compare `progress` against the run's opening scoreboard, naming the parked and
  `review` gap explicitly — `progress.shipped` counts `done` only.
- Name every task that is not `done` and what it needs.
- Post that full-roster report as a `comment_on_item` on the **Plan item's
  trail** — that is the durable record, not this transcript.
- Record what the run learned — every durable finding the agents returned and
  every decision you took on the trail that will bind a later build (a seam,
  an accepted deviation with its reason): `upsert_workspace_doc(workspace, kind, slug, title, content)` with kind `decision` / `convention` / `fact`, one entry per fact, a stable slug, and a body a stranger can act on. Read the reply's `similar` before the next write — a near match means update that slug, not add a twin. Not recorded: transcripts, file dumps, tool logs, anything already in the repo's own docs. Name the slugs in the report.
- Check in the Plan checkout.
- **Before you end.** Stop the processes this session started that outlive a command, such as a dev server, a browser or a watcher. Never stop a process you did not start. A test server `bin/run_tests` started is the runner's, so leave it. Never stop a shared Redis or Postgres, even one you started, because other checkouts use it. Remove each worktree this session created once its branch is merged into the integration branch, or for a verification worktree once its verdict is recorded.
- Give the user the same thing TL;DR-first, roughly 20 lines, every item a
  markdown link.

**The run never writes the Plan's stage itself.** If the rollup has not fired
because work remains, the report says why the Plan stays open.

## Forbidden

- Treating silence as new authority or inventing an approval decision
- Running work outside the intended roster or ignoring material changes
- Treating missing recommended artifacts or waves as a mandatory human decision
- Approving outside existing authority or disregarding an explicit Require human approval workspace rule
- Answering an elicitation dialog addressed to the human
- Ignoring enforced dependency policy or a real missing prerequisite; empty blockers alone prove nothing
- Treating unsupported/stale claims as evidence, or omitting required affected checks
- Letting a sub-agent write to the board, hold a checkout token, or push
- Creating roster tasks mid-run
- Writing the Plan's stage by hand
- Concurrent builds where the repo does not isolate checkouts
- Omitting required verification or hiding a failure for speed, or taking a
  baseline
- Ending the run with any task's stage stale, or without the full-roster report
  on the Plan's trail
