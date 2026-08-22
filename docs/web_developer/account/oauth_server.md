# Connecting an app with OAuth 2.1

This installation can act as an OAuth 2.1 authorization server, so an
application — an AI agent, a CLI, an MCP host — can act on a user's behalf at a
specific protected endpoint without anyone ever copying a secret.

The flow is the standard one: discover, register, send the user to the
installation's own sign-in and consent pages, exchange a PKCE-bound code for
tokens, refresh, revoke.

**The credentials you receive are confined.** An access token issued here works
at the one resource URL it names and nowhere else on the API. Presenting it to
any other endpoint returns `401`. That is by design, not a bug to route around.

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
  "scopes_supported": ["mcp"],
  "authorization_response_iss_parameter_supported": true,
  "client_id_metadata_document_supported": true
}
```

**Protected-resource metadata:**

```json
{
  "resource": "https://example.com/api/assistant/mcp",
  "authorization_servers": ["https://example.com/api/account/oauth"],
  "scopes_supported": ["mcp"],
  "bearer_methods_supported": ["header"]
}
```

A **404 from either document** means this installation is not offering remote
application access — either no public address is configured or the feature is
switched off. It is not a transient error; stop and tell the operator.

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
  protected-resource metadata reports it. If you omit it and the installation
  has exactly one resource enabled, that one is used; if several are enabled,
  you get `invalid_target`.
- **`scope`** may be omitted (defaults to `mcp`). Any other value is
  `invalid_scope`.
- **`state`** is echoed verbatim on every response, and is capped at 512
  characters.

The user signs in on the installation's own pages if needed, then sees a consent
screen naming your client and what the access means.

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
  "scope": "mcp"
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

Two things to know:

- **A lost response is forgiven for 30 seconds.** If your request succeeded but
  you never saw the answer, retrying with the same refresh token inside that
  window returns a fresh working pair. Outside it, reuse is treated as replay
  and the grant is revoked — you must send the user through consent again.
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

**At any other path this token is worth nothing** — a `401` with the generic
`Invalid token`. Do not try to use it against `/api/account/user/me`, the API-key
endpoints, or anything else; those are not oversights.

### The challenge header

A `401` from a protected resource carries:

```
WWW-Authenticate: Bearer error="invalid_token", resource_metadata="https://example.com/.well-known/oauth-protected-resource/api/assistant/mcp"
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
| A code or refresh token was replayed | Re-consent. |
| The resource was switched off | Nothing to do — wait for the operator. The grant is dormant, not destroyed, and refresh works again once it is back on. |

---

## Deployment note

The `/.well-known/` paths must reach the application. If a reverse proxy serves
`/.well-known/` from disk (a common ACME/Let's Encrypt arrangement), add a
location block for `/.well-known/oauth-authorization-server` and
`/.well-known/oauth-protected-resource` that proxies to the app, or discovery
returns the proxy's 404 and no client can connect.
