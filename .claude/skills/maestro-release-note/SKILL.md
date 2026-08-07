---
name: maestro-release-note
description: >-
  Draft the release note for the next release — work out what shipped since
  the last one, read the actual diffs, and write a short summary people can
  use. In the Maestro repo it writes the versioned markdown file that ships
  with the code; in any other repo it files a draft release note to Maestro
  over MCP. It never asks a person to write the words.
user-invocable: true
argument-hint: <version to cut (omit to use the next one the release process will mint)>
maestro-skill-version: 2
---

# Release Note — write what shipped, for the people who use it

A release note answers one question: *what changed for me?* You work that out
from the actual changes, write it, and file it. You never hand the writing
back to the user, and you never write a note whose diffs you have not read.

This skill runs in two modes. They must not bleed into each other.

## Mode — settle it before anything else

Check for **two marker files**, both relative to the repo root:

```
config/settings/version.py
docs/django_developer/maestro/Releases.md
```

- **Both present → mode A.** This is Maestro's own repo. Notes are files in
  the package; nothing is filed over MCP.
- **Either one missing → mode B.** Every other repo. The note is filed to
  Maestro as a draft; nothing is written to disk.

```bash
test -f config/settings/version.py \
  && test -f docs/django_developer/maestro/Releases.md \
  && echo "mode A" || echo "mode B"
```

**State the mode and the reason before your first read.**

Two markers, not one, on purpose: every django-mojo repo carries
`config/settings/version.py`, so a single-marker check would write Maestro's
own release note into another team's tree. Mode A is the branch that writes
files, so it is the branch whose guard must not false-positive. The reverse
error only ever produces a draft somebody can delete.

## Mode A — Maestro's own repo (files in the package)

Release notes here are markdown files in the package, one per released
version. Read `docs/django_developer/maestro/Releases.md` — the *Where the
notes live* section — for the directory and the header contract. That document
is the source of truth for the layout and this skill deliberately does not
restate it.

The portal's What's New panel (`GET /api/maestro/release`) and the MCP
`whats_new()` tool both read those files through one service, so a note
written once appears on every surface. There is no authoring UI and no model:
the file IS the mechanism.

1. **Find the span to describe.** The last note is the highest-versioned file
   in the notes directory. Collect what shipped since it: `git log --oneline`
   back to the commit that added that note, plus the board items finished in
   that span (their trails carry the deviations and decisions that never made
   it into a commit subject). Read the commits and diffs properly:
   **never write a note from commit subjects alone**. (There is no repo
   changelog; it was retired 2026-07-31.)
2. **Pick the version — it must match what `publish.sh` will mint.** The
   script bumps the PATCH on every deploy, so the note for "the next publish"
   is `current + 0.0.1`; read current from `config/settings/version.py`. For a
   deliberate minor or major cut, bump the version yourself first with
   `bin/versioning` and name the note accordingly — `publish.sh` nags on an
   `x.y.0` with no note.
3. **Write `<version>.md`** into the notes directory with the `---`-delimited
   header: `version:` (MUST equal the filename stem — the loader skips a
   mismatch), `date:` as YYYY-MM-DD, `title:`. Then the body.
4. **Voice and shape** — see `## Voice` below.
5. **Validate**: `bin/run_tests -t test_maestro.27_test_releases`. The suite
   parses every shipped note and fails on a version/filename mismatch or a
   malformed header. The test runner starts and owns its own server on a port
   derived for this checkout — do not start a server first and do not go
   hunting for a port number.
6. **Commit the note with (or right after) the code it describes** so it is
   reviewed as part of that diff and deploys with it. Then `./publish.sh`
   ships it; check the bump the script prints equals the note's version.

## Mode B — any other repo (filed to Maestro over MCP)

Nothing is written to disk. The note becomes a row in that project's own
release history, drafted by you and filed with `create_release(...)`.

1. **Resolve the project.** Read `.claude/maestro.json` at the repo root:
   `{"workspace": …, "board": …, "project": <project id>}`. `project` is the
   key you need. If it is absent, call `list_projects(workspace)`, name the
   candidates to the user, ask which one this repo ships, and offer to write
   the key back into the file. **Never infer the project from the directory
   name.** No config file at all → the standard Board Resolution path first,
   then ask.
2. **Run the versioning gate** (next section). If it does not pass, **stop
   before drafting**.
3. **Find the span.** `list_releases(project)` returns the last release and
   its `commit_ref`; that ref is the start of your `git log` span. No releases
   yet means this is the first one — agree the span start with the user (a
   tag, a ref, or the whole history) rather than assuming one.
4. **Read the changes.** `git log <last commit_ref>..HEAD`, then read the
   diffs:
   **never write a note from commit subjects alone** — a subject line says a
   file changed, not what a user will notice.
5. **Draft it.** A **TLDR**: one paragraph on what this release is about. Then
   **Key changes**: a bullet list of the handful of things people want to
   know. See `## Voice`.
6. **File it as a draft.**
   `create_release(project, version, title, tldr, body, released_on=…,
   commit_ref=…)` — pass the commit this release ships from.
   **Setting `commit_ref` is not optional in practice**: every listing carries
   it, and it is what lets the *next* note resolve its span start from one
   `list_releases` call instead of a second round trip. Skip it and you leave
   that cost for the next session.
   The note lands as a DRAFT. Nothing touches the filesystem.
