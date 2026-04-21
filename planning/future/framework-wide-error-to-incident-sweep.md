# Framework-wide error → incident-event sweep (non-REST paths)

**Type**: request
**Status**: open
**Date**: 2026-04-20

## Context: what already works

The REST entry point is **not** the gap. Every request routed through `@md.URL` is wrapped by `rest_request` in [mojo/decorators/http.py:138](mojo/decorators/http.py:138), which catches any unhandled `Exception`, logs it, and calls `MojoModel.class_report_incident_for_user(...)` with traceback + request context. `PermissionDenied` and `ValueError` have dedicated branches. The `MOJO_REST_EVENTS_ON_ERRORS` setting gates incident emission but is on by default.

Two conventions for reporting exist today:

- `mojo.apps.incident.reporter.report_event(details, title, category, level, request, scope, **kwargs)` — the raw API.
- `MojoModel.report_incident / class_report_incident / class_report_incident_for_user` in [mojo/models/rest.py:1289](mojo/models/rest.py:1289) — model-scoped wrappers used throughout `account/`, `assistant/`, `realtime/`.
- `assistant.services.agent._report_event` ([mojo/apps/assistant/services/agent.py:68](mojo/apps/assistant/services/agent.py:68)) — a private one-liner that swallows reporter failures. Used ~15x across the assistant subsystem.

Incident reporting is used in 80+ call sites across account (tokens, totp, sms, verify, passkeys, oauth), assistant, realtime, incident/asyncjobs, aws/ossec, and the rest/http decorators themselves. This sweep is not about retrofitting the REST path — it's about the edges REST doesn't cover.

## Real gaps

1. **Non-REST entry points** have no blanket wrapper:
   - `jobs/daemon.py`, `jobs/job_engine.py`, `jobs/scheduler.py`, `jobs/manager.py` — background workers catch exceptions and `pass` or `logger.error` without emitting incidents.
   - `realtime/handler.py` inner loops — some paths report, many `except` branches only log.
   - Django signal handlers and `POST_SAVE_ACTIONS` triggered via `on_rest_request` — a failure in a post-save action bubbles back into the REST wrapper, but async/threaded dispatch does not.
   - Custom management commands, `asyncjobs` outside the incident app, and long-running services.

2. **Silent `pass` swallows inside REST-wrapped code** — the outer wrapper catches what propagates. Catches that swallow (`except Exception: pass`) inside handlers hide bugs from the wrapper too. The rate-limit decorator at [mojo/decorators/limits.py:39](mojo/decorators/limits.py:39) is a canonical example: its incident emit is itself wrapped in `except Exception: pass`.

3. **Inconsistent helper usage** — the assistant has a good private helper (`_report_event`) that safely wraps the reporter. Jobs, realtime, and serializers open-code the `try: from mojo.apps.incident import report_event; report_event(...) except Exception: pass` pattern, or skip reporting entirely.

## Scope

- **Promote a shared helper** `mojo.helpers.incident.report_exception(exc, *, category, level=5, request=None, user=None, **context)` modeled on the assistant's `_report_event`. One-line call sites, never raises, auto-resolves traceback + user context.
- **Audit and fix non-REST entry points** — jobs, realtime inner handlers, async job workers, signal handlers. Every unexpected `except Exception` at a process/thread boundary should emit an incident.
- **Triage silent swallows** inside wrapped code — classify each as "expected fallback" (keep silent, add `# expected: ...` comment) or "hidden failure" (add `report_exception`).
- **Standardize categories**: `<subsystem>:error:<kind>` (e.g. `jobs:error:worker`, `realtime:error:dispatch`). Document in `docs/django_developer/`.

## Acceptance Criteria

- `mojo.helpers.incident.report_exception` helper exists and is used by jobs, realtime, serializers, and assistant (assistant's `_report_event` either replaced or re-implemented in terms of it).
- Every non-REST process/thread boundary in `mojo/apps/jobs/`, `mojo/apps/realtime/`, and async job workers emits an incident on unexpected exceptions.
- Silent `except: pass` sites are audited — each one either has a one-word justification comment or a `report_exception` call.
- `docs/django_developer/` has a section on the incident-reporting convention and when to use `report_exception` vs `report_incident` on a model vs `report_event` directly.

## Out of Scope

- Re-wrapping REST views — already handled by [mojo/decorators/http.py](mojo/decorators/http.py).
- Changing user-facing HTTP error responses.
- Rewriting the Event schema or storage.
- Performance work on incident logging.

## Related

- [planning/done/assistant-tool-result-datetime-serialization.md](planning/done/assistant-tool-result-datetime-serialization.md) — original motivating bug (assistant silently swallowed a serialization error; the REST wrapper didn't see it because the assistant catches internally).

## Known Open Concerns

- **Incident flooding**: the reporter has no rate limiting. A deterministically failing background job can emit one incident per tick. Add per-category dedup (TTL counter keyed on `category + exc_type + location`) in `report_exception`, not in every call site.
- **Exception-message PII leakage**: `f"Bad query: {user_input}"` exceptions leak input into incident details. `report_exception` should default to `type(exc).__name__ + truncated traceback` and require explicit opt-in to include `str(exc)`. Document the convention.
- **Log-only vs incident-worthy**: not every caught exception deserves an incident. Helper should take `level=` and callers should pick level ≤2 for "log but don't page", ≥5 for "real problem". Document the severity ladder.
