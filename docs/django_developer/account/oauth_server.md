# OAuth 2.1 Authorization Server

The `account` app is an OAuth 2.1 authorization server for resources that other
apps register with it. A spec client (an AI agent, a CLI, an MCP host) discovers
it, sends the user through the hosted sign-in pages to a consent screen, and
receives a short-lived JWT access token plus a rotating opaque refresh token.

The point of the design is **confinement**: an issued token authenticates at the
one registered resource path its audience names, and nowhere else on the
platform. A token sitting in a third party's config file cannot act as that
person against the rest of the API.

> This is not the same thing as [OAuth / Social Login](oauth.md), which is the
> *client* side — signing users in with Google. That lives at
> `/api/oauth/<provider>/…` and is untouched by any of this.

For the wire protocol as a client sees it, see
[web_developer/account/oauth_server.md](../../web_developer/account/oauth_server.md).

---

## Requirements

Two things must be true or the server refuses everything with a 404:

| Requirement | Why |
|---|---|
| `BASE_URL` is set to the public HTTPS origin | It is the issuer and the audience. There is deliberately **no** fallback to the request's `Host` header — an attacker-chosen host must never become an advertised issuer or a minted audience. |
| At least one registered resource is **enabled** | An authorization server with nothing to protect should not advertise itself, accept registrations, or issue anything. |

`REST_AUTO_PREFIX=True` is also required for the absolute routes
(`/.well-known/…`, `/api/account/oauth/…`) to mount at the root — the same
pre-existing requirement as the hosted auth pages and the Admin portal.

---

## Registering a resource

An app declares the endpoint it wants protected from its `AppConfig.ready()`:

```python
from django.apps import apps

class AppConfig(BaseAppConfig):
    def ready(self):
        if not apps.is_installed("mojo.apps.account"):
            return
        from mojo.apps.account.services import oauth_server
        from mojo.helpers.settings import settings

        oauth_server.register_resource(
            "/api/assistant/mcp",
            ["mcp"],
            lambda: settings.get("ASSISTANT_MCP_ENABLED", False, kind="bool"))
```

`register_resource(path, scopes, enabled)`:

- **`path`** is the absolute request path exactly as routed — a leading slash,
  no trailing slash. The comparison against `request.path` is exact string
  equality with no normalisation, so a `MOJO_APPEND_SLASH=True` deployment
  registers the slashed form.
- **`scopes`** is a list. Today the only scope is `["mcp"]`.
- **`enabled`** is a zero-arg callable, **re-evaluated on every read and never
  cached**. Flipping the setting takes effect immediately in both directions.
  A callable that raises counts as disabled — the registry fails closed.

Re-registering the same path replaces the entry, so a repeated `ready()` is
idempotent. `unregister_resource(path)` removes one.

**Disabled is dormant, not revoked.** While the switch is off, discovery 404s,
consent 404s, refresh is refused and live tokens stop validating — but the grant
rows survive, so the Admin still sees them and re-enabling brings them back.

### The `registry=` seam

`ResourceRegistry` is a class with one shared default instance (`REGISTRY`).
Every function that consults it takes `registry=None`. Module-level mutable
state is visible to every test module in testit's threaded runner, so tests
build a private `ResourceRegistry()` instead of mutating the shared one.

---

## Confinement: the `mcp` branch of `validate_jwt`

`User.validate_jwt` gains a `token_type == "mcp"` branch, placed after the
`user_api_key` branch and before the ordinary session lookup. Its body is
`services/oauth_server/tokens.validate_access(token, jwt_data, request)`, and it
runs, in order:

1. **`request is None` → refuse.** The refresh endpoint and the realtime
   consumer both call `validate_jwt` with no request. This is what stops an mcp
   token being exchanged for a session pair or opening a WebSocket.
2. **`aud` must be a single string**, and `urlsplit(aud).path` must equal
   `request.path` exactly. A list-valued `aud` is refused outright — PyJWT would
   otherwise match by membership, which is not confinement.
3. **That path must be a registered resource whose switch is on.** From here on,
   every refusal also stamps `request.www_authenticate`.
4. **The grant must resolve** from the token's `jti` (`OAuthGrant.access_jti`),
   be active, name the same resource, and belong to an active user and an active
   client.
5. **Signature, expiry and audience** are verified together against the
   **user's** `auth_key`. A disable, a closure or a `revoke_sessions` rotates
   that key and therefore kills every live mcp token on the next request.
6. **`request.oauth_grant` is stamped** and the grant's `last_used` updated.

Every refusal returns the generic `"Invalid token"`. Only expiry says
`"Token expired"` — the same oracle policy the existing branches follow.

**Scope is deliberately not checked here.** The resource server reads
`request.oauth_grant.scopes` itself, so it can answer `403 insufficient_scope`
rather than a blanket 401.

### What the resource server reads

| Attribute | Meaning |
|---|---|
| `request.oauth_grant` | The `OAuthGrant`. Present only for an accepted mcp token. `None`/absent for every other credential. |
| `request.user` | The granting user, as usual. |
| `request.bearer` | Stays `"bearer"`. It is **not** changed to `"mcp"`: the middleware overwrites it after the handler returns, and `fresh_auth.is_fresh` bypasses step-up for every non-`"bearer"` carrier — changing it would weaken step-up, not strengthen it. |

`mojo.apps.assistant.services.agent._build_request_meta` reads the marker and
reports `bearer="mcp"` for a grant-carrying request, so a tool that demands a
strictly interactive session (`bearer == "bearer"`) refuses an MCP-originated
call with no further change. `key_backed` stays `False`: an OAuth grant is not a
confined machine credential in the `ApiKey`/group-token sense.

### The `WWW-Authenticate` challenge

`www_authenticate(path, error="invalid_token", description="", scope="")` builds
the RFC 9728 header value:

```
Bearer error="invalid_token", resource_metadata="https://example.com/.well-known/oauth-protected-resource/api/assistant/mcp"
```

Who emits it:

- **The auth middleware**, for every bad-token 401 at a live resource path. A
  bad bearer never reaches a view — `mojo/middleware/auth.py` answers 401 before
  dispatch — so `validate_access` stamps `request.www_authenticate` and the
  middleware copies it onto its response. Nothing else stamps the attribute, so
  no other endpoint's 401 grows a header.
- **The resource server itself**, for the two cases the middleware cannot see:
  the no-credential 401 (`www_authenticate("/api/assistant/mcp")`) and the
  403 (`www_authenticate(path, error="insufficient_scope", scope="mcp")`).

`mojo/middleware/cors.py` lists `WWW-Authenticate` in
`Access-Control-Expose-Headers` so a browser-hosted client can read it.

`resource_metadata` is omitted when `BASE_URL` is unset.

---

## Models

Three models, all `models.Model, MojoModel`, all service-managed — there is no
REST CRUD endpoint for any of them. Their `RestMeta` exists for the Admin
graphs and carries `DENY_AI = True`.

| Model | Holds |
|---|---|
| `OAuthClient` | `client_id` (a random hex for DCR, the https document URL for CIMD), `kind`, `client_name`, `redirect_uris`, `metadata`, `is_active`, `last_used`. |
| `OAuthGrant` | `user`, `client`, `access_jti`, `access_expires`, `refresh_hash`, `prev_refresh_hash`, `refresh_expires`, `last_refreshed`, `last_used`, `scopes`, `resource`, `auth_time`, `is_active`, `revoked_reason`. |
| `OAuthCode` | `client`, `user`, `code_hash`, `redirect_uri`, `code_challenge`, `scope`, `resource`, `auth_time`, `expires`, `consumed`, `grant`. |

**Only hashes are stored.** The raw authorization code and the raw refresh
secret exist once, on the wire; the database holds `sha256` hex. Nothing here
can reproduce a credential.

`access_jti` is the `jti` of the **current** access token. That gives two
properties a grant-constant identifier could not: a refresh retires the previous
access token immediately, and revocation invalidates a live token before its
`exp` with no denylist. Both unique columns carry random values from creation
onward and are only ever overwritten with fresh random values, so a revoked
grant matches no live token and no two rows collide.

---

## The services package

`mojo/apps/account/services/oauth_server/`:

| Module | Owns |
|---|---|
| `resources.py` | `ResourceRegistry`, `register_resource`, `public_origin`, the issuer/canonical/PRM URL algebra, `is_ready`, and the four TTL readers. |
| `discovery.py` | `authorization_server_metadata`, `protected_resource_metadata`, `www_authenticate`. |
| `clients.py` | `validate_redirect_uri`, `redirect_uri_matches`, `register_client` (RFC 7591), `resolve_client` (DB or CIMD). |
| `codes.py` | `validate_pkce_challenge`, `verify_pkce`, `mint_code`, `consume_code`. |
| `tokens.py` | `create_grant`, `mint_access_token`, `issue_tokens`, `refresh_grant`, `revoke_grant`, `revoke_token`, the Admin API, and `validate_access`. |
| `consent.py` | `handle_authorize`, `handle_approve` — all the page logic, so it is testable in-process without `BASE_URL`. |

Siblings import the package, never its modules:

```python
from mojo.apps.account.services import oauth_server

oauth_server.register_resource(path, scopes, enabled)
oauth_server.www_authenticate(path, error="insufficient_scope", scope="mcp")
oauth_server.list_grants(user=some_user)
oauth_server.revoke_grant_by_id(grant_id, actor=request.user)
oauth_server.revoke_all_grants(actor=request.user)
```

### Clients

`register_client(data)` implements RFC 7591 and is deliberately **lenient**: a
requested `token_endpoint_auth_method` is ignored and `"none"` is echoed (§3.2.1
allows substitution, and SDK defaults routinely ask for `client_secret_post`),
and `grant_types` / `response_types` are intersected with what the server
supports. Only a bad `redirect_uris` is fatal — that one is a security boundary,
not a preference.

`resolve_client(client_id, fetcher=None)` takes the CIMD path when `client_id`
starts with `https://`. The document is fetched through the shared SSRF-safe
helper (`mojo.helpers.safe_fetch`, https only, 5 s, 64 KiB), must be a JSON
object that names itself, and is cached in Redis for 300 s. **An existing row
with `is_active=False` is refused before any fetch or write**, so the Admin's
deactivation is a real kill switch that re-resolution cannot undo. Tests inject
`fetcher=` and never touch the network.

