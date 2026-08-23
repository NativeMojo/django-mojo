# Connecting an app with OAuth 2.1

This installation can act as an OAuth 2.1 authorization server, so an
application — an AI agent, a CLI, an MCP host — can act on a user's behalf at a
specific protected endpoint without anyone ever copying a secret.

The flow is the standard one: discover, register, send the user to the
installation's own sign-in and consent pages, exchange a PKCE-bound code for
tokens, refresh, revoke.

**The credentials you receive are confined.** An access token issued here works
only within the one resource URL it names. There are two kinds, and you choose
which by the scope you ask for:

| Scope | Resource | Reach |
|---|---|---|
| `mcp` | the Assistant's MCP endpoint | That one path. Every other endpoint answers `401`. |
| `api` | the **API root** (`https://example.com/api`) | Every path beneath the root, as the user — exactly what their own session token can do, and nothing more. |

Either way, presenting the token outside its resource returns `401`. That is by
design, not a bug to route around.

> This is different from [OAuth / Social Login](oauth.md), which is about
> signing users in *with* Google or Apple. This page is about a third-party
> application getting confined access *to* this installation.

---

## Discovery

Two documents, both at **path-suffixed** well-known URLs (RFC 8414 §3.1 and
RFC 9728). The root forms are deliberately not served — they are reserved for a
different product that may share the host.

```
GET /.well-known/oauth-authorization-server/api/account/oauth
GET /.well-known/oauth-protected-resource/<resource path>
```

Both answer `Access-Control-Allow-Origin: *` and raw RFC JSON — **no
`{status, code, data}` envelope**, unlike the rest of this API.

If you were sent here by a `401` carrying a `WWW-Authenticate` header, its
`resource_metadata` parameter is the protected-resource URL to start from.

> **A resource path this installation does not host answers the application's
> ordinary `404`**, not the RFC `{"error": "not_found"}` body — the
> protected-resource URL is not served at all for a path nobody registered, so
> the response may be HTML and carries no `Access-Control-Allow-Origin` header.
> A resource this installation *does* host is unchanged, whether it is currently
> enabled or switched off: it still answers the raw RFC JSON, `200` with the
> document or `404` with `{"error": "not_found"}`. Either way, treat any non-`200`
> as "no metadata here" and do not parse the body.

**Authorization-server metadata:**

```json
{
  "issuer": "https://example.com/api/account/oauth",
  "authorization_endpoint": "https://example.com/api/account/oauth/authorize",
  "token_endpoint": "https://example.com/api/account/oauth/token",
  "registration_endpoint": "https://example.com/api/account/oauth/register",
  "revocation_endpoint": "https://example.com/api/account/oauth/revoke",
  "response_types_supported": ["code"],
  "response_modes_supported": ["query"],
  "grant_types_supported": ["authorization_code", "refresh_token"],
  "code_challenge_methods_supported": ["S256"],
  "token_endpoint_auth_methods_supported": ["none"],
  "revocation_endpoint_auth_methods_supported": ["none"],
  "scopes_supported": ["mcp", "api"],
  "authorization_response_iss_parameter_supported": true,
  "client_id_metadata_document_supported": true
}
```

`scopes_supported` is the union of what the installation's **enabled** resources
offer. If `api` is absent, this installation is not offering full API access —
asking for it answers `invalid_scope`.

**Protected-resource metadata**, for the MCP endpoint:

```json
{
  "resource": "https://example.com/api/assistant/mcp",
  "authorization_servers": ["https://example.com/api/account/oauth"],
  "scopes_supported": ["mcp"],
  "bearer_methods_supported": ["header"]
}
```

…and for the API root, at `/.well-known/oauth-protected-resource/api`:

```json
{
  "resource": "https://example.com/api",
  "authorization_servers": ["https://example.com/api/account/oauth"],
  "scopes_supported": ["mcp", "api"],
  "bearer_methods_supported": ["header"]
}
```

