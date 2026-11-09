---
name: maestro-review
description: >-
  Review a change and update the docs from it — one checklist, two ways to run
  it: answering a teammate's ask with a verdict bound to the exact commit, or
  as the closing pass of your own build. Correctness, fit to the Approach,
  security, and the documentation the change leaves behind.
user-invocable: true
argument-hint: <item id, branch or commit (omit to review what this session just built)>
maestro-skill-version: 11
---

# Maestro Review — Check a Change, Then Update Its Docs

Two checks close every change: **did this do what the item asked, safely**, and
**do the docs still tell the truth**. This file is the one source for both. It
is read two ways — as a teammate answering an ask, and as the closing pass of
an interactive build — and the checklist does not change between them.

**You never edit the code you are reviewing.** Reading, running the tests that
already exist, and writing findings is the whole of it. A change you rewrite is
a change nobody reviewed.

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

## The Repository's Own Checklist Wins

Before using the lists below, look for the repository's own: `.claude/agents/`
holds a project's review and docs checklists (`security-review.md`,
`docs-updater.md`, or whatever it named them), and `AGENTS.md` / `CLAUDE.md` /
`.claude/rules/` hold the conventions a finding is measured against. Where a
repository has written its own, **follow it** — it knows its framework's
permission decorators, its doc tracks and its exceptions, and this file's lists
are the generic version of the same job. Say in the verdict which checklist you
used.

Where it has written none, the lists below are the checklist.

## Review a Change

**Read the item first.** `get_work_packet(item)` carries the acceptance
criteria, the `## Approach` (or legacy `## Plan`), the `quality_contract` and
the rules the workspec names. Without the item there is no "fit to the
Approach" to judge — say so and review correctness and security only.

**Read the change in your worktree, switched to the exact commit**, never by
switching branches in a folder somebody else is working in. An agent teammate
uses its one worktree, `.maestro/<your folder>/worktree`, and makes no other:

```
git -C <your worktree> fetch origin <branch>
git -C <your worktree> switch --detach origin/<branch>
git -C <your worktree> log --oneline <base>..HEAD
git -C <your worktree> diff <base>...HEAD
```

Record the exact commit you reviewed — `git rev-parse HEAD` in that worktree.
Every finding and the verdict are bound to it. Commit nothing there; an agent
switches back to its own branch when it is done.

Then read for four things, in this order:

### Correctness

- The change does what its diff claims, on the paths a reader will actually
  take — not just the happy one. Empty input, absent optional data, the second
  call, the concurrent one.
- Errors are handled where they happen, and a failure leaves nothing half
  written.
- Nothing silently swallows an exception, and no failing check was made to pass
  by weakening, skipping or deleting it.
- The tests added actually fail without the change. Read them: a test that
  asserts the code calls the function it calls proves nothing.

### Fit to the Approach

- Every acceptance criterion is met, or the gap is named. Quote the criterion.
- What the diff does that the Approach did not ask for — an unrequested
  refactor, a second app touched, a new dependency, an abstraction for one
  caller. Scope the plan did not carry is a finding even when the code is good.
- What the Approach asked for and the diff does not do.
- A migration, a shared helper or a changed contract the Approach did not
  anticipate: the verification tier no longer bounds the change, and the
  finding says so.

### Security

Five categories, and a change gets all five even when four are obviously
empty:

- **Permission gaps** — an endpoint, handler or model with no authorization
  check, or one too broad for what it does; a write path reachable without the
  matching read permission; a permission check that a caller can satisfy by
  naming their own tenant.