7. **Publishing is a separate yes.** Show the user the draft. Only on an
   explicit go-ahead call `publish_release(project, version)` — never as the
   tail of step 6. `is_public` is a different decision again; leave it alone.

## The versioning gate (mode B)

A release note is only meaningful attached to a release that actually
happened. If a project's version number is typed in whenever somebody
remembers, the notes end up filed against versions nobody ever shipped — and a
history full of numbers that mean nothing is worse than no history at all.

So two things must both be true, and it is the second that matters:

1. **A version source exists** — `pyproject.toml` `[project].version`,
   `package.json` `version`, a `VERSION` file, a `<pkg>/version.py`, or git
   tags.
2. **Something automated bumps it** — a publish/release script, or a CI step.
   A version string that is only ever edited by hand does not qualify. That is
   precisely the case that produces notes pinned to versions nobody shipped.

Both true → read the version and carry on. Otherwise **stop before drafting**
and explain: notes are anchored to real releases, here is what is missing,
shall I set it up?

### How to check condition 2

In this order, and **say which check fired**. "I found automated versioning"
with no named evidence is exactly the false positive this gate exists to catch.

1. **A script in the repo that writes the version source.** Grep the release,
   publish and deploy scripts for the file condition 1 found.
2. **A CI workflow step** that bumps or tags on merge or on release.
3. **Release tooling that owns versioning** — semantic-release,
   release-please, changesets, bumpversion, poetry-dynamic-versioning, or the
   ecosystem's equivalent.
4. **`git log` on the version source, or `git tag` history.** Real evidence,
   but the weakest, and last for a reason: someone bumping diligently every
   release looks identical to a script until you read the commits.

**When it is ambiguous, ask. Never pass.** The costs are not symmetric — a
wrong pass leaves a permanent history of fictional versions, a wrong question
costs one line.

### The failure this prevents, from Maestro's own tree

Maestro is a live instance of it. Its `__version__` was set in the initial
commit and then sat at `1.0.186` for three months and roughly a hundred
deploys, because nothing ever called the bump script that existed the whole
time. Condition 1 satisfied, condition 2 absent. Every note filed in that
window would have named a version nobody shipped, and "is my fix live yet?"
had no answer. This is not a hypothetical trap; it is the normal one.

### Cases worth naming

- **Version source present, no bumping evidence at all** — the headline case.
  Refuse, and name both halves: the field you found *and* the absence of
  anything that writes it.
- **Tags exist but never move** (one `v0.1.0` from the week the repo was
  created) — fails condition 2, exactly like a stale `package.json`.
- **CI bumps, but several copies of the version disagree** — passes. Say which
  source you read, and flag the drift. Reconciling it is not this skill's job.
- **A monorepo with several version sources** — ambiguous. Ask which one the
  release is cut from.
- **"Just use 1.0.0"** — refuse. The gate is the stance, not a formality, and
  a version the user invents is the very thing it exists to prevent.
- **Mode A** — the gate does not run.

### Offering to set it up

Propose it, **get a yes**, then do it. Never as a silent side effect of asking
for a release note.

Port the pattern to the target repo's ecosystem rather than pasting somebody
else's script. The properties that make it work are portable even when the
language is not:

- **One command bumps the version** — major, minor, patch, or an explicit set.
- **It supports `--dry-run`**, so the bump can be seen before it is written.
- **Every copy of the version is written together.** Hand-editing one is how a
  package file comes to sit at `0.1.0` while the product reports `1.0.186`.
- **The deploy step calls it**, so shipping and bumping cannot come apart.
  This is the part that turns condition 1 into condition 2.

Once versioning is in and has produced one real version, **stop there** and
offer drafting as a next step. Do not chain straight into writing the note —
that turns one approval into two changes.

## Voice

Both modes, one voice.

- Written for the people **using** the product, not the ones building it —
  what changed for them, in their words. No file paths, no commit shas, no
  item numbers.
- Lead with the change users notice most. One section (mode A) or one bullet
  (mode B) per meaningful feature; sweep the small stuff into a closing "Also
  in this release".
- Plain markdown, no HTML — it renders through a markdown pipeline on every
  surface.
- Short. A note nobody finishes reading is a note nobody read.

## Rules

- Say which mode you are in, and why, before reading anything else.
- One note per released version. Never rewrite a shipped or published note's
  history — a correction goes in the next note.
- Don't file a note for a routine patch deploy with nothing user-visible.
  Notes are for releases worth announcing.
- If this session is already a `/maestro-build` or `/maestro-vibe` close-out,
  fold the note into that flow rather than running a separate ceremony.

## Forbidden

- Writing a release-note file into a repo that is not mode A. Mode B files to
  Maestro and writes nothing to disk.
- Filing Maestro's own notes over MCP. Mode A is files in the package; the
  project release tools are for other projects.
- Writing a note from commit subjects without reading the diffs.
- Asking the user to write the words. You draft; they approve.
- Publishing as the tail of drafting — `publish_release` needs its own yes.
- Falling back to writing a file when Maestro is unreachable in mode B. Say so
  and stop.
- Accepting a version the user proposes when the versioning gate has not
  passed. A number somebody picked is not a release.
- Setting versioning up as a side effect of being asked for a release note.
  It is its own change and needs its own yes.
