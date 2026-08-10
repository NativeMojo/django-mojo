# Built-in Admin Portal

django-mojo ships a small, dependency-free control-plane UI. Round one covers
the system overview, User and Group CRUD, and WebApp GitHub deployment-key
management. Fleet, network, operations, security, configuration, and metrics
can be added behind the same shell without changing its delivery boundary.

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

The portal is native HTML, CSS, and ES modules packaged inside `mojo`. Its
shared `TableView`, `FormView`, modal, icon, API, and routing primitives keep
page definitions declarative. The API wrapper refreshes an expired JWT once,
renews the source session, and handles HTTP `440` by returning through Bouncer
with `force_reauth=1`; that flag suppresses the ordinary silent-refresh path,
which cannot make an old `auth_time` fresh.

The Hybrid visual density is intentional: 14px body and form text, 13px tables,
24px page titles, and 26px KPI values. Light, dark, and system themes are built
in and stored only in browser local storage.

## Packaging and testing

Portal assets live under `mojo/apps/account/admin_portal/` rather than Django's
public static pipeline. Keep the server allowlist in
`mojo.apps.account.rest.admin_portal` synchronized when adding an asset.

Use `bin/create_testproject` and test through the real HTTP endpoints. The
security regression tests in `tests/test_account/test_admin_portal.py` prove
anonymous gate-only delivery, asset denial, cookie attributes, authorized
delivery, auth-key revocation, and forced-reauth behavior.

For pixel review without weakening Bouncer, run `bin/admin_preview` and open
`http://127.0.0.1:5608/`. It serves the exact packaged assets with deterministic
loopback-only API fixtures; it does not add a Django route or production bypass.
