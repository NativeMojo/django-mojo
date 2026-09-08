---
name: maestro-task
description: >-
  Explore the codebase, clarify scope, and file one or more work items onto the
  maestro workspace board via the maestro MCP — the board item (markdown
  workspec, stage=inbox) is the work record, not a local file.
user-invocable: true
argument-hint: <feature/bug description — one, or several separable pieces of work>
maestro-skill-version: 20
---

# Maestro Task — File Work onto the Board

The work record is a maestro board item, live to everyone on the workspace:
**state** (stage, priority, owner, due) in its column values, **the spec** (the
"workspec") in its markdown description, **progress** on its activity trail.

## One Item or Many

**One invocation files any number of items.** A request routinely carries
several separable pieces of work, and a user handing you a list expects a list
back — not one item that quietly staples them together.

- **Split on separable units of work**: two things that could be scoped, built
  and shipped independently are two items. Trivia that would each be a one-line
  diff rolls into one housekeeping item instead of six rows.
- **Say the split before filing** — the titles, one line each — and get a yes. A
  user who meant one item will say so, and a wrong split costs a sentence to fix
  now and four workspecs later.
- **Explore once, write per item.** Step 4 covers the whole ask; each item then
  gets its own self-contained workspec. State a shared constraint in every item
  it binds — never "see the other item".
- **Nest under a parent** with `parent=<id>` when the pieces are one epic's
  children: file the epic first, then the children. Unrelated items stay flat.
- **The step-3 size check applies per piece, not to the pile.** Three one-file
  fixes are three vibes, not three board items; a small piece riding alongside a
  substantial one is usually part of that item, not a row of its own.
- **Report as a table** — id (markdown link), title, priority, parent — and hand
  off with every id at once.

## Board Resolution (all maestro-* skills)

**A session-stated workspace comes first.** When the conversation's first
message says which Maestro workspace — and, for an attached item, which board —
this session is anchored to — My Maestro sends one:
`This session is anchored to Maestro workspace "My Maestro" (id 35), board "Backlog" (id 116) — use that for board resolution; it overrides .claude/maestro.json in this session.`
— that line IS the board config for this session. It overrides
`.claude/maestro.json`, whose board may belong to another workspace this repo
also files into. Use the stated
board; with a workspace alone, keep the file's board only if `get_board` shows it
in that workspace, else `list_boards(<stated workspace id>)` and ask. The numbers
in that line are the workspace and board ids — never item ids, never part of the
task text — with one exception: a **continued** session sends a shorter form of
the same line, which stops after the workspace and may end `, task #NNN` with a
note that the replayed task read may be stale. THAT number is an item id: re-read
it with `get_work_packet(NNN)` before acting on it. The short form carries the
same authority as the long one.
In every report and trail note, say which you used and why:
`board "Backlog" (My Maestro workspace, id 116) — from the session's anchor`.

1. Absent a session-stated workspace, read Maestro's repository config at
   `.claude/maestro.json` in the repo root:
   `{"workspace": "<name or id>", "board": <board id>, "project": <project id>}`.
   `project` is **optional**, for when several repos share one board: it is the
   Project column value stamped on every item these skills file from this repo.
   Store the numeric id — the column value verbatim, nothing to resolve or
   drift. Omit the key when the board serves a single repo.
2. If the file is missing or the board doesn't resolve: call `whoami()` to
   confirm auth, then `list_workspaces()` and `list_boards(workspace)`, ask the
   user which board is this repo's work queue, and offer to write Maestro config
   so future sessions skip this step.
   - `list_workspaces()` returning `[]` is **normal for a new account, not an
     error**: the personal workspace `whoami()` reports holds the key and
     credits but cannot hold a board. Say so, then offer `create_workspace(name)`
     — it makes them admin and comes with a strict Work board. Ask first;
     never create one unprompted. (Names are claimed globally, so a taken name
     fails — suggest a distinctive one.)
3. If maestro is unreachable or unauthenticated: **stop with an explicit
   notice** and offer the repo's local intake skill (e.g. `/request`) if one
   exists. Never fall back silently.
4. Call `get_board(board, items=False)` once and keep the column schema — the
   columns, the roster, the board's name and `item_url_template`, without the
   item list, most of the reply on a busy board. (An older server ignores
   the argument and returns the whole board: no error, just no saving.) Match
   `stage` / priority options **by value** from the schema — never assume the
   default template; warn the user if an expected stage option is missing.
5. **Stamp `project` on every item you create.** When the config carries a
   `project` and the board's schema has a `project` column, put it in the
   `values` of every `create_board_item` call — top-level items, sub-items,
   incidental findings and vibe history rows alike. Never ask the user which
   project a repo belongs to; that is what the config is for. If the config
   names a project but the board has no project column, file the item anyway and
   say the label was dropped — do not silently discard it.