The root's document exists at that one path only — there is no
protected-resource metadata beneath it, so do not probe
`/.well-known/oauth-protected-resource/api/account/user/me`.

A **404 from either document** means this installation is not offering remote
application access at that resource — no public address is configured, the
feature is switched off, or the resource path is not one this installation
hosts. It is not a transient error; stop and tell the operator.

The endpoint root is configurable (`OAUTH_SERVER_PATH`, default
`api/account/oauth`), so read the endpoints out of the metadata rather than
hard-coding them.

---

## Identifying your client

Two options. Both are public clients — **no client secret is ever issued**, and
`token_endpoint_auth_method` is always `none`.

### Client ID Metadata Document (preferred)

Publish a JSON document over https and use its URL as your `client_id`:

```json
{
  "client_id": "https://yourapp.example/oauth-client.json",
  "client_name": "Your App",
  "redirect_uris": ["http://127.0.0.1:0/callback"]
}
```

Rules the server enforces: the URL must be `https`, have a host and a path other
than `/`, and carry no query or fragment; the document must be JSON, at most
64 KiB, and its `client_id` must equal the URL **exactly**. The document is
cached for 300 seconds, so a change takes up to five minutes to take effect.

### Dynamic Client Registration (RFC 7591)

```http
POST /api/account/oauth/register
Content-Type: application/json

{
  "redirect_uris": ["http://127.0.0.1:8765/callback"],
  "client_name": "Your CLI"
}
```

`201` with:

```json
{
  "client_id": "9f2c…",
  "client_id_issued_at": 1767225600,
  "client_name": "Your CLI",
  "redirect_uris": ["http://127.0.0.1:8765/callback"],
  "token_endpoint_auth_method": "none",
  "grant_types": ["authorization_code", "refresh_token"],
  "response_types": ["code"]
}
```

**The server substitutes rather than refuses.** Ask for
`client_secret_post` and you get `none` back. Ask for `client_credentials` and
it is dropped from the echoed `grant_types`. Read what you were given; do not
assume you got what you asked for.

Only a bad `redirect_uris` is fatal (`400 invalid_redirect_uri`). At most 10
entries.

### Redirect URI rules

| Form | Accepted |
|---|---|
| `https://…` | Yes. Matched by **exact string**. |
| `http://localhost/…`, `http://127.0.0.1/…`, `http://[::1]/…` | Yes. Matched ignoring the **port**, so a CLI may bind a fresh ephemeral port each run (RFC 8252 §7.3). |
| `http://` anything else | No. |
| Custom schemes (`myapp://…`) | No. |
| Anything with a fragment, userinfo, or control characters | No. |

---

## Authorization

Open the user's browser at:

```
GET /api/account/oauth/authorize
      ?client_id=<client id>
      &redirect_uri=<one of your registered URIs>
      &response_type=code
      &state=<your opaque value>
      &code_challenge=<BASE64URL(SHA256(verifier))>
      &code_challenge_method=S256
      &resource=https://example.com/api/assistant/mcp
      &scope=mcp
```

- **PKCE is mandatory and S256-only.** `plain` and a missing challenge are
  refused. The verifier is 43–128 characters from `[A-Za-z0-9._~-]`.
- **`resource`** (RFC 8707) must be the canonical resource URL exactly as the
  protected-resource metadata reports it, and it is **echoed, never upgraded**.
  If you omit it, the server defaults to the single resource that is eligible
  for the scopes you asked for; if several are eligible, you get
  `invalid_target`.
- **`scope`** may be omitted (defaults to `mcp`), and may name `mcp`, `api`, or
  both (`scope=mcp api`). A scope this installation does not offer is
  `invalid_scope`.
- **`scope` and `resource` must agree.** `api` binds the API root and nothing
  else; `mcp` alone binds an exact endpoint and never the root. Naming the MCP
  endpoint while asking for `api` — or the root while asking for `mcp` alone —
  answers `invalid_scope`. To ask for full API access:

  ```
  &resource=https://example.com/api&scope=api
  ```

  or, for one credential that does both, `&scope=mcp api`.
