# Use logit instead of import logging

**Type**: bug
**Status**: resolved
**Date**: 2026-03-31
**Severity**: medium

## Description
Multiple files across the codebase use `import logging` and `logging.getLogger()` instead of the framework's own `from mojo.helpers import logit` / `logit.get_logger()`. This means those logs bypass the framework's log routing (var/log files), pretty-printing, and sensitive data masking. Claude Code also keeps introducing `import logging` in new code instead of using logit.

## Context
`logit` is the project's logging system — it routes logs to structured files in `var/log/` (`mojo.log`, `error.log`, `debug.log`), provides pretty-formatting for dicts, and masks sensitive data automatically. Using stdlib `logging` directly bypasses all of this, making logs harder to find and potentially leaking sensitive data.

## Acceptance Criteria
- All `import logging` / `logging.getLogger()` usage in `mojo/apps/` and `mojo/helpers/aws/` is replaced with `from mojo.helpers import logit` and `logit.get_logger(name, filename)`
- `logit.py` itself is the only file that should import stdlib `logging`
- Claude Code rules updated to enforce this convention going forward

## Investigation
**Likely root cause**: No rule in `.claude/rules/` enforcing logit over stdlib logging. Claude defaults to `import logging` because it's the Python standard.
**Confidence**: confirmed
**Code path**: 25 files use `import logging` — concentrated in:
- `mojo/apps/fileman/` (8 files: signals.py, tasks.py, renderer/*.py, management command)
- `mojo/apps/incident/` (5 files: rest/ossec.py, handlers/*.py, models/*.py)
- `mojo/helpers/aws/` (4 files: sns.py, ses.py, iam.py, ec2.py)
- `mojo/apps/account/utils/passkeys.py`
- `mojo/apps/realtime/auth.py`
- `publish.py` (root level, may be intentional)
**Regression test**: not feasible — this is a convention enforcement issue, not a runtime bug
**Related files**:
- `mojo/helpers/logit.py` — the correct logging module
- `.claude/rules/core.md` — needs a logit rule added
- All 25 files listed above need migration

## Resolution

**Status**: resolved
**Date**: 2026-03-31

### What Was Built
Replaced all `import logging` / `logging.getLogger()` with `from mojo.helpers import logit` / `logit.get_logger()` across 20 framework files. Removed dead `import logging` from 4 AWS helpers. Added logit enforcement rule to `.claude/rules/core.md`.

### Files Changed
- `mojo/apps/fileman/signals.py` — migrated to logit
- `mojo/apps/fileman/tasks.py` — migrated to logit
- `mojo/apps/fileman/renderer/__init__.py` — migrated to logit
- `mojo/apps/fileman/renderer/base.py` — migrated to logit
- `mojo/apps/fileman/renderer/utils.py` — migrated to logit
- `mojo/apps/fileman/renderer/image.py` — migrated to logit
- `mojo/apps/fileman/renderer/video.py` — migrated to logit
- `mojo/apps/fileman/renderer/audio.py` — migrated to logit
- `mojo/apps/fileman/renderer/document.py` — migrated to logit
- `mojo/apps/fileman/management/commands/cleanup_expired_uploads.py` — migrated to logit
- `mojo/apps/incident/rest/ossec.py` — migrated to logit
- `mojo/apps/incident/handlers/llm_agent.py` — migrated to logit
- `mojo/apps/incident/handlers/event_handlers.py` — migrated to logit
- `mojo/apps/incident/models/rule.py` — migrated to logit
- `mojo/apps/incident/models/incident.py` — migrated to logit
- `mojo/apps/incident/models/ticket.py` — migrated to logit (had both, removed dead import)
- `mojo/apps/account/utils/passkeys.py` — migrated to logit
- `mojo/apps/realtime/auth.py` — migrated to logit
- `mojo/helpers/aws/sns.py` — removed dead `import logging`
- `mojo/helpers/aws/ses.py` — removed dead `import logging`
- `mojo/helpers/aws/iam.py` — removed dead `import logging`
- `mojo/helpers/aws/ec2.py` — removed dead `import logging`
- `.claude/rules/core.md` — added logit enforcement rule
- `mojo/apps/fileman/README.md` — fixed logging example

### Tests
- No new tests — mechanical import refactor
- Full suite: 1023 passed, 39 skipped, 0 failed

### Docs Updated
- `mojo/apps/fileman/README.md` — fixed debug logging example

### Security Review
- Positive security impact: logit provides automatic sensitive data masking

### Follow-up
- `publish.py` still uses `import logging` — intentionally excluded (standalone CLI script, runs outside Django)