6. **Keep the board's `name` and `workspace.name` from that call, and use them
   in everything you say to the user.** Ids are internal keys — "board 8" tells
   a reader nothing. Whenever you report filing, moving, commenting on or merely
   mentioning something, lead with the human name:
   - board → `board "Backlog" (Maestro workspace, id 8)`, not `board 8`
   - item → `#586 "An agent cannot see what it deployed"`, not `#586`
   - parent → say it is one: `filed under #516 "Sites + domains (epic)"`
   - several items → a table of id, title and the values you set
   - always include the item URL — `create_board_item` returns one, and
     `get_board` returns `item_url_template` for items you did not create
   If you have only an id, look the title up (`get_board_item`) before writing
   the sentence.
7. **Say which client and model you are.** Pass `client=` (the client you are
   running in — "Claude Code", "Cursor", "ChatGPT", "Codex desktop", …) and
   `model=` (your model id) on every `create_board_item`, `update_board_item`
   and `comment_on_item` call. The server cannot observe either, so a write that
   stays quiet is recorded under the workspace's default label — the trail reads
   "via Claude" no matter who actually wrote it.

## Workflow

1. Call `get_workspace_context(workspace)` — apply any `rule` docs to your work.
   Reference docs by slug in the workspec ("Apply rules: ...") instead of
   pasting their content. Then read what the workspace already knows about
   this area: `search_knowledge(<2-3 content words>, project=<workspace or
   project id>)` for prior decisions and facts, and `get_knowledge_map(<the
   workspace>)` when you need the lie of the land. Cite what applies in the
   workspec's Investigation ("Prior knowledge: `<slug>` — …") so scoping starts
   from it instead of rediscovering it; an entry that says the ask is already
   settled or already shipped changes the filing, not the wording.
2. Parse the task description from the arguments (or ask what they want). If it
   carries several separable pieces of work, name the split now — see "One Item
   or Many"; everything below then runs once per piece.
3. **Size check — ask before filing.** Not every request belongs on the board.
   If the description reads like a small, single-session change (a typo, a
   one-file fix, a small bug, a config tweak — faster to do than to write a
   workspec for), stop and ask the user: "This looks small enough to vibe-code
   directly — want me to run `/maestro-vibe` on it now instead of filing a board
   item?" File without asking only when the task is clearly
   multi-session/cross-cutting, or the user has already indicated (in
   conversation, or by invoking this skill with that intent) that they
   specifically want it tracked. When in doubt, ask: a board cluttered with
   silly small items is worse than one extra question. If the user opts to vibe
   it, switch to the `maestro-vibe` skill and do not create a board item.
4. Explore the codebase — what exists, what changes, constraints. Ask
   clarifying questions until scope is unambiguous: contract/shape of the
   change, permissions, edge cases, what's explicitly out of scope.
5. **Dedupe before filing.** `search_items(<2-3 distilled content words>,
   include_finished=true)` — per separable piece. A live match: point at it
   instead of filing a twin. A `finished`-flagged match is a regression —
   file fresh and name the item it re-opens. A **`rejected`-flagged match
   was DECLINED**: report it as "declined on <date> because <kind>" with
   the `duplicate_of` link when it carries one, and **ask before filing
   anyway** — a deliberate re-proposal reopens the item or files with the
   prior decision named, never silently fresh.
6. Compose the workspec markdown (template below). Write the human block
   **first and for a person**: someone who knows the product but has never read
   the code must finish those few sentences knowing what is wrong, why it
   matters, and what done looks like. If it only makes sense to a reader who
   already has the codebase in their head, it is not the human block yet.
7. Create the item:
   `create_board_item(board, title, values={<resolved slugs and values>}, description=<workspec>)`.
   Resolve the workflow column by its option roles and stamp the option whose
   role is `intake`; never assume either slug `stage` or value `inbox`.
   Resolve Category, Horizon and Impact by explicit purpose (with only the
   documented legacy Horizon fallback). Category is required when the board
   supports it: map the workspec's canonical Kind to the exact option value.
   Impact is written only when the request or investigation supplies concrete
   evidence for high/medium/low; otherwise leave it unset and say so. Never
   invent `medium` as a neutral default. Put every supported stamp, including
   the configured Project value, in this one create call.
   **Resolve the dispatch column from `get_board()`'s schema, never by a
   hardcoded slug**: the category column marked `purpose: "horizon"` wins;
   without one, fall back to the category column slugged `moscow`. Write that
   column's slug with one of ITS option values, matched **by value, never by
   position**. On a purpose-marked column the vocabulary is horizon: default
   `next`; write `now` only when the user says the work should be picked up
   immediately; `later` only when they explicitly defer it. When urgency is
   unclear, ask in the same breath as the milestone question ("now, next, or
   later?"). Write only values the column defines — a column without `next`
   gets no stamp, never an invented value. On a moscow-resolved legacy
   column the default stays `should` (ask or infer as before). No column
   resolves → omit the key; never invent a column.
   **Milestone stamp** (boards with a milestone column): filing under a
   parent inherits the parent's milestone value in the same `values` — the
   trajectory rail only works if the journey writes it. No parent signal →
   ask in the same breath as priority ("which milestone — or none?"; "none"
   is fine), listing `list_milestones` when the user needs the options.
   Never block filing on it, and never create a milestone here — creation is
   scoping's call (`maestro-scope`'s stamp rule) or the human's.