- **Data exposure** — sensitive fields (passwords, tokens, secret keys, hashed
  values, another tenant's rows) reachable through a serialization shape, a
  search field or an over-broad response; identifiers in a URL or a log line
  that should not be there.
- **Injection risks** — queries built by string formatting or concatenation;
  unsanitized input reaching a shell command, a query, a template or a path;
  `eval`/`exec` or dynamic code execution over anything a user supplied.
- **Auth bypasses** — a path that skips authentication; a check using OR where
  AND is meant; a permission evaluated before the caller is known to be
  authenticated; a public-creation default on a model that should not have one.
- **Secret leakage** — credentials, API keys or tokens hardcoded, defaulted in
  settings, written into a fixture, or printed by a log line or an error
  message.

Focus on the diff, not the repository's pre-existing state — unless the change
makes an existing weakness reachable, which is a finding about this change.
Don't flag a pattern the repository deliberately documents as intentional; when
in doubt, rate it `info` and say why you are unsure.

### What It Leaves Uncovered

Behavior the change added with no test, and the test the plan promised and the
diff does not hold. The tier decides which tests *run*; it never decides
whether a behavior is covered.

## The Verdict

One comment on the item (`comment_on_item`), opening with exactly this line:

```
Review: pass | changes needed · commit <sha> · N critical / N warning / N info
```

- `pass` or `changes needed` — those two words, nothing else. **Any critical
  finding means `changes needed`.**
- `<sha>` is the full or short hash you actually read, from the worktree. A
  verdict that names no commit binds to nothing.
- The three counts are the findings below it.

Then the findings, **most severe first**, one block each:

```
[CRITICAL|WARNING|INFO] <category> — <file>:<line>
  <what is wrong, and the reasoning someone else can reproduce>
  Recommended: <what to do about it>
```

- **critical** — it must change before this merges: a security hole, a
  correctness defect on a path users take, an acceptance criterion not met.
- **warning** — it should change, and a person may decide otherwise: scope the
  plan did not carry, a missing test for new behavior, an error path nobody
  handles.
- **info** — worth knowing, blocks nothing.

Findings that are true but outside this item — a defect the change did not
create, doc debt it only revealed — go last, one line each with file and line,
under `Outside this item:`. They never block. On a Team the Maestro files each
as its own item; in an interactive session you file them after posting the
verdict (the incidental-findings rule).

Close the comment with **what you did not review and why** — a file you could
not read, a runtime claim you did not execute, a repository you cannot see. A
review with no limits stated reads as a review with none, and that is almost
never true.

**Then record it** with `record_work_result` (through `invoke_tool`): a
`verification` whose `subject` is the delivery record for the commit you
reviewed (`get_board_item(item).results` lists it), method `review` (or
`tests` for a run you did yourself), result `pass` or `fail`, your counts as
`checks`, and what you did not review as `limitations`. No delivery for that
commit on the task means none to verify: say so in the verdict instead.
Independence is computed from who delivered and who verified, never claimed.

Two rules about numbers: a test run you did is reported as counts (collected /
passed / failed), never as approval; a runtime claim you did not execute is
marked unexecuted. Neither is a verdict on its own.

## Review and integration authority

`require_merge_review` is off by default. Choose review in proportion to risk.
When the workspace enables it, obtain an independent passing review before
integration. A Team reviewer is the routing destination, not an implicit rule.

- Bind every review claim to the commit actually read. New changes invalidate
  that claim for the files changed; assess the delta rather than repeating
  unaffected checks.
- Fix critical defects or record a reasoned resolution within your authority.
  Escalate a material disagreement, an explicit wait, or a decision reserved
  by the workspace's human-approval rule. Do not ask a person merely to settle
  routine review details or because a fixed number of rounds elapsed.
- Never describe your own checking as an independent review. When independence
  is required, another agent can provide it; no human click is implied.
- Integrate verified authorized work without a fresh human staging go. Follow
  existing explicit or owner-recorded standing release authority for production;
  same-scope fixes reuse it. Material changes or absent authority need a decision.
  Explicit waits and release holds remain binding.

## Update the Docs

The other half, from the same diff. Run it after the review, on the change as
it actually landed — never from the plan, which is what was intended.

1. `git diff <base>...<branch> --name-only`, then the diff itself. What
   changed decides which pages move:
   - a new or changed endpoint, argument or response shape → the track the
     repository keeps for API consumers
   - a new or changed model, service or internal contract → the track it keeps
     for its own developers
   - configuration, defaults or deployment → usually both
   - behavior with no reader-visible surface → often none, and "no docs needed,
     because …" is a complete answer
2. **Read the pages before editing them.** Match their structure, headings and
   voice; a section written in a house style of your own is a second style the
   repository now has to keep.
3. Update the index a folder keeps (`README.md` or its equivalent) when you add
   a page. A page nothing links to is a page nobody finds.
4. Framework and third-party documentation is linked, never copied.
5. Retired conventions are not resurrected: if the repository says a file is no
   longer maintained (a changelog, a legacy index), leave it alone.
6. **Doc debt the diff merely revealed** — a whole folder with no index, a page
   describing behavior removed three releases ago, a track that was never
   written — is not this change's to fix. Name it, say it should be filed, and
   move on. Widening the docs pass is the same mistake as widening the diff.
7. **The item's `## How to check`.** **Building**: an engineer is writing it. **Review**: the code is pushed, and agents review, test and merge it, with no person. **Done**: reviewed, passing and merged, or finished with its outcome recorded and no change. A person is asked only when a task is marked for a person's check, and then once.
   Only a task marked for a person's check needs the block — Where, Live yet,
   Steps, You should see — in plain words a person can follow from that block
   alone: no hashes, file paths or test names, and "live" only when it has
   shipped. On such a task, missing or unfollowable is a finding.

The result is either the updated pages, or the sentence "no docs needed,
because …". Both count; silence does not.

**Who writes them.** When delegating docs, use the Team's Docs Writer on a
branch stacked on the build branch; otherwise update them yourself under this
checklist. Record who wrote them. An available role alone does not require a
handoff or an extra acceptance round.

## Two Ways to Run It

The checklist above is identical in both. What differs is who asks, and where
the answer goes.

### Answering an ask — the agent-teammate path

**A teammate session is one where `whoami()`'s `session.persona_agent` is
non-null.** Anything else — including a brokered owner session in a desktop
composer — is interactive, and takes the closing-pass path below.

- The ask arrives with an item id, a branch and usually a commit. Read the
  packet, review, and post the verdict with `comment_on_item`. **The verdict is
  the deliverable** — not a stage move, not a fix.
- **Never edit the code, never commit, never push, never move the item's
  stage**, and never file the findings as items of their own unless your role
  says you may. The verdict comment is the record.
- **Where the verdict goes.** `changes needed` is your answer to the author
  (their ask brings it back to them; if someone else asked, `ask_agent` the
  author too). On `pass`, when an integration handoff is needed and authorized,
  `ask_agent` the Team's Integrator to merge that commit, naming the item,
  branch and commit. Already-integrated or no-change reviews need no merge ask. A commit touching
  files outside the ones its piece was given goes to the Lead.
- **Before ending the turn, say who you are waiting on.** If you asked anyone
  anything, the comment names them and what you need; then
  `check_in_board_item_checkout` if you hold one, and end. On resume,
  re-acquire and re-read the packet — the change may have moved while you
  waited.
- Answer in the shape the ask came in. A `ask_agent` answer is the asker's next
  prompt, so the verdict text goes there too, not only on the item.
- **Before you end.** Stop every process you started that outlives a command,
  such as a dev server, a browser or a watcher, by the pid you recorded or the
  command you used. Never stop a process you did not start. A test server `bin/run_tests` started is the runner's, so leave it. Never stop a shared Redis or Postgres, even one you started, because other checkouts use it.
  Remove any worktree you made beyond your one per repository, after its work
  is pushed, or for a verification worktree once its verdict is recorded. Keep
  your one worktree, and never remove another agent's. The
  desktop app also stops what a session started and removes finished
  worktrees. This step is a courtesy that keeps the computer usable until then.

### The closing pass on your own build — the interactive path

After the commit, before the item is called done. Three steps, in order:

1. **The tier's tests**, exactly as the plan's verification contract names them
   — that is the build skill's step, unchanged.
2. **The review checklist** over your own diff. Apply the repository's checklist
   under `.claude/agents/` when present, otherwise the lists above against
   `git diff <base>..HEAD`. Use a fresh read-only helper or teammate when
   independent review is selected or risk warrants it. Self-checking is not
   independent review.
3. **The docs checklist** from the actual diff. Delegate to the repository's
   `docs-updater` when useful, otherwise update docs yourself.

Then report: the verdict line, the findings, and what the docs step did. A
closing pass that ran and found nothing is reported as "ran, nothing found" —
never as silence, which reads identically to not running it.

The same evidence standard applies to interactive sessions and teammates.
Use normal team handoffs when needed; a role label alone does not mandate an
extra review or acceptance round.

## Assigned Work Is The Same Shape

The same checklist answers an item assigned to you, a DM, or a request that
names no item at all — a person's branch, an old module, a release candidate,
a dependency bump. Only the inputs move:

- **No item** → there is no Approach to fit, so review correctness, security
  and coverage, and say that the fit half was not applicable. The verdict still
  names the commit; where there is no item to comment on, answer wherever the
  ask came from, in the same format.
- **A module or an audit rather than a diff** → the "commit" is the revision
  you read at (`git rev-parse HEAD`), so the verdict still pins what was true
  when.
- **A release candidate** → the base is the last released tag, and the fit half
  is measured against the release's own list of what it claims to contain.
- **A whole-wave security review** (the Maestro asks when the Plan flags a
  wave) → the diff is the wave's merged range on the integration branch; work the
  Security section only, and the verdict names the integration commit you read.

## Record What The Review Learned

A finding is about one change; what the reviews keep turning up is about the
project. When a review establishes a durable truth — a convention the code now
assumes, a decision with a rejected alternative, a measured fact a later change
must respect — record it where your role may:
`upsert_workspace_doc(workspace, kind, slug, title, content)`, kind `decision`
/ `convention` / `fact`, one entry per fact, a stable slug. Read the reply's
`similar` before the next write: a near match means updating that slug, not
adding a twin. Roles without the knowledge group say what should be recorded
and who should record it, rather than skipping it silently.

Not recorded: the findings themselves (they belong on the item), transcripts,
diffs, file dumps, or anything the repository's own docs already say.

## Forbidden

- Editing, committing or pushing the code you are reviewing, or rewriting a
  change so that it passes.
- A verdict that names no commit, or one that says `pass` over a critical
  finding.
- Reporting a green test run as approval, or a claim you did not execute as one
  you did.
- Reviewing your own work and calling it independent.
- Fixing doc debt or findings the change did not create — list them under
  `Outside this item:` and leave them.
- Ending a turn on an ask without saying who it waits for and checking the
  checkout in.
- Moving an item's stage, approving a plan, merging or releasing anything.
