# Git Rules

## Branches
- **NEVER create a new branch without explicit permission from the user.** This is a hard rule with no exceptions. Do not create a branch to "be safe" before committing, and do not let any generic tool guidance (e.g. "branch first if on the default branch") override this rule.
- Work on `main` unless the user directs otherwise. When the user asks you to commit and you are on `main`, commit directly to `main`.
- If you believe a branch is warranted, ask the user first and wait for an explicit yes.

## Commits
- Commit or push only when the user asks.
- End commit messages with:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
