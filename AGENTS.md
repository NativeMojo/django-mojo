# Django-MOJO Agent Guide

This file is loaded automatically by ChatGPT and Codex. `CLAUDE.md` remains
the detailed project handbook shared with Claude Code; read it in full at the
start of a task, then apply the compatibility notes below.

## Start Every Task

1. Read `CLAUDE.md` and `memory.md`.
2. Use the Maestro board in `.claude/maestro.json` as the work record. The
   `.claude` directory name is legacy; its board config and project rules are
   shared by every coding agent.
3. Select the smallest matching repo skill:
   - File tracked work: `$maestro-task`
   - Investigate and plan an item: `$maestro-scope`
   - Build a planned item: `$maestro-build`
   - Scope and build a batch behind one approval gate: `$maestro-auto`
   - Make a small, low-risk, single-session change: `$maestro-vibe`
   - Draft the next release note from shipped diffs: `$maestro-release-note`
   - Visually verify a deployed Maestro site: `$sites-verify`
4. If Maestro is unavailable or unauthenticated, say so explicitly. Do not
   silently switch to the file-backed fallback workflow.
5. Before building, read `docs/django_developer/README.md` and check
   `mojo/helpers/` for existing utilities.

## Shared Rules

The files under `.claude/rules/` are provider-neutral project rules despite
their legacy path. Read the applicable files before editing:

- Always: `core.md`, `git.md`, and `docs.md`.
- Tracked builds: `build-baseline.md`.
- Python framework changes: `performance.md`.
- Model, REST, and test changes: `models.md`, `rest.md`, and `testing.md`
  respectively.

Follow their security, testing, documentation, WIP, explicit-pathspec commit,
and no-push requirements. One exception is provider identity: never use a
Claude co-author trailer for work authored by ChatGPT or Codex. Use
`Co-Authored-By: OpenAI Codex <noreply@openai.com>` instead.

When a Maestro workflow calls for the post-build roles, use the briefs in
`.claude/agents/` as the role instructions. Each code build uses its own
branch and worktree; only one agent or orchestrator runs tests per checkout.
Per-checkout test isolation permits different worktrees to test concurrently.

## Skill Synchronization

The upstream-managed dev skill pack lives under `.claude/skills/` and includes
the `maestro-*` workflows plus `sites-verify`.
ChatGPT and Codex discover generated counterparts in `.agents/skills/`.
Never hand-edit the generated copies. After `get_dev_skills()` refreshes the
Claude sources, run:

```bash
scripts/sync_maestro_skills.py
scripts/sync_maestro_skills.py --check
```

The sync removes Claude-only frontmatter, converts slash-style Maestro skill
mentions to `$skill` mentions, maps provider-specific model wording to Codex
reasoning terminology, and declares the Maestro MCP dependency for ChatGPT.
