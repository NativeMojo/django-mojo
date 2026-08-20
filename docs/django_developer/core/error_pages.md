# Error Pages

django-mojo ships six styled HTML pages — **400**, **403**, **404**, **500**, **503**
and an **unconfigured root** (200). Any project on a recent django-mojo gets all of
them by upgrading; there is nothing to copy and nothing to configure.

They exist because every failure used to return raw JSON regardless of who asked. A
person who mistyped a URL saw `{"error":"Endpoint not found","code":404,...}`, and a
fresh install greeted its first operator with an unstyled `Permission Denied`.

**API clients are unaffected.** That is the point of the feature, not a footnote —
see [Content negotiation](#content-negotiation).

---

## What ships

| State | Headline | Shown when |
|---|---|---|
| 400 | We couldn't read that request | A malformed request the dispatcher rejects |
| 403 | You don't have access to this | `PermissionDeniedException`, `PermissionError` |
| 404 | That page doesn't exist | Unknown endpoint, unresolved path |
| 500 | Something went wrong on our end | Any unhandled exception |
| 503 | Back in a few minutes | Maintenance windows (rendered by the caller) |
| root | Nothing is published here yet | A project with no public site at `/` |

The pages are rendered in the framework's own auth identity — the same palette,
serif headline and 24px card as the hosted login pages (`mojo-auth-theme.css`) — and
follow the visitor's system light/dark preference.

Every page is **entirely self-contained**: no stylesheet link, no font link, no
script, no image. An error page that needs a healthy static pipeline or a reachable
CDN fails exactly when it is needed. The palette is therefore copied into the shipped
shell as literal values rather than imported; if the auth tokens ever move, move
these by hand too.

### The wordmark

Each page shows the project name read from `AUTH_CONFIG.theme.app_title` — the same
setting the hosted auth pages use, resolved through
`mojo.apps.account.services.auth_config`. Set it once and login, registration and
every error page agree:

```python
AUTH_CONFIG = {"theme": {"app_title": "ACME PLATFORM"}}
```

Unset, the pages simply render without a wordmark. Reading it never raises and never
blocks — an error page that dies reading configuration is worse than one with no
name on it.

---

## Content negotiation

`mojo.helpers.error_pages.prefers_html(request)` decides, and it is deliberately
strict. In order:

1. **No `Accept` header at all** → JSON. Nothing asked for a page.
2. **Any JSON media type at q>0** (`application/json`, `text/json`, `*+json`) → JSON,
   decisively, even when `text/html` is listed alongside it.
3. **`text/html` (or `application/xhtml+xml`) at a quality strictly greater than the
   one offered for `*/*`** → HTML.
4. **Anything else** → JSON.

So:

| `Accept` | Response |
|---|---|
| `text/html,application/xhtml+xml,...,*/*;q=0.8` (a browser) | HTML page |
| `text/html` | HTML page |
| `*/*` (curl, `requests`, most monitors) | JSON |
| *(header absent)* | JSON |
| `application/json, text/plain, */*` (axios default) | JSON |
| `text/html,application/json` | JSON |
| `text/html,*/*` (equal quality, no preference) | JSON |

`*/*` is a wildcard, not a preference — that is why it stays on JSON. A browser
always downweights the wildcard, which is what separates it from an HTTP library.

For any status that does have a page, the JSON branch returns **byte-for-byte the
body it returned before this feature existed**. The two negotiation tests in
`tests/test_error_pages/negotiation.py` assert exactly that by comparing against a
`JsonResponse` built from the same payload in the same process.

### Statuses with no page

Only the statuses in `error_pages.PAGES` render HTML. Everything else — 401, 408,
440 (step-up re-auth), 429 — falls through to JSON for every caller. Inventing a page
for those would put the wrong words in front of the user.

---

## `MOJO_APP_STATUS_200_ON_ERROR` is a separate axis

That setting folds error responses to HTTP 200 for API clients that cannot handle
error statuses. It applies to **the JSON branch only**. The HTML page always carries
the true status code, because a browser, a crawler and an uptime monitor all need
the real one.

```
MOJO_APP_STATUS_200_ON_ERROR = True

  curl  -H 'Accept: */*'        →  HTTP 200, {"error": ..., "code": 403, ...}
  browser                       →  HTTP 403, the styled 403 page
```

---

## The 500 page shows only a reference

When an unhandled exception reaches the dispatcher, the framework files an incident
(`class_report_incident_for_user`) carrying the stack trace, the request path and
the request data. The page shows **the id of that incident and nothing else**:

```
REF · 48213
```

No exception text. No stack. No request path. No internal names. All of it is
already on the incident record, which is access-controlled; the page's job is to
turn "it broke" into a traceable ticket. Look the reference up as
`mojo.apps.incident.models.Event` id `48213`.

If incident reporting is off (`EVENTS_ON_ERRORS = False`) or the report itself
failed, the page renders **without** a reference rather than inventing one.

Do not add detail to this page.

## The 403 page never confirms existence

The 403 copy reveals exactly as much as the 404 copy: nothing. It names no resource,
no path, no permission and no id, and it carries no incident reference. A 403 that
distinguishes itself from a 404 is free reconnaissance for anyone probing the API.
Keep it that way when editing.

---

## Overriding a page in your project

Resolution order for state `<name>`:

1. **`errors/<name>`** through Django's normal template loaders. Drop
   `templates/errors/404.html` into any installed app and it wins.
2. **`mojo/errors/<name>`** through the normal loaders — a second override hook.
3. The shipped page, loaded straight off disk through a private filesystem engine.

So the shortest path to your own 404 is:

```
myapp/
  templates/
    errors/
      404.html      # wins over the framework's page
```

The same works for `400.html`, `403.html`, `500.html`, `503.html` and `root.html`.
Your template receives `brand_name` and (on 500) `reference`.

Step 3 is what makes the pages work with no configuration at all: the framework's
own copies need neither `APP_DIRS` nor an entry in `INSTALLED_APPS` nor a healthy
app registry to render.

### Extending the shipped shell

An override resolved through the normal loaders cannot `{% extends "mojo/errors/_base.html" %}`
unless the framework's template root is also on the search path, because
`mojo/templates/` belongs to no installed app. Add it if you want the shell:

```python
import os, mojo

TEMPLATES[0]["DIRS"].append(
    os.path.join(os.path.dirname(mojo.__file__), "templates"))
```

`mojo.helpers.error_pages.TEMPLATE_DIR` is the same path.

---

## Wiring in a project URLconf

The dispatcher seams (`mojo/decorators/http.py`) and the outer 500 in
`mojo.middleware.logging` are wired inside the framework — nothing to do there.
Django's own handlers and the root page belong to the project:

```python
from django.urls import path, include

from mojo.helpers import error_pages

urlpatterns = [
    path("", error_pages.render_root_page),
    path("", include("mojo.urls")),
]


def handler400(request, exception=None):
    return error_pages.error_response(
        request, {"error": "Bad request", "code": 400, "status": False}, 400)


def handler403(request, exception=None):
    return error_pages.error_response(
        request, {"error": "Permission denied", "code": 403, "status": False}, 403)


def handler404(request, exception=None):
    return error_pages.error_response(
        request, {"error": "Endpoint not found", "code": 404, "status": False}, 404)


def handler500(request):
    return error_pages.error_response(
        request, {"error": "system error", "code": 500, "status": False}, 500)
```

Two things worth knowing:

- Django only routes to `handler404`/`handler500` when **`DEBUG = False`**. With
  `DEBUG` on you get Django's technical pages instead, which is usually what you
  want in development.
- `render_root_page` answers an API client with
  `{"status": true, "code": 200, "message": "Nothing is published here yet"}` at
  HTTP 200, so an uptime monitor pointed at `/` still gets a machine-readable
  healthy answer.

Replace the root route as soon as the project actually serves something at `/`.

---

## API reference

`mojo.helpers.error_pages`

| Symbol | Purpose |
|---|---|
| `prefers_html(request)` | The negotiation rule, as a boolean |
| `error_response(request, payload, json_status, page_status=None, reference=None)` | The seam: page for a browser, `payload` for everyone else |
| `render_error_page(request, status, reference=None)` | The page for `status` at that true status; `None` when the status has no page |
| `render_root_page(request)` | The unconfigured-root response (200) |
| `PAGES` | `{status: template basename}` — the statuses that have a page |
| `TEMPLATE_DIR` | The shipped template root |
