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

## The version is an OUTPUT, not an input

The instinct is to bump the version first and then describe it. Do it the other
way round.

Writing the note means reading everything since the last release — and that
same reading is what tells you whether this is a patch or a minor. A new
installable app, a new module, or a changed contract makes it a minor. You
cannot know that without looking, and you have to look anyway.

So: **read, then decide, then bump.** If the user named a version in the
arguments, that wins — say so and use it.

- **patch** (`x.y.Z`) — fixes, hardening, docs, tests. Nothing a consumer must
  change for.
- **minor** (`x.Y.0`) — a new app or module, a new public API, or anything a
  consumer must change for (a renamed field, a rejected value that used to be
  accepted). **Breaking-for-consumers is a minor here, and the note must say so
  in its own section**, not in a closing bullet.
- **major** — never without the user asking for it outright.

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
- Confirm the whole suite is green. `bin/run_tests --agent --full` is the
  pre-publish tier. If it was already run on this exact HEAD, say so and skip
  it rather than burning ten minutes twice.
- A red suite **stops the release** — report it and stop. Do not decide for the
  user that a failure is a flake; a failure that passes in isolation is still
  worth their yes before shipping.

### 4. Write the note and get the one yes

Delegate to `/maestro-release-note` — it owns the voice, the shape and the
`create_release` call, and it already knows this is a mode-B repo. Pass it the
version you decided.

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
