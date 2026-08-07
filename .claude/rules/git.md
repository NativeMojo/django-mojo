# Git Rules

## Branches & Worktrees
- Every code build uses a dedicated `codex/<item>` branch in its own Git
  worktree. Never edit from the primary `main` checkout or share a checkout
  between concurrent builds.
- Keep the primary checkout on `main` for integration. After scoped
  verification is green, merge the completed branch into local `main`.
- Cleanup is part of done: verify the branch is merged, remove that exact
  worktree, delete that exact merged local branch, run
  `uv run python testit/testenv.py prune` and `git worktree prune`, then
  confirm neither remains. Never bulk-delete worktrees or branches owned by
  other sessions.
- Pushing remains opt-in. A local merge into `main` does not authorize a push.

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
- **Run `testenv.py prune` after deleting every worktree** — Redis indexes are
  scarce (15 usable by default), and a removed tree keeps holding one.
- **Migrations are the real hazard, not the database.** django-mojo ships its
  own migrations, so two trees adding a model to one app both generate
  `0002_*.py`. They do not clash on disk; they clash at merge and need a manual
  merge migration. Think before doing model work in two trees at once.

## Commits
- **Commit when you finish a request.** Commit verified work on its item branch,
  then merge it into local `main` and perform the mandatory cleanup above.
  Stage specific files by name — never `git add -A` / `.`.
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