- **`state`** is echoed verbatim on every response, and is capped at 512
  characters.

The user signs in on the installation's own pages if needed, then sees a consent
screen naming your client and what the access means — one sentence per scope, and
for `api` an explicit statement that the Assistant's approval step does not apply
to direct API calls. The screen also shows the
host your `redirect_uri` points at, the resource being granted, and — for a DCR
client — that your name is self-declared. Publishing a **Client ID Metadata
Document** is what replaces that caveat with "Verified from <your URL>", so
prefer CIMD if your users will be looking at this screen.

**Approve** → the browser lands on your `redirect_uri` with:

```
?code=<single-use code>&state=<yours>&iss=https://example.com/api/account/oauth
```

Verify `iss` (RFC 9207) matches the issuer you discovered before using the code.

**Deny** → the same URI with `?error=access_denied&state=…&iss=…`.

### Errors at the authorization endpoint

| Situation | What you get |
|---|---|
| Unknown/inactive `client_id`, or a `redirect_uri` you never registered | An **HTML error page, HTTP 400**. No redirect — the server will not bounce a user to an address it cannot vouch for. |
| `state` longer than 512 characters | The same rendered error. |
| Bad or missing PKCE, wrong `response_type`, unknown `scope`, unknown/ambiguous `resource` | A **302** to your `redirect_uri` with `error`, `error_description`, `state` and `iss`. |
| Server unconfigured or the resource switched off | An HTML page, HTTP 404. |

---

## Token exchange

```http
POST /api/account/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code
&code=<the code>
&code_verifier=<the PKCE verifier>
&client_id=<client id>
&redirect_uri=<the same URI, port may differ for loopback>
&resource=https://example.com/api/assistant/mcp
```

JSON bodies are accepted too. `200`:

```json
{
  "access_token": "eyJhbGciOi…",
  "token_type": "Bearer",
  "expires_in": 3600,
  "refresh_token": "hR3f…",
  "scope": "mcp api"
}
```

The access token is a JWT, but **treat it as opaque**. Its claims are an
implementation detail of this installation; the only contract is that you send
it as `Authorization: Bearer <token>` to the resource URL.

The code is **single use**. Any failed exchange — wrong verifier, wrong client,
wrong redirect, expired — burns it, and presenting a spent code is treated as
theft: the grant is revoked and a security event is recorded. Never retry an
exchange with the same code.

### Refresh

```http
POST /api/account/oauth/token

grant_type=refresh_token
&refresh_token=<the refresh token>
&client_id=<client id>
```

**The refresh token rotates on every use.** Store the new one from every
response; the old one stops working.

**If you send `scope` on a refresh, echo the string the token response gave you,
byte for byte** — the server compares it to the stored value, so `api mcp` is
not `mcp api` and a de-duplicated or re-ordered string is `invalid_grant`.
Omitting `scope` entirely is always safe.

Two things to know:

- **A lost response is forgiven for 30 seconds.** If your request succeeded but
  you never saw the answer, retrying with the same refresh token inside that
  window returns a fresh working pair. Outside it, reuse is treated as replay
  and the grant is revoked — you must send the user through consent again.

  **Store the newest refresh token before you do anything else with the
  response, and retry at most once.** A forgiven retry supersedes the token
  from the request you missed, so that superseded token becomes the tripwire: if
  it is presented later, the whole grant is revoked. That is deliberate — it is
  how a stolen refresh token is detected rather than quietly working — but it
  means a second lost response in a row is not recoverable, and re-consent is
  the only way forward.
- **The grant expires 30 days after consent, absolutely.** Refreshing does not
  extend it. Plan for re-consent.

### Revocation (RFC 7009)

```http
POST /api/account/oauth/revoke

token=<an access or refresh token>&client_id=<client id>
```

Always `200`, with an empty JSON body, whatever the token was — that is the RFC
behaviour and it means this endpoint is not an oracle.

### Token-endpoint errors

