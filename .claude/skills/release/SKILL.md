---
name: release
description: >-
  Cut a release of django-mojo end to end: work out what shipped, decide the
  version from the changes themselves, write the release note, bump the version
  files, and run publish.py. One instruction ("cut a release") does the whole
  thing, with one human gate — approving the note before it is frozen.
user-invocable: true
argument-hint: <version to cut (omit — the changes decide it)>
---

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
- **`--full` is a last resort, never an automatic release gate.** Run it only
  when the user explicitly authorizes it in the current task and the release
  contains serious core-system changes or narrower tests cannot establish
  correctness. A request to release, publish, or perform pre-publish
  validation does not authorize `--full`.
- A red suite **stops the release** — report it and stop. Do not decide for the
  user that a failure is a flake; a failure that passes in isolation is still
  worth their yes before shipping.

### 4. Write the note and get the one yes

Delegate to `/maestro-release-note` for the mechanics — it owns the
`create_release` call and already knows this is a mode-B repo. Pass it the
version you decided **and the house format below**, which overrides that
skill's generic voice.

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

Then hand off:

```bash
python publish.py
```

It re-checks everything, finds the note the gate requires, and flips that note
from draft to published once the tag is pushed. **Pushing is inside the script**
— running it is the user's authorization to push, so never run it without an
explicit instruction to release.

## When the gate fires

`publish.py` refuses with *"no maestro release note for X"* only when this flow
was bypassed. The fix is to write the note, not to reach for `--skip-notes` —
that flag exists for maestro being unreachable, not for being in a hurry.

## Report

Short. The version and why that number, the note's headline, the suite result,
the tag, and anything you deliberately left out of the note.
