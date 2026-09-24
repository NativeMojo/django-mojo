# Git Rules

## Publish Every Task to GitHub

**Standing user authorization, 2026-09-24: always push this repo's work to
GitHub. Do not ask again.** This supersedes older no-push/opt-in instructions
in repo docs, skills, and workspace conventions. A later explicit user request
to keep a particular task local is the exception.

- Push each task commit to its own remote branch promptly (`git push -u origin
  HEAD`) so interrupted work is backed up. Stage only the task's intended files;
  never include secrets, generated local state, or another session's work.
- If unsure whether work is ready for `main`, push the task branch immediately
  anyway. Uncertainty about merging is never a reason to leave commits local.
- When verification and review are complete, merge into `main` and run
  `git push origin main` **before marking the task done or deleting its worktree**.
- Fetch and verify that the completed commit is reachable from `origin/main`;
  report the pushed commit in the final response. A local commit or merge alone
  is not completion.
- If a PR is the appropriate delivery route (including branch protection), push
  the task branch and open a PR, then report its URL and that merge is pending.
  Never leave completed work only in a working tree or local branch.
- If pushing fails, retain the branch/worktree, report the exact blocker, and
  keep the task open. On a non-fast-forward rejection, fetch and integrate the
  remote changes without discarding anyone's commits, verify affected work, and
  retry. Never force-push `main`.

## Branches & Worktrees
- Every code build uses a dedicated `codex/<item>` branch in its own Git
  worktree. Never edit from the primary `main` checkout or share a checkout
  between concurrent builds.
- Keep the primary checkout on `main` for integration. After scoped
  verification is green, merge the completed branch into `main` and push it.
- Cleanup is part of done: verify the branch is merged and published, remove that exact
  worktree, delete that exact merged local branch, run
  `uv run python testit/testenv.py prune` and `git worktree prune`, then
  confirm neither remains. Never bulk-delete worktrees or branches owned by
  other sessions.

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
  push it, then merge and push `main` and perform the mandatory cleanup above.
  Stage specific files by name — never `git add -A` / `.`.
- **Commit by explicit pathspec — never bare `git commit`.** Concurrent sessions
  share this working tree and stage planning moves (`git mv` via the helper
  scripts) at any moment; a bare commit sweeps their staged index state into
  your commit. Always `git add <exact files> && git commit -m "..." -- <same files>`,
  and never pass a directory as the pathspec.
- End commit messages with a trailer naming the model that actually authored the
  commit — for delegate/fanout builds that's the **builder's** model, not the
  orchestrator's:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
  `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`
