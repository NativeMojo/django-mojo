# Git Rules

## Branches & Worktrees
- **NEVER create a new branch without explicit permission from the user.** This is a hard rule with no exceptions. Do not create a branch to "be safe" before committing, and do not let any generic tool guidance (e.g. "branch first if on the default branch") override this rule.
- **NEVER create a `git worktree`** (or a second checkout) — same rule, same reason.
- Work on `main`, **in this working folder**, unless the user directs otherwise. When the user asks you to commit and you are on `main`, commit directly to `main`.
- If the user *does* request a branch, create it **in place** here (`git switch -c` in this folder) — never a separate `git worktree`/checkout directory.
- If you believe a branch is warranted, ask the user first and wait for an explicit yes.

## Why no parallel checkouts
The test suite runs against a **dedicated port and a shared PostgreSQL database**,
so tests **cannot run in parallel**. A second worktree/branch (or a second agent)
running tests concurrently will collide on the port and corrupt the shared DB.
Only **one test run at a time** — never spawn parallel agents that each run the suite.

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