8. **Record what exploring taught.** A premise that is false, a measured limit,
   a convention the code assumes — anything a stranger filing the next item
   here would want to know: `upsert_workspace_doc(workspace, kind, slug, title, content)` with kind `decision` / `convention` / `fact`, one entry per fact, a stable slug, and a body a stranger can act on. Read the reply's `similar` before the next write — a near match means update that slug, not add a twin. Not recorded: the workspec
   itself, transcripts, file dumps, anything already in the repo's own docs.
9. Name the new item as a **markdown link**, never a bare id — see "Naming an
   Item" below — and hand off: "run `/maestro-scope <item-id>` to scope it."
   Several items: the table from "One Item or Many", then one hand-off line
   carrying every id — `/maestro-scope 431 432 438`, or `/maestro-auto 431 432
   438` to scope and build them unsupervised.

## Naming an Item

Every time you name an item to the user — here, in a recap, anywhere — write it
as a markdown link:

```
[#<id> <title> (<stage>)](<url>)
```

Take `url` straight from the tool result (`create_board_item`, `get_board_item`
and friends return it; `get_board` returns one `item_url_template` with `{id}`
to substitute). Never hand-assemble a host.

Not cosmetic: a bare id is not something a user juggling parallel sessions can
place, and in a repo with a GitHub remote a bare `#123` gets auto-linkified by
the client to `github.com/<org>/<repo>/issues/123` — a real link to the wrong
system.

## Workspec Template

**A workspec has two tiers, and the `---` is the line between them.** Above: a
plain-language block written for a person, the only part of the description a
human is expected to read. Below: everything the sessions that scope and build
the work need, dense on purpose.

```markdown
<Human block — 2-5 plain sentences, no heading: the problem or the want, why
it matters, and what done looks like. Written for a reader who knows the
product but has not read the code: product nouns are fine, file paths, code
symbols and item ids are not.>

---

## Spec

Agent-facing from here down. **Kind**: bug | feature | ui-change | chore | docs | security ·
**Requested by**: <who asked for this> · **Source**: <what found it, date,
commit — incidental findings only>

### Acceptance Criteria

- [ ] <Specific, testable criteria>

### Investigation

- **What exists**: <current state of related code — file paths, not dumps>
- **What changes**: <high-level summary>
- **Constraints**: <framework limits, permissions, costs>
- **Related files**: <paths>
- **Out of scope**: <explicitly excluded>
```

The meta line replaces the old title/date/requester header. Two optional pairs
may follow on it when they apply:

- `**Parent**: #<id> "<title>"` — for a child of an epic; say what the child
  owns and what the parent's plan already fixes.
- `**Filed**: <YYYY-MM-DD>` — **only** when the description's real origin date
  differs from the item's own `created` (a fallback file folded onto the board
  after an outage). Otherwise leave it out — the item already records when it
  was created.

Anything else — a dependency, a related item, a decision still open — is prose
in the spec where it arises, not invented meta syntax.

`/maestro-scope` appends its `## Approach` below this, at the same level as
`## Spec` (the task-level design section — "Plan" now means the parent
roster unit, #2905; legacy items keep `## Plan` and the build skill reads
both), and keeps the human block true as understanding changes.

## Rules

- Do NOT implement anything. Exploration and documentation only.
- No Status line in the workspec — stage lives on the board.
- **The human block earns its place by being readable.** No file paths, code
  symbols, item ids or acronyms above the divider; if a technical fact is
  load-bearing, state its consequence in plain words up there and the fact
  itself below.
- **Say each thing once.** Put a fact where it belongs and reference it from
  nowhere else — repetition is what made old workspecs long without making them
  clearer.
- Keep repo dumps out of the workspec — reference file paths; the scoping and
  build sessions run inside the repo and can read them.
- A work item is board-backed XOR file-backed — never create both.