| `error` | HTTP | Meaning |
|---|---|---|
| `invalid_request` | 400 | A required parameter is missing. |
| `invalid_client` | 401 | The `client_id` is unknown, inactive, or its metadata document could not be read. |
| `invalid_grant` | 400 | The code or refresh token is not usable. Deliberately undifferentiated — unknown, expired, wrong client, wrong redirect, bad verifier, revoked and replayed all look identical. |
| `unsupported_grant_type` | 400 | Only `authorization_code` and `refresh_token` exist. |
| `invalid_target` | 400 | The `resource` does not match the one the code was bound to. |
| `not_found` | 404 | The server is unconfigured or the resource is switched off. |

---

## Using the token

```http
POST /api/assistant/mcp
Authorization: Bearer <access token>
```

**An `mcp`-only token is worth nothing at any other path** — a `401` with the
generic `Invalid token`. Do not try to use it against `/api/account/user/me`, the
API-key endpoints, or anything else; those are not oversights.

## Full API access (`api`)

A token bound to the API root with the `api` scope authenticates on **every**
endpoint beneath the root, exactly as the user's own session token would:

```http
GET /api/account/user/me
Authorization: Bearer <access token>
```

**What it equals.** The same permissions that account already has through the
API, the same group scoping, the same `440` when an endpoint wants a recent
sign-in (the token carries the `auth_time` of the session that approved it —
when it goes stale, the only way forward is re-consent). It widens nothing.

**What it cannot do:**

| Attempt | Result |
|---|---|
| `POST /api/account/oauth/approve` — approving another grant | `401`/`403`. Only a real interactive session may approve; one credential cannot mint another. |
| `POST /api/account/jwt/refresh` — trading up for a session pair | `401`. |
| Opening a WebSocket | Refused. |
| Any path **outside** the root — `/.well-known/…`, a differently-mounted app | `401`, with **no** `WWW-Authenticate` header. |
| Calling the Assistant's MCP endpoint **without** the `mcp` scope | `403 insufficient_scope`. Ask for `scope=mcp api` if you want both. |

**The approval step does not apply.** Changes an Assistant *tool* makes still
wait for an operator's approval in the Admin; a direct API call under `api` does
not. The consent screen says so.

Everything else is unchanged: the token expires in an hour, the refresh token
rotates, the grant expires 30 days after consent, and an operator can disconnect
it from the Admin at any time.

### The challenge header

A `401` from a protected resource carries:

```
WWW-Authenticate: Bearer error="invalid_token", resource_metadata="https://example.com/.well-known/oauth-protected-resource/api/assistant/mcp"
```

For a token bound to the API root the `resource_metadata` names the **root's**
document, whatever path you were refused at:

```
WWW-Authenticate: Bearer error="invalid_token", resource_metadata="https://example.com/.well-known/oauth-protected-resource/api"
```

and a `403` for a token whose scopes are insufficient carries:

```
WWW-Authenticate: Bearer error="insufficient_scope", scope="mcp"
```

`WWW-Authenticate` is listed in `Access-Control-Expose-Headers`, so a
browser-based client can read it. A `401` **without** the header means the path
is not a live protected resource — usually the feature is switched off — and
re-authenticating will not help.

### Why access can stop working

| Cause | Recovery |
|---|---|
| The token expired (1 hour) | Refresh. |
| The user's account was disabled, closed, or had its sessions revoked | The user must sign in again, then re-consent. |
| An operator revoked the grant | Re-consent. |
| A code or refresh token was replayed, or a superseded refresh token was presented after the grace window | Re-consent. |
| The resource was switched off | Nothing to do — wait for the operator. The grant is dormant, not destroyed, and refresh works again once it is back on. |

---

## Deployment note

The `/.well-known/` paths must reach the application. If a reverse proxy serves
`/.well-known/` from disk (a common ACME/Let's Encrypt arrangement), add a
location block for `/.well-known/oauth-authorization-server` and
`/.well-known/oauth-protected-resource` that proxies to the app, or discovery
returns the proxy's 404 and no client can connect.
