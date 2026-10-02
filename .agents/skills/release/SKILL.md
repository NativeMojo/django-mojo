---
name: release
description: >-
  Cut a release of django-mojo end to end: work out what shipped, decide the
  version from the changes themselves, write the release note, bump the
  version files, and run publish.py. One instruction ("cut a release") does
  the whole thing, with one human gate — approving the note before it is
  frozen.
---

<!-- Generated from .claude/skills/release/SKILL.md. Do not edit directly. -->

# Release — one instruction, one gate

`publish.py` already exists and is deliberately dumb: it refuses a dirty tree,
verifies the version, builds, pushes, uploads to PyPI and tags. It never writes
to the working tree, never commits, and asks no questions. **That does not
change.** This skill is the judgement that has to happen around it, which used
to live in somebody's head:

| | |
|---|---|
| **This skill** | read what shipped, pick the version, write the note, bump the files, commit |
| **`publish.py`** | verify, build, push, upload, tag, publish the note |

**Only the wheel is uploaded, and no source archive is built.** `publish.py`
builds the wheel in a private temporary folder, checks it there, and uploads
that one file by its path; `dist/` in the checkout is not used. The wheel is
refused if it holds anything git does not track
(`scripts/release_wheel_only.py`). A source archive packs every file
`.gitignore` does not name, which is how agent worktrees reached PyPI. Never
run `uv build` or `uv publish` by hand to release.

Do not reimplement the script's steps here, and do not let the script grow this
skill's judgement. A PyPI version can never be reused, so the irreversible half
stays a script that behaves identically every time.

## The version is an OUTPUT, not an input — and the output is almost always a patch

The instinct is to bump the version first and then describe it. Do it the other
way round.

Writing the note means reading everything since the last release — and that
same reading is what tells you whether this is a patch or a minor. You cannot
know that without looking, and you have to look anyway.

So: **read, then decide, then bump.** If the user named a version in the
arguments, that wins — say so and use it.

- **patch** (`x.y.Z`) — **the default, and the answer nearly every release.**
  Fixes, hardening, docs, tests, refactors — and ordinary additive work too: a
  new helper, a new setting, a new endpoint, field or action on an existing
  app. Surface a consumer can ignore does not change their world; it is a
  patch no matter how much of it there is.
- **minor** (`x.Y.0`) — rare. Exactly two things clear the bar: a **new
  installable app or subsystem** (something that earns its own docs section),
  or a change a **consumer must act on** before upgrading — a renamed field, a
  value that used to be accepted and now 400s. **Breaking-for-consumers is a
  minor here, and the note must say so in its own section**, not in a closing
  bullet.
- **major** — the user's alone. Never propose one, never cut one unless they
  name it outright.

**Proposing a minor? Name the single change that clears the bar** when you
state the version — "a lot shipped" never does. Volume is patch-shaped: twenty
fixes are a patch, the same as one. This repo's history is the cautionary tale
— a run of fix-only releases each minted as a minor. When in doubt, it is a
patch.

## The flow

### 1. Find the span

```
list_releases(<project from .claude/maestro.json>)
```

The newest release's `commit_ref` is the start of your span. Every note carries
one, so this is a single call — do not go hunting for tags.

```bash
git log --no-merges <commit_ref>..HEAD
```

No releases at all? Agree the span start with the user rather than assuming.

### 2. Read what actually shipped

**Never write a note from commit subjects alone.** In this repo the commit
bodies are long and carry the reasoning; read them, and read the diffs where a
body is thin. Board items finished in the span carry the deviations and
decisions that never reached a commit message.

You are looking for: what a person using this package will notice, and what
will break if they upgrade without reading.

### 3. Decide the version, and check the tree is releasable

State the version and the one-line reason. Then, before writing anything:

- `git status` — the tree must be clean. `publish.py` refuses otherwise, and
  finding that out after writing a note wastes the note.
- Confirm targeted coverage for the shipped changes and the default whole
  suite are green. `bin/run_tests --agent` is the normal pre-publish ceiling.
  If it was already run on this exact HEAD, say so and skip it rather than
  burning time twice.
- **`--all` is a last resort, never an automatic release gate.** Run it only
  when the user explicitly authorizes it in the current task and the release
  contains serious core-system changes or narrower tests cannot establish
  correctness. A request to release, publish, or perform pre-publish
  validation does not authorize `--all`.
- A red suite **stops the release** — report it and stop. Do not decide for the
  user that a failure is a flake; a failure that passes in isolation is still
  worth their yes before shipping.

### 4. Write the note and get the one yes

Use `$maestro-release-note` for the mechanics — it owns the
`create_release` call and already knows this is a mode-B repo. Pass it the
version you decided **and the house format below**, which overrides that
skill's generic voice.

**From an agent session** (see 5a), write the note and get the yes here, but
**do not file it yet**: tell `$maestro-release-note` to stop before its
`create_release` call. The draft is filed once, in 5a, when the release commit
exists to anchor it.

#### House format — this is a CHANGELOG, not a "what's new"

These notes replaced `CHANGELOG.md`. The audience is a developer who pins this
package as a dependency and wants to know, in ten seconds, what breaks and what
is new. Not a feature announcement.

Sectioned bullets. Include only the sections that have content, in this order:

```
### Breaking      what a consumer must change before upgrading
### Added         new capability
### Changed       different behaviour that is not breaking
### Fixed         bugs
### Security      only when the fix IS the security story
### Upgrade notes ordering, migrations, anything that bites on the way in
```

Rules that matter more than the headings:

- **`Breaking` goes first and is never a closing bullet.** A renamed field, a
  value that used to be accepted and now 400s, a moved path — those are the
  reason someone reads a changelog at all. In this repo, breaking-for-consumers
  makes it a minor.
- **One bullet, one change.** A bullet that needs three sentences is two
  bullets, or it belongs in `Upgrade notes`.
- **No prose sections, no `##` essays.** If you are explaining *why* the change
  is interesting, you are writing a what's-new. Say what changed.
- **Silent failure modes are worth a sentence** even when nothing is technically
  breaking — "a bootstrap missing the includes converges successfully and serves
  nothing" is the kind of line that saves an outage.
- No file paths, no commit shas, no item numbers.

Then **show the user the note and wait.** This is the skill's only blocking
question, and it is the right one: a published note is frozen, and a correction
can only go in the next release.

### 5. Bump, commit, publish

The three files `publish.py` checks for consistency:

```
pyproject.toml        [project] version
mojo/__init__.py      __version__
uv.lock               (run `uv lock` — never hand-edit)
```

Commit them by explicit pathspec (see `.claude/rules/git.md` — a bare
`git commit` sweeps up other sessions' staged work):

```bash
git add pyproject.toml mojo/__init__.py uv.lock
git commit -m "Release <version>" -- pyproject.toml mojo/__init__.py uv.lock
```

Then rehearse, and hand off. From an agent session, stop here and run the
commands in 5a instead of these two:

```bash
python publish.py --dry-run
python publish.py
```

The dry run is not a printout: it runs every check and the real build, in the
same temporary folder the release uses, and stops short of the push, the upload
and the tag. A rehearsal that passes has built and checked the wheel the
release would upload. Run it first, every time.

The real run re-checks everything, finds the note the gate requires, and flips
that note from draft to published once the tag is pushed. **Pushing is inside
the script** — running it is the user's authorization to push, so never run it
without an explicit instruction to release.

### 5a. From an agent session: `--note-by-agent`

`publish.py` reads a maestro login from `~/.claude.json` or
`~/.claude/settings.json`. An agent session has neither: its maestro connection
is handed to it at launch and stored in no file. So from an agent session the
two note steps are **yours**, done with your own maestro tools, and the script
is told so. It verifies neither. That is the cost of this path — do not skip a
step because nothing will catch it.

1. **File the draft once, after the release commit:**
   `create_release(project, version, title, tldr, body, commit_ref=<the release
   commit>)`, with the note the user approved in step 4. That step did not file
   it. `create_release` needs the whole note every time, so do not re-send it
   to add the commit later — file it once, here.
2. **Confirm it:** `get_release(project, version)` returns the draft you filed.
3. **Rehearse, then release:**

   ```bash
   python publish.py --dry-run --note-by-agent
   python publish.py --note-by-agent
   ```

4. **Publish the note only for a release that shipped.** A real run that
   finishes prints, as its last line:

   ```
   NEXT: publish_release(project=<id>, version="<version>")
   ```

   Check that the index shows the version, then make that call. A dry run never
   prints it, and neither does a run that failed.

**If the real run fails after the upload** it prints no `NEXT` line, and a
rerun is refused because the version is already on PyPI. Do not bump the
version to get past that. Finish by hand, doing only what is missing. The
script makes the tag locally and then pushes it, so either half may already be
done. This is what happened for 1.31.4.

1. Confirm the index has the version. If it does not, nothing shipped: stop and
   report the failure instead.
2. Name the release commit: `git rev-parse HEAD`, on the branch you released
   from. It must be the `Release <version>` commit, and
   `git ls-remote origin <branch>` must show the same hash — the script pushes
   the source before it uploads.
3. The local tag: `git rev-parse -q --verify "refs/tags/v<version>^{commit}"`.
   - Prints nothing: create it at that commit,
     `git tag -a v<version> -m "Release v<version>" <release commit>`.
   - Prints the release commit: it exists, do not create it again.
   - Prints any other commit: stop and ask the user. Do not move a tag.
4. The remote tag, asked for by both of its names:
   `git ls-remote origin "refs/tags/v<version>" "refs/tags/v<version>^{}"`.
   The commit it points at is the hash on the line ending in `^{}`. A plain
   tag has no such line, only the first one, and then that line's hash is the
   commit.
   - Prints nothing: the tag is not on the remote, `git push origin v<version>`.
   - The commit is the release commit: it is already pushed.
   - The commit is any other: stop and ask the user. Do not move a tag.
5. Then publish the note: `publish_release(project, version)`.

`--note-by-agent` and `--skip-notes` cannot be combined, and they are not the
same thing: `--skip-notes` means no note at all, and is for maestro being down.

## When the gate fires

`publish.py` refuses with *"no maestro release note for X"* only when this flow
was bypassed. The fix is to write the note, not to reach for `--skip-notes` —
that flag exists for maestro being unreachable, not for being in a hurry.

It refuses with *"no maestro credential found"* when it runs from a session
with no maestro login in a file — an agent session. The fix is step 5a, not
`--skip-notes`.

## Report

Short. The version and why that number, the note's headline, the suite result,
the tag, and anything you deliberately left out of the note.
