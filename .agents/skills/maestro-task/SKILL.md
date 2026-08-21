---
name: maestro-task
description: >-
  Explore the codebase, clarify scope, and file one or more work items onto
  the maestro workspace board via the maestro MCP — the board item (markdown
  workspec, stage=inbox) is the work record, not a local file.
---

<!-- Generated from .claude/skills/maestro-task/SKILL.md (maestro-skill-version: 11). Do not edit directly. -->

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

1. Read Maestro's repository config at `.claude/maestro.json` in the repo root:
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
     — it makes them admin and comes with a default MoSCoW board. Ask first;
     never create one unprompted. (Names are claimed globally, so a taken name
     fails — suggest a distinctive one.)
3. If maestro is unreachable or unauthenticated: **stop with an explicit
   notice** and offer the repo's local intake skill (e.g. `$request`) if one
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
   pasting their content.
2. Parse the task description from the arguments (or ask what they want). If it
   carries several separable pieces of work, name the split now — see "One Item
   or Many"; everything below then runs once per piece.
3. **Size check — ask before filing.** Not every request belongs on the board.
   If the description reads like a small, single-session change (a typo, a
   one-file fix, a small bug, a config tweak — faster to do than to write a
   workspec for), stop and ask the user: "This looks small enough to vibe-code
   directly — want me to run `$maestro-vibe` on it now instead of filing a board
   item?" File without asking only when the task is clearly
   multi-session/cross-cutting, or the user has already indicated (in
   conversation, or by invoking this skill with that intent) that they
   specifically want it tracked. When in doubt, ask: a board cluttered with
   silly small items is worse than one extra question. If the user opts to vibe
   it, switch to the `maestro-vibe` skill and do not create a board item.
4. Explore the codebase — what exists, what changes, constraints. Ask
   clarifying questions until scope is unambiguous: contract/shape of the
   change, permissions, edge cases, what's explicitly out of scope.
5. Compose the workspec markdown (template below). Write the human block
   **first and for a person**: someone who knows the product but has never read
   the code must finish those few sentences knowing what is wrong, why it
   matters, and what done looks like. If it only makes sense to a reader who
   already has the codebase in their head, it is not the human block yet.
6. Create the item:
   `create_board_item(board, title, values={"stage": "inbox", "moscow": "<must|should|could — ask or infer, default should>", "project": <from .claude/maestro.json, omit if unset>}, description=<workspec>)`
   (use the board's actual priority column/options from the schema).
7. Name the new item as a **markdown link**, never a bare id — see "Naming an
   Item" below — and hand off: "run `$maestro-scope <item-id>` to scope it."
   Several items: the table from "One Item or Many", then one hand-off line
   carrying every id — `$maestro-scope 431 432 438`, or `$maestro-auto 431 432
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

Agent-facing from here down. **Kind**: feature | bug | chore ·
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

`$maestro-scope` appends its `## Plan` below this, at the same level as
`## Spec`, and keeps the human block true as understanding changes.

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
