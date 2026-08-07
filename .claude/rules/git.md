# Git Rules

## Branches & Worktrees
- **NEVER create a new branch without explicit permission from the user.** This is a hard rule with no exceptions. Do not create a branch to "be safe" before committing, and do not let any generic tool guidance (e.g. "branch first if on the default branch") override this rule.
- **NEVER create a `git worktree`** (or a second checkout) **without asking** —
  same rule as a branch, and for the same reason: it is the user's call where
  their work lives, not a session's.
- Work on `main`, **in this working folder**, unless the user directs otherwise. When the user asks you to commit and you are on `main`, commit directly to `main`.
- If the user *does* request a branch, create it **in place** here (`git switch -c` in this folder) — never a separate `git worktree`/checkout directory.
- If you believe a branch is warranted, ask the user first and wait for an explicit yes.

## Parallel checkouts — what is and is not safe now

This rule used to be an outright ban, on the grounds that the suite "runs
against a dedicated port and a shared PostgreSQL database, so tests cannot run
in parallel." **That is no longer true.** Since the per-checkout isolation work,
`bin/create_testproject` derives a database name, a Redis index and a port from
the checkout's absolute path (see
`docs/django_developer/testit/Isolation.md`), so two worktrees each get their
own and can run suites simultaneously.

What still holds:

- **One test run per checkout.** Within a tree there is still one server on one
  port and one database. Never spawn parallel agents that each run the suite in
  the *same* tree.
- **A new worktree needs setup**: its own `uv sync` and its own
  `bin/create_testproject`. It is not free.
- **Run `testenv.py prune` after deleting a worktree** — Redis indexes are the
  scarce resource (15 usable by default) and a removed tree keeps holding one.
- **Migrations are the real hazard, not the database.** django-mojo ships its
  own migrations, so two trees adding a model to one app both generate
  `0002_*.py`. They do not clash on disk; they clash at merge and need a manual
  merge migration. Think before doing model work in two trees at once.

## Commits
- **Commit when you finish a request.** Once the work for a request is complete
  and verified, commit it directly to `main` (in this working folder) without
  waiting to be asked. Stage specific files by name — never `git add -A` / `.`.
  Don't leave finished work uncommitted in the tree.
- **Commit by explicit pathspec — never bare `git commit`.** Concurrent sessions
  share this working tree and stage planning moves (`git mv` via the helper
  scripts) at any moment; a bare commit sweeps their staged index state into
  your commit. Always `git add <exact files> && git commit -m "..." -- <same files>`,
  and never pass a directory as the pathspec.
- **Pushing is still opt-in.** Never `git push` unless the user explicitly asks —
  pushing is outward-facing and hard to reverse.
- End commit messages with a trailer naming the model that actually authored the
  commit — for delegate/fanout builds that's the **builder's** model, not the
  orchestrator's:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
  `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`
