# OAuth / Social Login — Django Developer Reference

## Overview

OAuth2 social login is built into the framework. The full flow — CSRF state management, provider token exchange, user resolution, and JWT issuance — is handled by the framework. Your project only needs to configure credentials and (optionally) register additional providers.

**Current providers:** `google`, `apple`, `github`

All three are toggleable auth-config methods (`LOGIN_METHODS` /
`REGISTRATION_METHODS` in `services/auth_config.py`): enabled by default,
disable per group via `login.methods` / `registration.methods`, and the hosted
auth pages render a button for each enabled provider — see
[Auth Pages](auth_pages.md).

---

## Architecture

```
GET  /api/auth/oauth/<provider>/begin    ->  OAuthProvider.create_state()
                                             OAuthProvider.get_auth_url()

POST /api/auth/oauth/<provider>/complete ->  OAuthProvider.consume_state()    (CSRF check)
                                             OAuthProvider.exchange_code()    (token exchange)
                                             OAuthProvider.get_profile()      (uid + email)
                                             _find_or_create_user()           (auto-link)
                                             jwt_login(request, user)         (issue JWT)
```

Key files:

| File | Purpose |
|---|---|
| `mojo/apps/account/rest/oauth.py` | REST endpoints + auto-link logic |
| `mojo/apps/account/models/oauth.py` | `OAuthConnection` model |
| `mojo/apps/account/services/oauth/base.py` | `OAuthProvider` base class |
| `mojo/apps/account/services/oauth/google.py` | Google implementation |
| `mojo/apps/account/services/oauth/apple.py` | Apple implementation |
| `mojo/apps/account/services/oauth/github.py` | GitHub implementation |
| `mojo/apps/account/services/oauth/__init__.py` | Provider registry |

---

## Required Settings

```python
# settings.py

GOOGLE_CLIENT_ID     = "your-client-id.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "your-client-secret"

# The URL Google redirects back to after login.
# Must match an authorised redirect URI in Google Cloud Console.
OAUTH_REDIRECT_URI = "https://your-app.example.com/api/oauth/google/complete"
```

### Apple Settings

```python
APPLE_CLIENT_ID    = "com.example.web"           # Service ID from Apple Developer portal
APPLE_TEAM_ID      = "ABCD1234EF"                # 10-character Team ID
APPLE_KEY_ID       = "ABCD123456"                # Key ID from the .p8 file
APPLE_PRIVATE_KEY  = "-----BEGIN PRIVATE KEY-----\n..."  # Full PEM content of the .p8 file
```

Apple does not use a static client secret. The framework generates a short-lived ES256 JWT from these four values on every token exchange. The `.p8` private key content should be stored as a multiline string (or loaded from an environment variable) — never committed to source control.

If `OAUTH_REDIRECT_URI` is not set, the server builds it from the request `Origin` header as `<origin>/auth/oauth/<provider>/complete`. This works for single-origin SPAs but is less reliable for server-rendered or multi-origin setups — prefer the explicit setting in production. `OAUTH_REDIRECT_URI` may itself be a custom-scheme deep link; because it is an operator-configured value it is treated as vetted, so the [callback bounce](#the-callback-bounce) will emit its scheme even without an `ALLOWED_REDIRECT_URLS` entry.

### GitHub Settings

```python
GITHUB_CLIENT_ID     = "your-github-oauth-app-client-id"
GITHUB_CLIENT_SECRET = "your-github-oauth-app-client-secret"
```

GitHub does not always return an email on the `/user` endpoint — if the user has marked their email as private, the provider falls back to `GET /user/emails` and picks the primary verified address. No extra configuration is needed; the default scope `read:user user:email` covers both cases.

| Setting | Default | Purpose |
|---|---|---|
| `GITHUB_CLIENT_ID` | — | OAuth App client ID from GitHub Developer Settings |
| `GITHUB_CLIENT_SECRET` | — | OAuth App client secret |
| `GITHUB_SCOPES` | `"read:user user:email"` | OAuth scopes requested from GitHub |

### Optional Settings

| Setting | Default | Purpose |
|---|---|---|
| `GOOGLE_SCOPES` | `"openid email profile"` | OAuth scopes requested from Google |
| `OAUTH_STATE_TTL` | `600` | Seconds a CSRF state token is valid (Redis-backed) |
| `ALLOWED_REDIRECT_URLS` | `[]` | Allowlist for per-request `redirect_uri` (see below) |

---

## Per-Request redirect_uri

For multi-app deployments (a portal and a marketing site, say) where each frontend has its own callback URL, the `begin` endpoint accepts an optional `redirect_uri` query parameter.

```
GET /api/auth/oauth/google/begin?redirect_uri=https://portal.example.com/auth/callback
```

### Allowlist Configuration

A `redirect_uri` is matched as a **URL**, not as a string prefix. If no allowlist is configured and a `redirect_uri` is provided, the request returns `400`.

The allowlist has **two** sources, combined at validation time.

**Project-wide allowlist** (`settings.py`):

```python
ALLOWED_REDIRECT_URLS = [
    "https://portal.example.com/",
    "https://tenant-a.example.com/",
]
```

**Per-group allowlist** (`Group.metadata["allowed_redirect_urls"]`):

```python
group.metadata["allowed_redirect_urls"] = [
    "https://tenant-b.example.com/",
]
group.save()
```

The group list is read with `get_metadata_value()`, which traverses the parent
chain — so a child group inherits whatever its ancestors registered, and a
tenant with a group tree configures its landing origin once, at the top. The
group consulted is the one this request resolved (`?group=<id>` /
`?group_uuid=<uuid>`); with no group context, only the project-wide list
applies. See [The per-group source](#the-per-group-source) for what that means
and why it is kept.

### Matching rules

An `http(s)` entry admits a `redirect_uri` when **all four** hold. This is the
same matcher the auth-handoff destination allowlist uses
(`mojo/apps/account/services/redirect_allowlist.py`), with wildcards turned off.
Custom schemes — mobile deep links — have their own narrower rules; see
[Custom URL schemes](#custom-url-schemes-mobile-deep-links) below.

1. **Scheme** — `http` or `https` on both, and the *same* one. An `https://`
   entry never admits an `http://` landing URL; the OAuth `code` and `state`
   would otherwise travel in cleartext.
2. **Host** — equal after case-folding. `https://APP.Example.com/x` matches an
   entry of `https://app.example.com` (hosts are case-insensitive per RFC 3986);
   `https://app.example.com.evil.tld/` does not.
3. **Port** — equal, with the scheme default filled in when absent. So
   `https://app.example.com` and `https://app.example.com:443` are the same
   origin, and `https://app.example.com:8443` is a different one.
4. **Path** — the entry path, or a path underneath it terminating on a segment
   boundary. `/app` admits `/app` and `/app/inner`, and refuses `/application`.
   An entry ending in `/` admits every path on that host.
5. **No dot segments** — a `redirect_uri` (or an entry) whose path carries a `.`
   or `..` segment, in any `%2e` spelling, is **refused outright** rather than
   normalized. A browser resolves those segments before it issues the request,
   so admitting `…/callback/../../x` under an entry of `…/callback` would land
   the OAuth `code` off the prefix — degrading the segment-bounded rule above to
   host-only matching. `%2f`-encoded slashes and double-encoded `%252e` are
   deliberately *not* dot segments (a browser resolves neither) and still match.

Query and fragment are ignored on **both** sides. A `redirect_uri` may therefore
carry its own query (e.g. an app passing `?redirect=/workspaces/` through the
login page) — and so, less usefully, may an entry, where it is simply dead
weight. The full URI — query included — is stored as `frontend_uri` and
reproduced when the callback bounces the browser back: the callback merges its
`code`/`state` (and any `group_uuid`) into the existing query with `&` rather
than appending a second `?`. This is what lets a `?redirect=` target survive the
OAuth round-trip. (The bundled `mojo-auth.js` cooperates: when no explicit
callback URL is given, its default return URL keeps the current page's query
string — minus any stale `code`/`state` — and strips only the hash.)

Against an entry of `https://app.example.com`:

| `redirect_uri` | Result |
|---|---|
| `https://app.example.com/callback` | admitted |
| `https://APP.Example.com/callback` | admitted — hosts are case-insensitive |
| `https://app.example.com:443/x` | admitted — explicit default port |
| `https://app.example.com/x?to=%2Fhome` | admitted — the query is not matched |
| `https://app.example.com/v1.2/callback` | admitted — dots inside a label are not a `.`/`..` segment |
| `https://app.example.com/callback/../../x` | **refused** — a `..` path segment resolves out of the prefix before the request |
| `https://app.example.com.evil.tld/` | **refused** — a different host that merely begins with the entry |
| `https://app.example.com@evil.tld/` | **refused** — userinfo; the real host is `evil.tld` |
| `https://evil.tld\@app.example.com/` | **refused** — backslash; a browser reads the host as `evil.tld` |
| `https://app.example.com%2Eevil.tld/` | **refused** — percent-encoded host separator |
| `https://app.example.com./` | **refused** — the trailing dot is not normalized away |
| `https://app.example.com:8443/x` | **refused** — different port |
| `http://app.example.com/x` | **refused** — no scheme downgrade |
| `//app.example.com/x` | **refused** — no scheme to compare |

Two further rules:

- **List IDN hosts in punycode** (`https://xn--80ak6aa92e.example/`). A unicode
  host is refused rather than guessed at, along with bracketed IPv6 literals —
  a host this deployment cannot read the same way a browser does is not a host
  it should be sending tokens to.
- **Wildcards are not supported here.** A `*.example.com` entry is skipped as
  unusable, with a log line naming it. (`AUTH_HANDOFF_ALLOWED_URLS` *does*
  support `*.`; this list deliberately does not — see the CHANGELOG entry.)

Entries that cannot be parsed as an absolute URL — `""`, `"h"`, `"/relative"` —
are skipped with a warning and can never match anything. Under the prefix test
they replaced, an entry of `"h"` admitted every `http(s)://` URL in existence. An
entry whose own path carries a `.`/`..` segment is dropped the same way, so
`https://app.example.com/oauth/callback/../..` matches nothing (it would
otherwise degrade to host-only matching once a browser resolved it).

### Custom URL schemes (mobile deep links)

An entry may name a custom scheme, so a native app can complete OAuth on its own
deep link:

```python
ALLOWED_REDIRECT_URLS = [
    "https://portal.example.com/",
    "myapp://callback",              # the app's registered deep link
    "com.example.app:/oauth",        # the reverse-DNS form, empty authority
]
```

A custom-scheme URL is **not** a web origin, so none of the four rules above
apply to it. It matches when all three hold:

1. **Scheme** — equal after case-folding, and *not* `http`/`https`. Schemes are
   compared before anything else, so a custom-scheme entry can never admit an
   `http(s)` URL and an `http(s)` entry can never admit a deep link.
2. **Authority** — equal after case-folding, compared **byte-for-byte**. There
   is no default-port logic (a custom scheme has no default port) and no
   hostname rules (the authority is a label the app registered with the OS, not
   a DNS name). Byte comparison is also what makes the web confusables inert
   here: `myapp://evil@callback` is simply a *different authority*, not a
   rewriting of `callback`.
3. **Path** — the same segment-bounded prefix rule as above. `myapp://callback`
   (path `/`) admits every path under that authority; `myapp://callback/oauth`
   admits `/oauth` and `/oauth/done` but not `/oauthdone`.

Query and fragment are ignored, exactly as for `http(s)`, and the backslash
guard applies to every scheme.

`com.example.app:/oauth` and `com.example.app:///oauth` are the **same value** —
both parse to an empty authority with the path `/oauth`. An empty authority is a
real value, not a wildcard: it never equals `callback`.

Refused, fail-closed:

| Entry or `redirect_uri` | Why |
|---|---|
| `myapp:callback`, `mailto:a@b` | the opaque form — no `//` and no leading `/`, so there is no way to tell an authority from a path |
| `myapp:`, `myapp://` | neither authority nor path; as an entry it would authorize a whole scheme |
| `javascript:…`, `data:…`, `vbscript:…` | a navigation sink, never a destination — refused even if an operator lists one |
| `myapp://callback/../..`, `myapp://callback/%2e%2e/x` | a `.`/`..` path segment (any `%2e` spelling) — refused on every scheme, and a dot-segment deep-link entry is unusable |
| `myapp://*.callback` | `*.` is not a wildcard here; it is an authority nothing equals |

The same rules apply to `AUTH_HANDOFF_ALLOWED_URLS`, since the two lists share
one matcher. That is intended — a per-list scheme policy is exactly the drift
the shared implementation exists to prevent. A handoff code is a bearer
credential, so treat a deep-link handoff entry with the same care as a web one:
the OS decides which installed app receives that scheme.

#### The callback bounce

Admitting a deep link at `/begin` is only half the flow — the browser still has
to *land* on it. After the provider redirects to `/callback`, the server 302s the
browser to the `frontend_uri` with `code`/`state` appended, and that redirect
must carry the custom scheme. Django's stock redirect refuses any scheme outside
`http`/`https`/`ftp`, so the bounce is widened deliberately and narrowly:

- The bounce always permits `http(s)`. It permits **exactly one** custom scheme
  on top — the one recorded in the OAuth state at `/begin` time, and only when
  re-parsing the final `Location` URL yields that same scheme.
- The recorded scheme is trusted only when it came from a vetted provenance: the
  caller's `redirect_uri` cleared the allowlist (`ALLOWED_REDIRECT_URLS` plus the
  group source), **or** the `frontend_uri` byte-equals the deployment's own
  `OAUTH_REDIRECT_URI`. An **Origin-derived** landing — the default when no
  `redirect_uri` is passed, built from the unvalidated `Origin` header — is never
  trusted, so its scheme never widens the bounce.
- A **pre-upgrade state** (minted before this behavior existed, so carrying no
  recorded scheme) bounces `http(s)`-only. Deep-link logins in flight at upgrade
  time fail for at most `OAUTH_STATE_TTL` (default 600s), after which every state
  is a new one that carries the scheme.
- Anything else — an unrecorded or mismatched scheme, a URL Django still refuses —
  returns a **400 `Cannot return to the redirect_uri in this OAuth state`**
  rather than the 500 a raw `DisallowedRedirect` would produce. `http(s)` landings
  are byte-for-byte unaffected: they record no scheme and bounce exactly as before.

### The per-group source

`Group.metadata["allowed_redirect_urls"]` exists so a white-label tenant can
self-serve the origin its own login page lands on, without a deploy of the
platform. Two properties of it are worth stating plainly, because they are
deliberate and were decided with eyes open:

- Plain `metadata` is writable by any holder of `manage_group` on that group
  (only `metadata["protected"]` is gated by `PROTECTED_JSON_PERMS`), so the
  value is **tenant-writable**.
- `begin` is a public endpoint, and the caller — including an anonymous one —
  picks which group applies by passing `?group=<id>` / `?group_uuid=<uuid>`, so
  the list that applies is **caller-selectable**.

Together those mean a tenant can authorize an OAuth landing origin and any
caller can select that tenant's list. That residual risk is accepted. What
bounds it is the matcher: entries from *both* sources go through the same
parsed-URL match, so an entry authorizes the **exact host it names** and nothing
that merely begins with it. A tenant blesses an origin it already controls; it
cannot reach a neighbour's. Domain verification, moving the key under
`metadata["protected"]`, and scoping the read to group members were each
considered and declined — the last is not even available on a public endpoint
whose whole purpose is a signed-out white-label login page.

The two sources are combined, never substituted: the project-wide list always
applies, and the group's entries are added on top when a group resolved.
`ALLOWED_REDIRECT_URLS` itself is read through `settings.get`, so it may be set
in `settings.py` **or** as a global `Setting` row:

```python
from mojo.apps.account.models.setting import Setting
Setting.set("ALLOWED_REDIRECT_URLS",
            '["https://portal.example.com/", "https://tenant-a.example.com/"]')
```

A `Setting` row holds text, so the value is read with `kind="list"`: a JSON
array (above) and a comma-separated string (`"https://a.example/,https://b.example/"`)
both work. A **group-scoped** `Setting` row is never consulted for this key —
per-group entries live in group metadata, not in a scoped setting.

Write the group value as a **list**. A bare string is stored happily (`metadata`
is a JSONField and nothing coerces it), but it is then spread one character at
a time into the combined list, where every character is refused as an unusable
entry — so the tenant silently gets no entry at all rather than the one it
meant.

The bundled hosted auth pages do not exercise this path: `mojo-auth.js` folds
the page's query string *inside* the encoded `redirect_uri`, so `group_uuid`
never becomes a top-level parameter on `begin` and no group is resolved there.
The per-group list is for a custom frontend that deliberately passes group
context.

### Security

- The validated `redirect_uri` is stored in the Redis state token (single-use, TTL-bound).
- The `complete` endpoint retrieves it from the state — the client never re-sends it.
- This prevents an attacker from substituting a different `redirect_uri` in the callback.
- The match compares scheme, host, port and path, so the query part of a
  `frontend_uri` is **not** validated. That is why the callback strips any
  `code`/`state`/`group_uuid` the caller smuggled into that query before
  appending the server's own — otherwise a duplicate `?code=` placed first would
  shadow the real value (`URLSearchParams.get()` returns the first match) and
  sabotage the victim's login. Tightening the host match did not retire that
  strip; an allowlisted URL can still carry any query at all.

---

## OAuthConnection Model

```python
# mojo/apps/account/models/oauth.py

class OAuthConnection(MojoSecrets, MojoModel):
    user         = ForeignKey(User, related_name="oauth_connections")
    provider     = CharField(max_length=32)      # e.g. "google"
    provider_uid = CharField(max_length=255)     # provider's stable user ID (e.g. Google "sub")
    email        = EmailField(null=True)         # email as reported by provider at link time
    is_active    = BooleanField(default=True)
    created      = DateTimeField(auto_now_add=True)
    modified     = DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("provider", "provider_uid")]
```

Access/refresh tokens from the provider are stored encrypted in `mojo_secrets` (via `MojoSecrets`). They are refreshed on every successful OAuth login. They are never exposed in REST graphs.

**One connection per (user, provider) pair.** A user who has connected Google once will always reuse that connection on subsequent logins.

### REST Permissions

`OAuthConnection` exposes a REST endpoint via `MojoModel`:

- **View:** `owner` (the connected user), `manage_users`, or the combined `users` term
- **Save/Delete:** `manage_users` or the combined `users` term (it includes `manage_users` by definition)
- `mojo_secrets` is always excluded from REST output (`NO_SHOW_FIELDS`)

---

## Auto-Link Logic

`_find_or_create_user(provider_name, profile)` resolves which account to use, in priority order:

1. **Existing `OAuthConnection`** for this `(provider, provider_uid)` → return that user directly
2. **Existing `User` with matching email** → create a new `OAuthConnection` linking this provider to that user
3. **No match** → create a new `User` + `OAuthConnection`

### Email Verification on Auto-Link

OAuth is treated as a trusted identity provider. When any of the three paths above runs, the framework guarantees `is_email_verified=True` on the resolved user:

- **Path 1 (existing connection):** no change to `is_email_verified` — the user was already verified when they first connected
- **Path 2 (email match):** if `is_email_verified` is currently `False`, it is set to `True` and saved — the provider has confirmed ownership of the address
- **Path 3 (new user):** `is_email_verified` is set to `True` at account creation time

This means a user who has never clicked a verification email will be automatically marked verified the first time they log in via OAuth. This is intentional: OAuth provider confirmation is considered equivalent to email link verification.

**`is_email_verified` is a `SUPERUSER_ONLY_FIELDS` write.** It cannot be cleared by a normal REST update. The only code paths that set it are internal (token flows, OAuth, magic login).

---

## MFA Behaviour

**OAuth logins bypass MFA.** A user with `requires_mfa=True` (TOTP/SMS enrolled) is not presented with an MFA challenge after completing an OAuth login. The JWT is issued directly.

### Rationale

OAuth is a trusted second factor in its own right:

- The user has already authenticated to a third-party identity provider (Google, etc.)
- The provider may have enforced its own MFA (Google Advanced Protection, Workspace policies, etc.)
- The CSRF `state` token (Redis-backed, single-use, 10-minute TTL) prevents replay and CSRF attacks
- The authorization `code` is exchanged server-side — it never passes through the browser unprotected

Requiring an *additional* TOTP or SMS step after a successful OAuth assertion would be redundant and would harm UX without meaningfully improving security. If your project has a strict policy requiring a local second factor regardless of OAuth, you can override `on_oauth_complete` in a project-level URL handler.

---

## Adding a New Provider

1. Create `mojo/apps/account/services/oauth/<provider>.py` subclassing `OAuthProvider`:

```python
# services/oauth/myprovider.py
import requests
from urllib.parse import urlencode, quote

from mojo.helpers import logit
from mojo.helpers.settings import settings
from .base import OAuthProvider

AUTH_URL  = "https://provider.example.com/oauth/authorize"
TOKEN_URL = "https://provider.example.com/oauth/token"
USER_URL  = "https://api.provider.example.com/user"


class MyOAuthProvider(OAuthProvider):

    name = "myprovider"

    def get_auth_url(self, state, redirect_uri):
        params = {
            "client_id": settings.get("MYPROVIDER_CLIENT_ID"),
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": "user email",
        }
        return f"{AUTH_URL}?{urlencode(params, quote_via=quote)}"

    def exchange_code(self, code, redirect_uri):
        resp = requests.post(TOKEN_URL, json={
            "client_id": settings.get("MYPROVIDER_CLIENT_ID"),
            "client_secret": settings.get("MYPROVIDER_CLIENT_SECRET"),
            "code": code,
        }, headers={"Accept": "application/json"}, timeout=10)
        if not resp.ok:
            logit.error("oauth.myprovider", f"Token exchange failed: {resp.status_code}")
            raise ValueError("Failed to exchange code")
        return resp.json()

    def get_profile(self, tokens):
        resp = requests.get(USER_URL, headers={
            "Authorization": f"Bearer {tokens['access_token']}",
            "Accept": "application/json",
        }, timeout=10)
        if not resp.ok:
            logit.error("oauth.myprovider", f"Profile fetch failed: {resp.status_code}")
            raise ValueError("Failed to fetch profile")
        data = resp.json()
        email = (data.get("email") or "").lower().strip()
        # Some providers don't return email directly — add a fallback here if needed
        if not email:
            raise ValueError("Could not retrieve verified email from provider")
        return {
            "uid": str(data["id"]),
            "email": email,
            "display_name": data.get("name"),
        }
```

> **Note on the `/user/emails` fallback:** GitHub may not return an email on the `/user` endpoint when the user's email is set to private. The built-in `GitHubOAuthProvider` handles this by falling back to `GET /user/emails` and picking the entry where `primary=True` and `verified=True`. If your provider has a similar pattern, add an equivalent fallback in `get_profile()` before raising.

2. Register it in `services/oauth/__init__.py`:

```python
from .myprovider import MyOAuthProvider

PROVIDERS = {
    "google": GoogleOAuthProvider,
    "apple": AppleOAuthProvider,
    "github": GitHubOAuthProvider,
    "myprovider": MyOAuthProvider,
}
```

3. Add settings:

```python
MYPROVIDER_CLIENT_ID     = "your-client-id"
MYPROVIDER_CLIENT_SECRET = "your-client-secret"
```

The new provider is immediately available at:
- `GET /api/auth/oauth/myprovider/begin`
- `POST /api/auth/oauth/myprovider/complete`

No URL registration or model changes are required.

---

## CSRF State Token

Each OAuth flow begins with a `state` token stored in Redis (key prefix `oauth:state:`). The token is:

- Generated as a random UUID hex string
- Stored in Redis with a TTL of `OAUTH_STATE_TTL` seconds (default 600)
- Consumed (deleted) immediately on use — single-use

If `consume_state()` returns `None` (expired, already used, or forged), the complete endpoint raises a `401`. This protects against CSRF and replay attacks regardless of provider.

Redis is a hard dependency for the OAuth flow. If Redis is unavailable, `begin` will raise and no state token will be issued.

---

## Incident Logging

All OAuth events are logged via `logit` under the `"oauth"` category:

| Event | Level |
|---|---|
| New user created via OAuth | `info` |
| Existing user email marked verified via OAuth | `info` |
| Successful login | `info` |
| Token exchange failure (provider error) | `error` |
| Profile fetch failure (provider error) | `error` |

These appear in your standard Mojo log output. Failed logins (invalid state, disabled account) raise `PermissionDeniedException` which is surfaced to the client as `401`/`403` and also recorded automatically by the framework's incident system.

---

## Security Design Notes

- **No password is created** for OAuth-only users. If a user registers via OAuth and later wants password login, they must use the "forgot password" / reset flow to set one.
- **`provider_uid` is the stable identifier**, not the email. A user who changes their Google email address is still matched correctly on the next OAuth login via the existing `OAuthConnection`.
- **Access and refresh tokens are stored encrypted** in `mojo_secrets` and updated on every login. They are available for server-side API calls on behalf of the user if needed, but are never exposed in REST responses.
- **`is_email_verified` cannot be downgraded** via REST by a non-superuser. Once set by OAuth (or any other trusted flow), it stays set.

---

## See Also

- [OAuth REST API](../../web_developer/account/oauth.md) — client-facing flow, JavaScript examples, error table
- [Authentication Flow](auth.md) — JWT tokens, MFA, password reset
- [User Model](user.md) — `is_email_verified`, `requires_mfa`, `SUPERUSER_ONLY_FIELDS`
