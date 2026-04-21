# Framework-wide error → incident-event sweep

**Type**: request
**Status**: open
**Date**: 2026-04-20

## Description
Ensure that every unexpected exception raised inside django-mojo framework code — not just REST permission denials, not just the assistant — is reported through the incident-event system (`mojo.apps.incident.report_event`). Today, many `except Exception:` branches only `logger.exception(...)`, which means the user / operator never sees the failure surface outside a log file.

## Motivation
The assistant datetime-serialization bug ([planning/issues/assistant-tool-result-datetime-serialization.md](planning/issues/assistant-tool-result-datetime-serialization.md)) exposed that a class of agent failures was silently bubbling up as a user-visible error string with no corresponding incident row. That makes bugs invisible to ops dashboards and to the admin/audit trail. The user raised this as a general framework expectation: errors must flow through the incident system so users are aware.

## Scope (proposed — refine during /design)
- Audit every `except Exception` / bare `except:` in `mojo/` (excluding vendored code).
- For each catch site, decide:
  - **Raise incident** (unexpected failure in framework flow — default)
  - **Swallow silently** (only if the exception is expected and handled, e.g. optional import fallback)
- Introduce a small helper, e.g. `mojo.helpers.incident.report_exception(exc, *, category, level, request=None, **context)`, so call sites are one line and consistent.
- Standardize incident categories (e.g. `framework:error:<subsystem>`) and levels.

## Acceptance Criteria
- A documented list of every framework-level `except` site and its disposition (incident / intentional swallow).
- A `report_exception` helper in `mojo/helpers/` (or reuse an existing one) that wraps `incident.report_event` with traceback truncation + request auto-resolution.
- At least the high-traffic subsystems (REST dispatch, serializers, assistant, jobs, realtime, auth) updated to use it.
- Docs update: `docs/django_developer/` section explaining the convention so new code follows it.

## Out of Scope
- Changing user-facing error responses or HTTP status codes.
- Rewriting the incident-event schema.
- Performance work on incident logging (separate concern if it surfaces).

## Related
- [planning/issues/assistant-tool-result-datetime-serialization.md](planning/issues/assistant-tool-result-datetime-serialization.md) — the bug that motivated this sweep.

## Known Open Concerns (carry forward)
- **Incident flooding**: `_report_event` has no rate limiting. A deterministically failing tool can emit one incident per invocation per turn; an attacker with assistant access could flood the incident table. Add per-user / per-category rate limiting or dedup with a TTL counter as part of this sweep.
- **Exception-message leakage**: Incident `details` strings include `{exc!r}` and truncated `traceback.format_exc()`. A tool handler that interpolates user input into an exception message (e.g. `raise ValueError(f"Bad query: {user_input}")`) will leak that input into the incident record. Either sanitize incident details to exception type only, or add a code convention enforced by review that tool-handler exceptions must not include raw input. Document in the framework-wide sweep.
