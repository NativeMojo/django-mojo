# OAuth 2.1 Authorization Server

The `account` app is an OAuth 2.1 authorization server for resources that other
apps register with it. A spec client (an AI agent, a CLI, an MCP host) discovers
it, sends the user through the hosted sign-in pages to a consent screen, and
receives a short-lived JWT access token plus a rotating opaque refresh token.

The point of the design is **confinement**: an issued token authenticates within
the one registered resource its audience names, and nowhere else on the
platform. A resource is either one exact path (the Assistant's MCP door) or a
prefix — the REST API root — and the scope the person consented to decides which
kind a grant may bind to.

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

        def enabled():
            return settings.get("ASSISTANT_MCP_ENABLED", False, kind="bool")

        oauth_server.register_resource("/api/assistant/mcp", ["mcp"], enabled)
        # …and, behind the same switch, the whole REST API:
        oauth_server.register_resource(
            API_ROOT, ["mcp", "api"], enabled, prefix=True)
```

`register_resource(path, scopes, enabled, prefix=False)`:

- **`path`** is the absolute request path exactly as routed — a leading slash,
  no trailing slash. The **token audience** is always resolved by exact string
  equality with no normalisation. What that path then covers depends on the
  entry: an exact resource matches `request.path` by the same exact equality,
  while a prefix resource also accepts any path strictly beneath it (see
  `covers`, below). No normalisation happens in either case.

  > **Register the path as Django routes it, not as you wish it looked.**
  > Confinement compares the literal `request.path`. A `MOJO_APPEND_SLASH=True`
  > deployment must register the slashed form, and a deployment mounted under a
  > WSGI `SCRIPT_NAME` prefix must register the prefixed form. Get it wrong and
  > the failure is fail-closed and total — every token is refused at the door —
  > rather than a hole, but nothing will work until it matches.
- **`scopes`** is a list. Two scopes exist: `mcp` (the Assistant's tool door) and
  `api` (full REST reach as the consenting person).
- **`enabled`** is a zero-arg callable, **re-evaluated on every read and never
  cached**. Flipping the setting takes effect immediately in both directions.
  A callable that raises counts as disabled — the registry fails closed.
- **`prefix`** declares a **subtree** instead of an endpoint. A token bound to a
  prefix resource may be presented at the path itself or anywhere strictly
  beneath it; an exact resource covers its own path and nothing else.

### The invariant: prefix ⇔ `api`

`register` raises `ValueError` for either half of the mismatch:

| Registration | Result |
|---|---|
| `prefix=True` without `api` in `scopes` | Refused — that is full reach nobody had to consent to. |
| `prefix=False` with `api` in `scopes` | Refused — an exact resource cannot honour a scope its confinement does not grant. |

The same invariant is enforced twice more, at consent (a grant may name a prefix
resource **iff** its scopes include `api`) and at the chokepoint (step 7 below).
Three enforcements for one rule is deliberate: a prefix resource is the whole
API, and unlike the MCP door there is no resource server sitting at the root to
read scopes and answer 403.

`offered_scopes(registry=None)` is the ordered, de-duplicated union of the
**enabled** entries' scopes. Discovery publishes exactly that, so an installation
that registers only an exact resource never advertises `api`.

`covers(entry, request_path)` answers "may this entry's token be presented
here": `True` for the entry's own path; for a prefix entry also `True` for
anything starting with `entry.path.rstrip("/") + "/"`. The separator is attached
before the comparison, so `/api` covers `/api/x` and never `/apix`.

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
   consumer both call `validate_jwt` with no request. This is what stops an
   OAuth token being exchanged for a session pair or opening a WebSocket.
2. **`aud` must be a single string** with a path. A list-valued `aud` is refused
   outright — PyJWT would otherwise match by membership, which is not
   confinement.
3. **`urlsplit(aud).path` must resolve EXACTLY** to a registered resource whose
   switch is on. The lookup is keyed by the **audience's** own path, never by the
   request's, so a token always names the one entry it was minted for.
4. **That entry must cover `request.path`** — equal for an exact resource, equal
   or strictly beneath for a prefix one. Steps 3 and 4 together are what the old
   single `aud path == request.path` comparison was, generalised by exactly one
   resource kind.
5. From here on, every refusal also stamps `request.www_authenticate`, built from
   the **resource's** path — so a bad `api` token at `/api/account/user/me`
   points the client at `/.well-known/oauth-protected-resource/api`.
6. **The grant must resolve** from the token's `jti` (`OAuthGrant.access_jti`),
   be active, name the same resource, and belong to an active user and an active
   client.
7. **A grant bound to a prefix resource must carry the `api` scope.** Defence in
   depth: consent refuses to create such a row in the first place, and this is
   the last place a full-reach grant without full-reach consent can be stopped.
   `scopes` must be a list — `"api" in "apix"` is true for a string.
8. **Signature, expiry and audience** are verified together against the
   **user's** `auth_key` — a disable, a closure or a `revoke_sessions` rotates
   that key and therefore kills every live token on the next request — then
   **`request.oauth_grant` is stamped** and the grant's `last_used` updated.

Every refusal returns the generic `"Invalid token"`. Only expiry says
`"Token expired"` — the same oracle policy the existing branches follow. A
refusal that lands before step 5 (unknown resource, switched off, not covered)
carries **no** challenge: the caller is not at a live door, and saying so would
be an existence oracle.

**Scope is otherwise deliberately not checked here.** An exact resource server
reads `request.oauth_grant.scopes` itself, so it can answer
`403 insufficient_scope` rather than a blanket 401. Step 7 is the one exception,
precisely because a prefix resource has no such server.

### What the resource server reads

| Attribute | Meaning |
|---|---|
| `request.oauth_grant` | The `OAuthGrant`. Present only for an accepted mcp token. `None`/absent for every other credential. |
| `request.user` | The granting user, as usual. |
| `request.bearer` | Stays `"bearer"`. It is **not** changed to `"mcp"`: the middleware overwrites it after the handler returns, and `fresh_auth.is_fresh` bypasses step-up for every non-`"bearer"` carrier — changing it would weaken step-up, not strengthen it. |

`mojo.apps.assistant.services.agent._build_request_meta` reads the marker and
reports `bearer="mcp"` for a grant-carrying request, so a tool that demands a
strictly interactive session (`bearer == "bearer"`) refuses an MCP-originated
call with no further change.

**`key_backed` stays `False`, deliberately.** `is_key_backed_session()` and
`restricted_identity()` are not extended to cover an OAuth-grant session, and the
reason is what the grant **is** rather than where it can arrive: it is the
person's own session, consented to on this installation's sign-in page, carrying
their `auth_time` and dying with their `auth_key`. A grant holding `api`
therefore reaches every endpoint their session JWT reaches — including
`denies_key_backed_session` ones such as `generate_api_key`, and including
resolving Assistant approval cards at `POST /api/assistant/action` when the
person holds the permission for it. That is the equality the consent screen
states, and in both cases the grant gains no reach: it could make the same
mutation directly, under the same permission, superuser and fresh-auth gates.
An `mcp`-only grant never leaves its tool door, and the approval step still
protects it.

An `ApiKey` or a group-scoped token is the opposite — a secret in a config file
acting for somebody — which is what `denies_key_backed_session` exists to keep
away from credential mutation. The signal that means "a remote agent is driving
this, not a person" is `request_meta.bearer == "mcp"`.

An API key an `api` grant mints is a **separate** credential with its own
lifetime: revoking the grant does not revoke it, exactly as revoking a browser
session does not revoke a key minted from it. Both are revocable on their own.

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

## The two scopes

| Scope | Binds to | What it opens |
|---|---|---|
| `mcp` | an **exact** resource | That one endpoint. The Assistant's MCP door reads `grant.scopes` itself and answers `403 insufficient_scope` without it. |
| `api` | a **prefix** resource | Every path beneath the API root, as the consenting person — the same permissions, the same `auth_time`, the same `requires_fresh_auth` behaviour their session JWT has. |

**What `api` is.** Exactly what that account can already do through the API with
its own session token: the same permission checks, the same group scoping, the
same 440 when a step-up endpoint wants a recent login. Nothing about it widens a
permission.

**Accepted, and worth stating plainly, because "the same as a session" has sharp
edges:**

- **Owner-only surfaces are reachable.** `require_request_admin`, System Setup
  and Admin settings answer an `api` grant exactly as they answer that person's
  browser session, subject to the same step-up window — the grant carries the
  `auth_time` of the session that approved it, so a stale one gets 440 and the
  only way forward is re-consent. A grant held by a superuser is a superuser
  credential; that is the equality, not a gap in it.
- **It can approve the Assistant's own pending actions.** `POST
  /api/assistant/action` gates on `request.bearer == "bearer"` and
  `denies_key_backed_session`, both of which an `api` grant passes. It gains no
  reach by doing so — it could make the same mutation directly, under the same
  permission, superuser and fresh-auth gates — but the approval step stops being
  a second party for that grant. The consent screen says so in those words.
- **`generate_api_key` is reachable, and a minted key outlives the grant.** It
  is a separate credential with its own lifetime, revoked on its own, exactly as
  a key minted from a browser session is. A Security follow-up is filed for a
  fresh-auth gate on that endpoint; it is not part of this feature.

These follow from the requester's rule — an `api` grant equals a session token
in reach — rather than qualifying it. What bounds the credential is that it is
short-lived, absolutely capped at 30 days, revocable from the Admin, and dies
with the person's `auth_key`.

**What `api` is not.**

- It is **not** a session. It cannot approve a new grant
  (`consent._session_auth_time` requires `token_type == "access"`), cannot be
  exchanged at `/api/account/jwt/refresh`, and cannot open a WebSocket
  (`realtime` validates with no request, and step 1 refuses that).
- It is **not** reach outside the root. `/.well-known/…` and anything else above
  or beside `API_ROOT` refuses it, with no challenge.
- It does **not** carry the Assistant's approval step. The consent screen says
  so in those words — including that approving the Assistant's own pending
  actions is part of what is being granted.

Both scopes ride the one `ASSISTANT_MCP_ENABLED` switch — "remote agents may
sign in" — and both kinds of grant are listed and revoked from the same Admin
section.

**An installation with `MOJO_PREFIX=""` offers no `api` scope at all.** The root
would resolve to `/`, and a prefix resource there covers every path the host
serves — the hosted sign-in pages and any other product sharing the host, not
merely the REST API. `register` refuses it (`ValueError`) and the assistant app
skips that one registration with a `logit.error`, so the tool door still works
and discovery simply never advertises `api`. Fail-closed, and visible in the
log rather than silent.

**One grant can serve both doors** only while the MCP path sits beneath
`API_ROOT`, which the shipped default (`/api/assistant/mcp` under `/api`) does.
Move `ASSISTANT_MCP_PATH` outside the root and both resources still work
separately; what is lost is an `mcp api` root grant reaching the door, which then
answers 401 because the root does not cover it. Fail-closed, and the same caveat
a `SCRIPT_NAME`-mounted deployment already carries.

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
| `resources.py` | `ResourceRegistry`, `register_resource`, `covers`, `offered_scopes`, `public_origin`, the issuer/canonical/PRM URL algebra, `is_ready`, and the four TTL readers. |
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

# An Admin surface that owns ONE resource scopes and bounds its reads:
oauth_server.list_grants(resource_path="/api/assistant/mcp", limit=200)
oauth_server.count_grants(resource_path="/api/assistant/mcp")
oauth_server.revoke_all_grants(actor=request.user,
                               resource_path="/api/assistant/mcp")

# …or SEVERAL, as one predicate — the Assistant setup page owns both doors:
oauth_server.list_grants(resource_path=["/api/assistant/mcp", "/api"])
```

`resource_path` scopes by the resource's **path**, never its full URL: the URL
embeds `BASE_URL`, so matching on it would hide still-valid grants after a
public-address change. It takes **one path or several** — a list becomes one
`OR` predicate, so a caller spanning two resources still gets one query and one
bulk `UPDATE` rather than two of each. The SQL suffix filter is a **superset**
(`https://x/nested/api` also ends with `/api`), so all three re-confirm the
parsed path in Python before counting, listing or revoking a row —
`count_grants` included, which is why it reads the `resource` column rather than
issuing a bare `COUNT(*)`. That is what makes the number it reports equal to
what is listed and what a sweep would touch, past the `limit` slice. All three
default to the previous behaviour — every grant, every resource, unbounded; an
empty list selects nothing.

`revoke_all_grants` is one bulk `UPDATE` on the scoped active set. Deactivating
the row is what kills the credential (`validate_access` filters `is_active=True`
and `_check_refreshable` refuses an inactive grant), so the per-row column
rotation `revoke_grant` performs is not repeated here; the audit is one
`oauth:grant_revoked` line per affected user carrying that user's count.

### Clients

`register_client(data)` implements RFC 7591 and is deliberately **lenient**: a
requested `token_endpoint_auth_method` is ignored and `"none"` is echoed (§3.2.1
allows substitution, and SDK defaults routinely ask for `client_secret_post`),
and `grant_types` / `response_types` are intersected with what the server
supports. Only a bad `redirect_uris` is fatal — that one is a security boundary,
not a preference.

`resolve_client(client_id, fetcher=None)` takes the CIMD path when `client_id`
starts with `https://`. The document is fetched through the shared SSRF-safe
helper (`mojo.helpers.safe_fetch`, https only, 5 s, 64 KiB, at most one
redirect), must be a JSON object that names itself, and is cached in Redis for
300 s. **An existing row with `is_active=False` is refused before any fetch or
write**, so the Admin's deactivation is a real kill switch that re-resolution
cannot undo. Tests inject `fetcher=` and never touch the network.

Two supporting rules make that kill switch hold:

- **`canonical_cimd_url(url)` is the identity.** `client_id` is matched by exact
  string, so `https://EVIL.example/c.json`, `https://evil.example:443/c.json` and
  a percent-encoded spelling of the same path would otherwise be three separate
  rows — and deactivating one would leave the other two live. Every variant is
  reduced (lowercased scheme and host, default port stripped, path unquoted /
  de-duplicated / re-quoted, userinfo / query / fragment refused) before the
  inactive-row check, the cache key, the self-naming comparison and
  `get_or_create` see it, and the canonical string is what is stored.
- **Failures are cached too**, under the same key and window. Otherwise a
  `client_id` pointing at a URL that stalls or 404s turns every authorize and
  token attempt into an outbound fetch from this server.

A client's own strings — `client_name` in a registration or a metadata document,
and the metadata URLs — go through `clean_text()`: printable characters only,
whitespace collapsed, truncated. A client names itself, and that name reaches a
`logit.info` line and the consent screen; a newline in it would forge a log
record.

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
pair is minted. Outside the window it is **replay**: the grant is revoked
(`revoked_reason="refresh_replay"`) and an incident is reported.

Two details of the grace re-issue are load-bearing:

- **`last_refreshed` is not moved**, so the window keeps ticking from the
  original rotation and a client that retries forever cannot walk it forward.
- **`prev_refresh_hash` IS moved**, to the `refresh_hash` the re-issue replaces
  — the successor that has just been orphaned. This is a detection control, not
  bookkeeping. A grace hit is *inherently* ambiguous: it looks the same whether
  the real client lost its response or a thief is using a captured token. If the
  orphaned successor were simply dropped, the party still holding it would get a
  bare `invalid_grant` with no incident, and a stolen refresh token would be a
  **silent** takeover — the victim's client would just look broken. Parking the
  orphan in `prev_refresh_hash` means presenting it after the window trips
  replay, so the theft surfaces on the victim's next refresh.

The cost is deliberate and bounded: a response lost **twice in a row** is not
recoverable, because the original token is no longer in either column after the
first grace re-issue. Re-consent is one click; an undetected takeover is not.

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

**One sentence per granted scope**, tools first: `mcp` renders "Use the
Assistant's tools as {email} — the same permissions as your account; changes
still need your approval in the Admin", and `api` adds "Full API access as
{email} — everything your account can do through the API, and nothing more,
including approving the Assistant's own pending actions. The Assistant's
approval step does not apply to direct API calls." The `{email}`
placeholder is filled in client-side once `/me` answers; the sentences
themselves are rendered and autoescaped by the template, so no server value is
concatenated into executable text.

**The `resource` is scope-driven and never upgraded.** Eligibility is
`entry.prefix == ("api" in granted)` plus "the entry offers every granted scope",
so an `api` request binds the API root and an `mcp`-only request binds an exact
resource. An explicit `resource` is echoed exactly (RFC 8707) — naming the MCP
door while asking for `api` answers `invalid_scope`, not a silent upgrade to the
root, and `_exchange_code` would refuse the mismatch at the token endpoint
anyway. Omitting `resource` defaults when exactly one **eligible** entry is
enabled, which is why adding the API root beside the door did not make every
pre-2025-06-18 MCP client ambiguous. The practical consequence: an MCP client's
built-in sign-in cannot obtain `api` — a client that wants it must name the root.

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
frameable on the shipped default. Every response is also `Cache-Control:
no-store` — the page names the signed-in person and is one click from a
credential.

**The page shows what a client name cannot forge.** A display name is whatever
the client typed, so the name alone is a phishing surface. Beside it the page
renders three facts the client does not choose: whether anything vouched for the
name (the `client_id` URL, labelled "Verified from", for a CIMD client;
"unverified name — registered by the client itself" for a DCR one), the exact
resource the token will open, and the host the credential will actually be
delivered to. All autoescaped, none of them links.

The **presented** `redirect_uri` is re-validated with `validate_redirect_uri`
before it is matched, at `authorize`, at `approve` and again at
`consume_code`. `redirect_uri_matches` ignores the port for loopback URIs, so
without that a presented value could carry userinfo, a fragment, or enough bytes
to overflow `OAuthCode.redirect_uri` past the comparison.

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
