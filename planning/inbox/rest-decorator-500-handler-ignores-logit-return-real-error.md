---
id:
type: bug
title: REST decorator 500 handler ignores LOGIT_RETURN_REAL_ERROR (leaks str(err) regardless)
priority: P2
effort: S
owner: backend
opened: 2026-06-29
depends_on: []
related: []            # wmwx/docs DD-051 (WMX-side decision record)
links: []
---

# REST decorator 500 handler ignores LOGIT_RETURN_REAL_ERROR (leaks str(err) regardless)

## What & Why

There are **two** places a 500 becomes an HTTP body in django-mojo, and they
disagree on whether the `LOGIT_RETURN_REAL_ERROR` flag is honored:

- `LoggerMiddleware.__call__` (`mojo/middleware/logging.py:92-95`) — **honors** the
  flag: `error = "system error"; if LOGIT_RETURN_REAL_ERROR: error = str(e)`.
- `dispatch_error_handler` (`mojo/decorators/http.py:211`, the `except Exception`
  branch) — **ignores** it: returns `str(err)` **unconditionally**.

The decorator is the **dominant** 500 path — it wraps every `@md` REST view via
`dispatcher`, so a view that raises is caught there first and rarely reaches the
middleware. Net effect: `LOGIT_RETURN_REAL_ERROR` **cannot actually suppress** raw
exception text on the common path. Setting the flag False scrubs only the rare
middleware-level 500; decorator 500s still return internal paths / DSNs / table
names / upstream response bodies. So the flag is, in practice, a no-op for the
surface it most needs to cover.

This was originally attempted from the **wrong session/repo** (the WMX consumer side,
WMX-API-118) — a gate was added to both handlers then **reverted** (django-mojo commit
`b7f20cf`) because a framework change shouldn't be driven from a consuming app. This
item re-homes it in django-mojo to be scoped and applied properly here.

## Acceptance Criteria
- [ ] `dispatch_error_handler`'s 500 branch returns `"system error"` (not `str(err)`)
      when `LOGIT_RETURN_REAL_ERROR` is False, matching `LoggerMiddleware`.
- [ ] The flag is read **live** (`settings.get`) so it tracks the active settings
      profile, not import-time (`get_static`) — apply the same to the middleware,
      which currently caches it at module load.
- [ ] Server-side logging is **unchanged** — the full error + traceback is still
      logged (`logger.exception` + incident report) regardless of the flag.
- [ ] A decision is made + documented on the **framework default**: keep `True`
      (current, back-compat) or flip to `False` (secure-by-default). See open question.
- [ ] Regression test covers both the decorator and middleware 500 paths, both flag
      values.

## Repro — bugs only
1. In any app, add an `@md` view that raises `RuntimeError("dsn=postgres://secret@…")`.
2. Set `LOGIT_RETURN_REAL_ERROR = False`.
3. `GET` the endpoint.
- Expected: 500 body `{"error": "system error", "code": 500, "status": false}`.
- Actual: 500 body `{"error": "dsn=postgres://secret@…", "code": 500, …}` — the flag
  is ignored on this path; the secret leaks.

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes
Investigation (so /scope needn't redo it):
- The reverted gate (`b7f20cf`) is the reference implementation: a small
  `_return_real_error()` helper reading `settings.get("LOGIT_RETURN_REAL_ERROR", True)`,
  used in the 500 branch as `str(err) if _return_real_error() else "system error"`; and
  the middleware switched from the module-level `get_static` constant to a live
  per-error `settings.get`. `git show b7f20cf` recovers the exact diff.
- **Out of scope (note, don't blanket-fix):** the decorator's sibling non-500 `str(e)`
  returns — `PermissionError`→403 (`http.py:180`) and `ValueError`→400 (`http.py:195`),
  plus `rest.py` delete/bulk-create 4xx bodies. Some 4xx text is **intentional client
  feedback**, so a blanket scrub is wrong; if covered at all it needs per-path judgment.
  Keep this item to the 500 catch-alls.

### Open question (framework-owner call)
- **Default value.** Flipping the framework default `True`→`False` is secure-by-default
  but changes behavior for **all** django-mojo consumers (a 500 body that used to carry
  `str(e)` becomes `"system error"`). Keep `True` and let each deployment opt into
  scrubbing, or flip to `False` and let deployments opt into real errors? The WMX
  deployment's own posture is independent and already settled (its prod **intentionally**
  returns real errors — auth-gated; recorded in `wmwx/docs` DD-051); this item is about
  making the flag *work* on both paths + the framework default, not dictating any one
  app's chosen value.

## Resolution
- closed:
- branch:
- files changed:
- tests added:
