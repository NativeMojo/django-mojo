# Built-in Admin Portal

django-mojo ships a small, dependency-free control-plane UI. It covers System
Setup/readiness, the system overview, User and Group CRUD, Domains, provider
credentials, live DNS record sets, Certificates, Upstreams, Vhosts, Routes,
and WebApp GitHub deployment-key management. Fleet, operations, security,
configuration, and metrics extend the same shell without changing its delivery
boundary.

## Enable and route it

The portal is loaded with the account app and defaults to `/admin/`. The path
and source-session lifetime are static deployment settings:

```python
MOJO_ADMIN_PATH = "admin"          # one URL-safe path segment
MOJO_ADMIN_SESSION_TTL = 900       # seconds
MOJO_ADMIN_COOKIE_NAME = "mojo_admin"
MOJO_ADMIN_COOKIE_SECURE = True    # defaults to not DEBUG
```

An interactive JWT user needs a global `view_admin`, `manage_users`, or
`admin` permission (superusers pass automatically). API keys and group-scoped
tokens cannot create an Admin source session.

## Private source delivery

An anonymous `GET /admin/` receives only a small Bouncer handoff page. It does
not receive the shell, page declarations, forms, or private CSS. Anonymous
requests for `/admin/assets/*` return `404`.

After Bouncer login, the gate exchanges the already-validated interactive JWT
at `POST /api/account/admin/session`. The server writes a short-lived Redis
record and a path-scoped `HttpOnly; SameSite=Strict` cookie. Every shell and
asset request revalidates the Redis session, the active User, the recorded JWT
expiry, and the fingerprint of `User.auth_key`. Changing the auth key therefore
revokes both JWTs and Admin source access.

Private responses use `Cache-Control: no-store`, a restrictive CSP, frame
denial, and MIME-sniffing denial. Production should terminate TLS and keep
`MOJO_ADMIN_COOKIE_SECURE=True`.

This is a delivery boundary, not DRM: an authorized operator or anyone with a
build artifact can inspect frontend source. REST permissions remain the
authority for every data read and mutation.

## Browser architecture

The portal is native HTML, CSS, and ES modules packaged inside `mojo`. The
shell owns authentication, theme, navigation, routing, stale-render
cancellation, overlay cleanup, and heading focus. A fixed registry imports the
six feature namespaces `dashboard`, `people`, `webapps`, `activity`,
`platform`, and `advanced`; there is no runtime plugin discovery or
settings-based module import. Each descriptor declares its routes, navigation,
stylesheet, title, capability check, and one
`render({ctx, route, navigate, signal})` function. Render must resolve to
exactly one DOM `Node`; an optional `node.dispose()` releases feature-local
listeners or work.

Python publishes the same fixed namespaces at `bootstrap.features`, alongside
the stable flat `bootstrap.capabilities` compatibility object. Provider output
is validated as named boolean capabilities. An exception or malformed provider
disables only that feature and logs its exception class, never request data or
secrets.

Shared `TableView`, `FormView`, overlay, model, relationship, icon, API, and
view-state primitives live under `assets/components/` and `assets/core.js`.
The full-envelope API helper preserves `data`, `results`, `items`, `count`,
`start`, and `size`; use it for paged relationships instead of discarding REST
metadata. The relationship control URL-encodes searches, filters, graph names,
and detail identifiers; debounces search, cancels stale requests, supports
paging and keyboard listbox behavior, and posts only its selected id through a
hidden input. The ordinary API wrapper still refreshes an expired JWT once,
renews the source session, and handles HTTP `440` by returning through Bouncer
with `force_reauth=1`; that flag suppresses the ordinary silent-refresh path,
which cannot make an old `auth_time` fresh.

The Hybrid visual density is intentional: 13px base text, 11–12px tables and
forms, 21px page titles, and 20px KPI values. Light, dark, and system themes are
built in and stored only in browser local storage. The responsive shell keeps
keyboard-visible controls, traps focus in modals, restores focus on close, and
uses text nodes for API data; only the fixed local SVG icon catalog uses HTML.

The Platform feature owns the System Setup/readiness/control-plane journey and
is visible only to a literal superuser. Its private
`assets/features/platform/page.js` module renders the normalized readiness
report, durable step progress, typed
late choices, cancellation, and bounded live log. See
[System Setup and Readiness](system_setup.md) for the service and security
contracts.

The Advanced feature owns raw Domains, Credentials, DNS, Certificates,
Upstreams, Vhosts, and Routes. Its `assets/features/advanced/page.js` module is
the permanent hosting UI. It does not
create portal-only mutation endpoints. Provider DNS writes are keyed
single-flight and receive no automatic transport retry. Every write is
followed by a fresh authoritative
record read; an unconfirmed outcome latches that record set as
refresh-required until the operator explicitly refreshes it. Vhost creation is
a four-shape wizard (`api`, `site`, `site_api`, `redirect`); `site_api` routes
are created sequentially so a partial result can be repaired without replaying
successful rows.

## Packaging and testing

Portal assets live under `mojo/apps/account/admin_portal/` rather than Django's
public static pipeline. Delivery is generated from the exact base
`manifest.json` and the six owner-local `assets/features/*/manifest.json`
files. A feature manifest may name only files in its own directory; duplicate,
missing, unknown, absolute, backslash, dot, and traversal paths fail closed.
Add a shared asset to the foundation manifest or a feature asset to that
feature's manifest. Do not add a second Python allowlist.

Use `bin/create_testproject` and test through the real HTTP endpoints. The
split tests in `tests/test_account/test_admin_portal_*.py` prove anonymous
gate-only delivery, exact asset denial, cookie attributes, authorized delivery,
feature-provider isolation, shell/component contracts, auth-key revocation,
forced-reauth behavior, and WebApp key lifecycle.

For pixel review without weakening Bouncer, run `bin/admin_preview` and open
`http://127.0.0.1:5608/`. It serves the exact packaged assets with deterministic
loopback-only API fixtures; it does not add a Django route or production bypass.
The launcher is intentionally thin; `bin/admin_preview_support/` owns the
support server, foundation gallery, and resettable feature providers. Use
`--key-state missing|active|rotated|revoked` to exercise the four WebApp
deployment-key presentations, `--setup-state idle|choice` for resumable Setup,
and `--port` when parallel work needs isolation. Every launch resets mutable
provider state before serving.