Redirect URIs are exact-string matched **except** that loopback `http` URIs
(`localhost`, `127.0.0.1`, `::1`) match on any port — RFC 8252 §7.3 makes that a
MUST, because a CLI client binds an ephemeral port per run. https or loopback
http only; no custom schemes, no fragments, no userinfo, and control characters
are refused at validation so a stored URI can never reach a `Location` header
malformed.

### Rotation, grace and replay

`issue_tokens(grant, expected_refresh_hash=None)` rotates in ONE conditional
`UPDATE … WHERE refresh_hash = <presented>`. Two concurrent refreshes therefore
settle in the database: exactly one moves the row, and the loser raises
`RotationLost`, re-reads, and lands in the grace path — where it is handed a
working pair rather than a dead one.

`refresh_grant` matches the presented hash against `refresh_hash` first, then
`prev_refresh_hash`. A `prev_refresh_hash` match inside
`OAUTH_REFRESH_GRACE_SECONDS` of the rotation is a **lost response**: a fresh
pair is minted without moving `prev_refresh_hash` or `last_refreshed`, so the
window keeps ticking from the original rotation and a client retrying forever
cannot walk it forward. Outside the window it is **replay**: the grant is
revoked (`revoked_reason="refresh_replay"`) and an incident is reported.

The refresh lifetime is **absolute** — 30 days from consent, never slid. A
credential in a third party's custody gets a hard upper bound with no human in
the loop; re-consent is one click.

### Events

| Event | Where |
|---|---|
| `oauth:grant_created` | `user.log`, on `create_grant`. |
| `oauth:grant_revoked` | `user.log`, on `revoke_grant`. |
| `oauth:code_replay` | `report_incident`, level 6. |
| `oauth:refresh_replay` | `report_incident`, level 6. |

Dynamic client registration writes a `logit.info` line only — there is no user
yet to attribute it to.

---

## The consent page

`GET /api/account/oauth/authorize` renders `account/oauth_consent.html`, which
extends `account/auth_base.html` and is themed through
`auth_config.resolve_auth_config` like every other hosted page. With no session
its script sends the visitor to the bouncer login with `?redirect=` back to
itself (same origin, so the login page navigates straight back). With a session
it shows the identity, the client name, the access in plain words, and
Approve / Deny.

Two rules shape the validation order:

- A bad `client_id` or an unregistered `redirect_uri` **renders an error page
  (400) and never redirects** — redirecting there is exactly the open redirect
  the check exists to prevent.
- Every later parameter error **redirects** (302) to the now-trusted redirect
  URI with an RFC error code, the echoed `state` (capped at 512 characters) and
  the RFC 9207 `iss`.

Approve POSTs to `/api/account/oauth/approve` with the session bearer in an
`Authorization` header. **No cookie is read anywhere on the page**, so the POST
is CSRF-proof by construction rather than by a token. The handler re-runs every
validation from scratch against the posted values — the page is a convenience,
never a trust boundary — and requires the bearer to be an ordinary interactive
session token (`token_type == "access"`) carrying an `auth_time`. That value is
copied onto the code, onto the grant, and into every access token the grant ever
mints, so step-up semantics carry through to the resource server.

Every consent response carries **`X-Frame-Options: DENY` unconditionally**.
`AUTH_CSP_ENABLED` ships `False`, so the CSP's `frame-ancestors 'none'` is not
guaranteed to be there, and a one-click credential-minting page must not be
frameable on the shipped default.

---

## Settings

All five are read with `get_static` — the **deployment file only**, never the
database plane. A `manage_settings` holder must not be able to lengthen a
credential lifetime through a `Setting` row.

| Key | Default | Meaning |
|---|---|---|
| `OAUTH_SERVER_PATH` | `api/account/oauth` | Path root of the server's endpoints. Also derives the issuer and the request-logging labels. |
| `OAUTH_ACCESS_TTL` | `3600` | Access-token lifetime, seconds. |
| `OAUTH_REFRESH_TTL_DAYS` | `30` | Absolute grant ceiling, measured from consent. |
| `OAUTH_REFRESH_GRACE_SECONDS` | `30` | Lost-response forgiveness window. |
| `OAUTH_CODE_TTL` | `300` | Authorization-code lifetime, seconds. |

`BASE_URL` (System Setup, or the deployment file) and each resource's own enable
setting complete the configuration.

The three credential-bearing paths are labelled in
`mojo.helpers.request.sensitive_body_label` — `token` and `revoke` as
`oauth_token`, `approve` as `oauth_approve` — so broad request/response logging
never writes a code verifier, a refresh token or a session bearer in plaintext.

---

## Tests

- `tests/test_oauth_server/` — default tier, in-process against private
  registries: the registry and discovery, clients and CIMD, codes and PKCE,
  grants and rotation, **confinement**, and the consent render.
- `tests/test_oauth_server_extended_serial/` — the whole flow over the wire,
  which needs `BASE_URL` and the enable switch on the running server.
