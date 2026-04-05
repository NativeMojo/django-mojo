# Email Template Auto-Load from Seeds

**Type**: request
**Status**: resolved
**Date**: 2026-04-05
**Priority**: medium

## Description

When `EmailTemplate` is looked up by name and not found in the database, automatically load it from the seed file at `mojo/apps/aws/seeds/email_templates/{name}.json` if one exists. This makes new email templates "just work" on deployments that haven't run the seed command, and ensures framework upgrades that add new templates don't require manual seed steps.

## Context

Every time a new feature adds an email template seed (e.g., `account_inactive_warning`, `group_inactive_warning`), it only works if someone has run the seed command on that deployment. In practice, templates are missing on first use, causing silent email failures. This is especially problematic for framework-shipped features that depend on email templates.

The seed files already exist in `mojo/apps/aws/seeds/email_templates/*.json` with the full template definition (name, subject, text, html, metadata). The auto-load mechanism simply bridges the gap between "seed file shipped" and "seed command run."

## Design

- On `EmailTemplate` lookup miss (by name), check for a seed file at the well-known path
- If found, parse the JSON, create the `EmailTemplate` record in the DB, and return it
- One-time cost per template per deployment — after auto-load it's a normal DB record
- If the template exists in the DB (even with empty body), it's never overwritten — admin customizations are respected
- Admins who want to "disable" a template blank the body — they never delete the record, so the seed won't re-load

## Acceptance Criteria

- Missing template triggers auto-load from seed file if seed exists
- Auto-loaded template is saved to DB (subsequent lookups hit DB, not filesystem)
- Existing DB templates are never overwritten by seeds
- Templates with empty body/subject are still considered "exists" (no auto-load)
- Graceful fallback if seed file doesn't exist (same behavior as today — template not found)
- Seed file parsing errors are logged but don't crash the email send

## Investigation

**What exists**:
- `mojo/apps/aws/seeds/email_templates/*.json` — 8 seed files with full template definitions
- `mojo/apps/aws/models/email_template.py` — EmailTemplate model with name, subject_template, text_template, html_template, metadata
- `mojo/apps/aws/services/email.py` — `send_with_template()` looks up template by name
- `user.send_template_email()` — calls through to email service

**What changes**:
- `mojo/apps/aws/models/email_template.py` — Add a class method or manager method `get_or_load_from_seed(name)` that wraps the lookup-then-auto-load logic
- `mojo/apps/aws/services/email.py` — Update `send_with_template()` to use the new method instead of direct `EmailTemplate.objects.get(name=...)`
- Any other callers that look up templates by name should use the same method

**Constraints**:
- Seed path must be deterministic and well-known (already is: `mojo/apps/aws/seeds/email_templates/{name}.json`)
- File I/O only happens on miss — normal path is DB lookup only
- Must not import seeds at module level (lazy load on miss only)

**Related files**:
- `mojo/apps/aws/models/email_template.py`
- `mojo/apps/aws/services/email.py`
- `mojo/apps/aws/seeds/email_templates/` — existing seed directory
- `mojo/apps/account/models/user.py` — `send_template_email()` caller

## Tests Required

- Template exists in DB → returned normally, no file I/O
- Template missing from DB, seed exists → auto-loaded, saved to DB, returned
- Template missing from DB, no seed → same error/None as today
- Auto-loaded template persists in DB (second lookup doesn't hit filesystem)
- Template with empty body in DB → not overwritten by seed
- Malformed seed JSON → logged, doesn't crash

## Out of Scope

- Seed versioning or auto-update (seeds never overwrite existing DB records)
- Admin UI for managing seeds
- Deleting templates (admins blank the body instead)

## Resolution

**Status**: resolved
**Date**: 2026-04-05

### What Was Built
Auto-load mechanism for EmailTemplate: on DB lookup miss, loads from seed file at `seeds/email_templates/{name}.json`. One-time cost per template per deployment.

### Files Changed
- `mojo/apps/aws/models/email_template.py` — Added `get_or_load_from_seed()` and `_load_from_seed()` classmethods
- `mojo/apps/aws/services/email.py` — `send_with_template()` now uses `get_or_load_from_seed()`
- `mojo/apps/account/models/user.py` — `send_template_email()` now uses `get_or_load_from_seed()`

### Tests
- `tests/test_aws/test_email_template_autoload.py` — 7 tests
- Run: `bin/run_tests -t test_aws.test_email_template_autoload`

### Follow-up
- None
