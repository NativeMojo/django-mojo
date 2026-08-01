# Authentication Flow — Django Developer Reference

## Overview

Authentication is JWT-based. `AuthMiddleware` validates Bearer tokens on every request and populates `request.user`. All REST endpoints in `mojo/apps/account/rest/user.py` handle the full auth lifecycle.

## Login

**Endpoint:** `POST /api/login` (also: `/api/auth/login`)

Required params: `username`, `password`

```python
# Pseudocode of what the framework does
user = User.objects.filter(Q(username=username) | Q(email=username)).last()
if not user.check_password(password):
    raise PermissionDeniedException()
token_package = JWToken(user.get_auth_key()).create(uid=user.id, ip=request.ip)
```

Returns `access_token`, `refresh_token`, and a `user` dict. Token lifetimes are carried in each JWT's `exp` claim — they are not returned as a separate field.

## `jwt_login` Helper

All login endpoints (password, OAuth, magic link, MFA complete, invite accept) issue tokens via the shared `jwt_login` helper:

```python
from mojo.apps.account.rest.user import jwt_login

return jwt_login(request, user)
```

### Extra response data

Pass an `extra` dict to merge additional fields into the response `data` without polluting the JWT payload:

```python
return jwt_login(request, user, extra={"is_new_user": True})
```

The JWT payload stays clean — `extra` fields are only in the HTTP response body. This is how the OAuth flow signals a newly-created account to the frontend.

### Webapp URL tracking

On every `jwt_login` call the framework captures the frontend origin from `request.DATA["webapp_base_url"]` or `HTTP_ORIGIN` and stores it on the user:

- `user.metadata["protected"]["orig_webapp_url"]` — set once at first login, never overwritten
- `user.metadata["protected"]["last_webapp_url"]` — updated on every subsequent login

These are used as a fallback in the `build_token_url` lookup chain (see [Token URLs](#token-urls)).

### Geofence enforcement

`jwt_login` is also the post-credential geofence enforcement point (DM-043):
its first statement checks the geofence engine with the now-verified `user`
and, on block, returns the standard geofence 403 before `last_login`, the
`UserLoginEvent`, or `USER_LOGIN_HANDLER` fire — a blocked login has zero
success side effects. This only applies to `source`s not listed in
`GEOFENCE_EXEMPT_JWT_SOURCES` (`sessions_revoke`, `email_change` — authed
re-issues, not logins). See [Geofencing — Post-credential
enforcement](geofence.md#post-credential-enforcement-after_authtrue) for the
full contract, including the MFA-branch check in `on_user_login` and the
token-proven-action behavior (password reset / email verify / invite accept
apply their mutation before the session is withheld).

---

## MFA Challenge (Login with MFA enabled)

MFA is opt-in per user via the `requires_mfa` boolean field (default `False`). Your app sets this when creating or updating users — the framework never forces it automatically. When `requires_mfa=True`, the login endpoint does **not** return a JWT. Instead it returns an MFA challenge:

```json
{
  "status": true,
  "data": {
    "mfa_required": true,
    "mfa_token": "<short-lived token>",
    "mfa_methods": ["sms"],
    "expires_in": 300
  }
}
```

The client must detect `mfa_required: true` and route the user to the appropriate second factor.

**MFA methods:**
- `"sms"` — user has a verified `phone_number`; use the SMS OTP flow
- `"totp"` — user has an active TOTP device; use the TOTP flow (enrolling TOTP auto-sets `requires_mfa=True`)
- `"passkey"` — user has a registered passkey; can be used as second factor

**Completing MFA:**
- SMS: `POST /api/auth/sms/verify` with `mfa_token` + `code`
- TOTP: `POST /api/auth/totp/verify` with `mfa_token` + `code`

Both return the standard JWT response (`access_token`, `refresh_token`, `user`) on success.

The `mfa_token` is single-use and expires in `expires_in` seconds (default 300).

## Token Refresh

**Endpoint:** `POST /api/refresh_token` (also: `/api/token/refresh`)

Required param: `refresh_token`

Validates the refresh token and issues a new token pair.

## Password Reset

Two flows supported:

### Code-based (OTP)
1. `POST /api/auth/forgot` with `email` + `method=code`
2. 6-digit code emailed, stored encrypted in user secrets
3. `POST /api/auth/password/reset/code` with `email`, `code`, `new_password`
4. Returns new JWT on success

### Link-based
1. `POST /api/auth/forgot` with `email` + `method=link`
2. Signed token emailed
3. `POST /api/auth/password/reset/token` with `token`, `new_password`
4. Returns new JWT on success

## Magic Login

Passwordless login via a signed single-use `ml:` token, delivered by email or SMS.

```python
from mojo.apps.account.utils.tokens import generate_magic_login_token

# Email (default)
token = generate_magic_login_token(user)
user.send_template_email("magic_login_link", {"token": token})

# SMS
token = generate_magic_login_token(user, channel="sms")
phonehub.send_sms(user.phone_number, f"Your login token: {token}")
```

`verify_magic_login_token(token)` returns `(user, channel)` — the channel is whichever was passed to `generate_magic_login_token`. On success the framework automatically marks `is_email_verified` or `is_phone_verified` depending on the channel.

Tokens are single-use and expire after `MAGIC_LOGIN_TOKEN_TTL` seconds (default 3600). The channel is stored encrypted in `mojo_secrets` and cleared on consume.

See the [Magic Login REST API](../../web_developer/account/magic_login.md) for the full client-facing flow.

## Cross-Origin Auth Handoff

When the auth page (`/auth`) and the consuming app live on different origins,
`localStorage` is partitioned, so the destination cannot read tokens minted at
the auth origin. The handoff service mints a short-lived, single-use code that
the auth page appends to the redirect URL; the app exchanges it for a JWT.

```python
from mojo.apps.account.services import auth_handoff, redirect_allowlist

# Issued from the authenticated POST /api/auth/handoff handler. Destination
# ENFORCEMENT IS OPT-IN and off until a deployment configures it — see below.
destination = request.DATA.get("redirect_uri")
if redirect_allowlist.is_enforced():
    if not destination:
        raise merrors.ValueException("redirect_uri is required for auth handoff")
    if not redirect_allowlist.is_allowed_destination(destination, request):
        redirect_allowlist.report_unlisted_destination(
            destination, request=request, enforced=True)
        raise merrors.ValueException("redirect_uri is not permitted for auth handoff")
elif destination and not redirect_allowlist.is_allowed_destination(destination, request):
    # Monitor mode: mint anyway — that is the pre-existing behavior — but file
    # an incident naming the destination.
    redirect_allowlist.report_unlisted_destination(
        destination, request=request, enforced=False)
code = auth_handoff.create_handoff_code(request.user, destination=destination, ip=request.ip)

# Consumed by the public POST /api/auth/exchange handler
data = auth_handoff.consume_handoff_code(code)
# -> {"uid": <id>, "ip": "...", "dest": "https://app.example.com/"} or None
```

Codes are 32-hex random strings stored under Redis key `auth:handoff:<code>`,
with a TTL controlled by the `AUTH_HANDOFF_CODE_TTL` setting (default `60`
seconds). Single-use is enforced atomically via Redis `GETDEL`, so concurrent
exchange attempts cannot both win. The exchange endpoint reuses
`jwt_login(request, user, source="handoff")`, so the standard login-event
tracking, last-login bump, and webapp-URL metadata all fire on the handoff
exchange.

### The destination is decided at issuance

A handoff code buys an access **and** refresh token pair, so where it is going
is decided when it is minted, and nowhere else. Neither `dest` nor `ip` is
re-checked on consume: `POST /api/auth/exchange` is a server-to-server call from
the consuming app's own backend, which chooses its own egress IP and its own
headers, so an `Origin`/`Referer` check there would reject honest callers and
stop nobody who already holds the code. Both stored fields are audit records.

### Enforcement is OPT-IN — the setting is the switch

Whether that decision is *binding* is a deployment choice, and it is **off until
you make it**. As with [`JOBS_ALLOWED_CHANNELS`](../jobs/settings.md#channels),
the setting itself is the switch — there is no flag day:

| State | `redirect_uri` | Destination not on the allowlist | Incident filed |
|---|---|---|---|
| **Neither `AUTH_HANDOFF_ALLOWED_URLS` nor `AUTH_HANDOFF_RESOLVER` set** — *monitor mode*, what every deployment upgrades into | optional | code is **minted anyway**, exactly as before | `auth:handoff_destination_unlisted`, level 3 |
| **Either one set** — a resolver dotted path, or a list, **even an empty one** — *enforced* | **required**, `400` when missing | `400`, **no code minted** | `auth:handoff_destination_refused`, level 3 |

Both incidents are suppressed to **one per destination host per hour**, so a
crafted-link flood cannot spam the incident plane — the host is the useful unit,
since that is what goes in the allowlist. The body names the destination, so in
monitor mode the incident feed writes `AUTH_HANDOFF_ALLOWED_URLS` for you.

Expect noise on day one: in monitor mode there is no list, so **every**
destination is unlisted and each distinct destination host produces one incident
per hour until you list it. That is the feature — the feed converges on exactly
the set of hosts you need to allow. A handoff with no `redirect_uri` at all
reports nothing (there is no destination to name) and still mints.

`redirect_allowlist.is_enforced()` is the mode predicate.
`is_allowed_destination()` answers allow/deny **only** and never considers the
mode — callers pair the two, which is why a `False` in monitor mode is reported
rather than acted on.

`matches_allowlist(url, entries, source="allowlist", allow_wildcard=False)` is
the matcher underneath, in its pure form: it reads no settings and takes no
request, just "does this URL match one of these entries". It never raises.
Reach for it when you already hold the entries — the OAuth `redirect_uri`
allowlist is the second in-tree caller, which is what keeps one implementation
behind two lists. Reach for `is_allowed_destination()` instead when you want the
handoff list *plus* any configured resolver. `source` only names the setting in
the "ignoring unusable entry" warning (logged once per distinct entry, not once
per request); `allow_wildcard` decides whether a `*.host` entry is honored or
dropped as unusable.

Same-origin and relative redirects are unaffected in either mode — they never
mint a code, because the tokens already live in `localStorage` on the
destination origin.

#### The scheme is refused client-side, in both modes

Before any of the above runs, the hosted auth page itself refuses a destination
that does not resolve to `http:`/`https:` — **no request is made**, the page
navigates nowhere, and it shows "That destination isn't allowed. Please return
to the app and try again." The check lives in `auth_base.html` and is the same
guard `?back=` uses.

This matters to an operator reading this page because monitor mode mints for
anything: monitor mode is no longer the only thing standing between a
`javascript:` destination and the browser's navigation sink, and the guard also
covers the direct-navigation branch, which does not consult the server at all.
It is a **scheme** check only — the destination host is not restricted by it,
which stays the job of the opt-in allowlist above.

One consequence to know before upgrading: the guard runs on the group's
`theme.success_redirect` too, not only on a `?redirect=` param. A group whose
success redirect uses a custom app scheme (`myapp://home`) dead-ends sign-in on
**every** attempt, not just on crafted links — point it at an `https`
universal/app link instead.

**The browser guard and the allowlist disagree about custom schemes, and that
is deliberate.** A custom scheme *is* a usable `AUTH_HANDOFF_ALLOWED_URLS` entry
(see [Settings](#settings) below), but the bundled auth pages never reach the
server to find out: `safeNavUrl` refuses it in the browser, in both modes,
whatever the allowlist says. So listing `myapp://callback` buys a valid handoff
destination for a **custom frontend** calling `POST /api/auth/handoff` itself —
not for the shipped pages. The OAuth `redirect_uri` on
`GET /api/auth/oauth/<provider>/begin` is a different parameter on a different
endpoint and does not pass through this guard at all, so a native-app OAuth flow
landing on a deep link is unaffected; see
[OAuth § Custom URL schemes](oauth.md#custom-url-schemes-mobile-deep-links).

#### Rolling enforcement out

1. **Upgrade.** Nothing breaks. With neither setting present you are in monitor
   mode and the endpoint mints exactly as it always has.
2. **Watch the incident feed** for `auth:handoff_destination_unlisted`. Each one
   names a destination that is currently receiving token-buying codes.
3. **Add the legitimate ones** to `AUTH_HANDOFF_ALLOWED_URLS` (or write a
   resolver, below).
4. **You are now enforcing.** The setting exists, so anything unlisted is
   refused with a `400` instead of reported. There is no third setting to flip.

Turning enforcement back off means removing **both** settings.
`AUTH_HANDOFF_ALLOWED_URLS = []` is not "no opinion" — it is "enforce, and allow
nothing".

#### What enforcement actually buys you — and what it does not

Worth being honest about, because the answer is narrower than "stops phishing".

**It closes a specific one-click token leak.** An attacker sends an
already-signed-in user a link to *your own* auth page carrying
`?redirect=https://attacker.example/`. The page has a live session, so it mints
a handoff code — good for an access **and** refresh token pair — and the browser
navigates straight to the attacker's origin with that code in the URL. The
victim types nothing, the URL bar shows your real domain the whole time, and
there is no credential prompt to be suspicious of. **A password manager, a
passkey and a 2FA prompt do not stop this**, because none of them is involved:
the user is already authenticated and never enters anything.

**It is not a general anti-phishing control.** Nothing here stops an attacker
from building their own login page against your public REST API and phishing
your users directly. That attack is always available, needs no `?redirect=`
param, and against a password-only user population it yields the attacker *more*
than a handoff code does — the password itself, reusable and not expiring in 60
seconds.

So: worth turning on, cheap once the incident feed has named your destinations,
and a narrow, specific hardening rather than a category of protection.

### Settings

| Setting | Default | Purpose |
|---|---|---|
| `AUTH_HANDOFF_CODE_TTL` | `60` | Seconds before a handoff code expires |
| `AUTH_HANDOFF_ALLOWED_URLS` | **unset** (monitor mode) | Static list of allowed destination URLs. **Setting it — even to `[]` — turns enforcement on** |
| `AUTH_HANDOFF_RESOLVER` | `""` | Dotted path to `fn(url, request=None) -> bool`. **When set it decides**, the static list is not consulted, and enforcement is on |

The matching rules below define what "allowed" means. They run in **both**
modes — in monitor mode the same evaluation decides whether an incident is
filed, it just does not decide whether the code is minted.

Static entries match on **exact host + path prefix**:

```python
AUTH_HANDOFF_ALLOWED_URLS = [
    "https://app.example.com/",        # any path on that host
    "https://portal.example.com/app",  # /app and /app/... only, not /application
    "https://*.tenants.example.com/",  # one extra dot-free label, plus the base host
]
```

- Scheme **must match the entry** — an `https://` entry never admits an
  `http://` destination. A **custom scheme** (`myapp://callback`, a mobile deep
  link) is also a usable entry, matched on exact scheme + exact case-folded
  authority + the same path rule, with no default ports and no wildcards; it can
  never admit an `http(s)` URL, or vice versa. `javascript:`, `data:` and
  `vbscript:` are refused outright — a navigation sink is never a destination.
  A handoff code is a bearer credential, so a deep-link entry deserves the same
  scrutiny as a web one: the OS decides which installed app receives that scheme.
- Host comparison is case-folded and exact. `*.example.com` admits
  `example.com` and `a.example.com`, but **not** `a.b.example.com` and **not**
  `example.com.evil.tld`.
- Path matching stops at a segment boundary, so `/app` does not admit
  `/application`. Query strings are ignored. Host-only matching was
  deliberately rejected — it would turn every open redirector, query reflector
  and analytics beacon on an allowed host into a token-deposit site. A path
  carrying a `.`/`..` segment (in any `%2e` spelling) is **refused outright**
  rather than normalized, on both the destination and every entry — a browser
  resolves it out of the prefix before the request, so admitting one would
  degrade this segment boundary to host-only matching.
- Hostnames must be plain ASCII host characters; list IDN destinations in
  punycode. That closes the parser-differential class where Python keeps a
  character inside the host that a browser treats as an authority terminator.

`AUTH_HANDOFF_ALLOWED_URLS` is deliberately **separate** from
`ALLOWED_REDIRECT_URLS` (the OAuth `redirect_uri` allowlist). Operators wrote
those values under different semantics — notably, wildcards are inert there and
live here — so handoff destinations are never inherited from them. Note the
side effect of the opt-in design: an existing `ALLOWED_REDIRECT_URLS` does not
put you into handoff enforcement, and adding a handoff entry for the first time
does. What the lists share is the matcher: both go through
`redirect_allowlist.matches_allowlist`, so the rules above apply verbatim to
`ALLOWED_REDIRECT_URLS`, minus wildcard support. Separate *values*, one
implementation, so the next hardening lands on both.
Where they differ is the source. `AUTH_HANDOFF_ALLOWED_URLS` is **deployment
configuration only**; `ALLOWED_REDIRECT_URLS` is combined with a per-group
`Group.metadata["allowed_redirect_urls"]`, which is tenant-writable and selected
by the caller's `?group=` — deliberate, so a white-label tenant can self-serve
its OAuth landing origin. See
[OAuth](oauth.md#the-per-group-source). A handoff destination is never inherited
from either of those.

### Supplying a resolver instead of a list

A multi-tenant platform that cannot enumerate destinations in a settings file
implements one function against its own domain registry:

```python
# myapp/services/handoff.py
def allow_tenant_destination(url, request=None):
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if parts.scheme != "https":
        return False
    return TenantDomain.objects.filter(
        hostname=(parts.hostname or "").lower(), is_active=True).exists()
```

```python
AUTH_HANDOFF_RESOLVER = "myapp.services.handoff.allow_tenant_destination"
```

Loaded once via `mojo.helpers.modules.load_function()` and cached by dotted
path. **Setting it also turns enforcement on** — a resolver is one of the two
opt-in switches, so `redirect_uri` becomes required and a `False` answer is a
`400` rather than an incident.

**A resolver is security-critical code in your deployment**: compare hosts
exactly, never substring- or prefix-match a hostname, and check the scheme. The
framework fails closed around it — a resolver that raises, or a dotted path that
fails to import, refuses **everything** and is logged. Unlike
`USER_LOGIN_HANDLER`, which swallows errors so a failing analytics hook cannot
lock users out, a broken resolver must never open the gate. (Fail-closed here is
about the *resolver*, not about the feature: with no resolver and no list
configured at all, the deployment is in monitor mode and nothing is refused.)

See [Auth Pages — Cross-Origin Redirect Handoff](../../web_developer/account/auth_pages.md#cross-origin-redirect-handoff)
for the end-to-end client-side flow.

### Gated destinations — deliver a group token instead of a JWT

**Off by default.** A deployment can declare certain destination hosts
**gated**: a handoff code minted for one exchanges into a
[group-scoped token](#group-scoped-tokens) package — a `gt1.` bearer confined
to one group, with **no refresh token** — instead of the platform access +
refresh pair every other destination still receives.

The property, stated honestly: *a gated destination host never receives a
platform JWT through a mojo-hosted auth flow.*

```python
AUTH_HANDOFF_GROUP_TOKEN_MODE  = "enforce"          # off (default) / monitor / enforce
AUTH_HANDOFF_GROUP_TOKEN_HOSTS = {"tenant-a.example.com": "<group uuid>"}
```

All three settings are **file-only** (`settings.get_static`), unlike the
`AUTH_HANDOFF_*` pair above. A DB/Redis-backed `Setting` row is writable
through the generic settings REST plane, so a remotely-writable mode would let
settings-write access silently downgrade every gated destination back to a
platform JWT. Group-scoped `Setting` rows are not consulted either, and neither
is `group.metadata` — a tenant must never hold the switch that decides which
credential its own visitors receive.

#### The decision is taken at the mint and never re-taken

`POST /api/auth/handoff` already has a server-validated `redirect_uri`, so
gating resolves there, stamps the group id into the Redis handoff payload as
`gid`, and `POST /api/auth/exchange` honors it **without consulting the mode or
the destination again**. Re-resolving would let a resolver that breaks inside
the code's TTL turn a gated code back into a JWT. Consequences worth knowing:

- a code minted before a mode flip is honored under the decision taken when it
  was minted — in **both** directions — for up to `AUTH_HANDOFF_CODE_TTL`;
- a code minted before this feature existed carries no `gid` and behaves
  exactly as it always has;
- rolling deploys: an older server ignores `gid`. Deploy the fleet **first**,
  then set the mode.

Gating is never selected at login and never from a client-supplied
`group_uuid` — an attacker simply omits a parameter they control.

#### The matcher is a DENY rule, deliberately not the allowlist's

`AUTH_HANDOFF_ALLOWED_URLS` matches on exact scheme, exact port and a path
prefix, and its wildcard admits exactly one extra label. Every one of those is
correct for an **allow** rule and a bypass in a **deny** rule: an entry of
`gated.example.com` that failed to gate `http://gated.example.com`,
`https://gated.example.com:8443`, `https://gated.example.com.` or
`https://a.b.gated.example.com` would mint a plain JWT whose code lands right
back on the gated origin. So the gating matcher is separate and looser:

- **Entries are hosts.** A full URL is accepted and reduced to its host with a
  warning — scheme, port and path are ignored, because a deny rule must never
  be narrowed by them.
- **Every entry covers the host and all of its subdomains, at any depth.**
  `example.com` and `*.example.com` are the same rule; the `*.` is normalized
  away. A forgotten star is a silent hole, and `app.tenant.example.com` is the
  same tenant as `tenant.example.com`. When several entries match, the most
  specific (most labels) wins. **An operator who gates an apex gates the whole
  zone**, including their own admin app on a sibling subdomain — which then
  receives a scoped token or a refusal instead of a JWT. That fails closed and
  shows up immediately in monitor mode.
- **List IDN destinations in punycode.** A non-ASCII host is reported
  *suspicious*, not guessed at.
- **IP-literal entries are refused in every encoding** — dotted-quad, decimal,
  hex, octal, bracketed IPv6 — and so are single-label names like `localhost`.
  We do not normalize numeric host forms and must not pretend to. A destination
  in one of those shapes is *suspicious* too: no entry can ever be an IP form,
  so treating an IP-literal destination as "matches nothing" would be
  fail-**open** for a deployment whose gated box is an internal address. Use a
  real dotted hostname.
- **Suspicious fails CLOSED** — the exact inversion of the allowlist's "no
  match". A backslash in the URL, a unicode label, a bracketed IPv6 literal or
  a malformed port refuses under `enforce` rather than falling through as
  ungated.
- **A defective entry, or two entries normalizing to one host with different
  groups, refuses every handoff until it is fixed** — plus a `logit.error` and
  a level-2 incident. A dropped entry would be a silent hole; a fatal entry is
  a loud, correctable outage.

#### `enforce` hard-requires handoff-destination enforcement

`AUTH_HANDOFF_GROUP_TOKEN_MODE = "enforce"` without
`AUTH_HANDOFF_ALLOWED_URLS` or `AUTH_HANDOFF_RESOLVER` **refuses every
handoff** — `400`, no code, a `logit.error` and a level-2
`auth:handoff_group_token_misconfigured` incident. A deny map cannot enumerate
"every host that is not mine", so that combination would let a tenant point
`?redirect=` at any host it controls that is merely *absent* from the map and
collect a full JWT pair while the operator believes gating is on. The
alternatives — degrade to monitor, or refuse only gated destinations — both
keep shipping JWTs under that belief. A two-line settings mistake becoming a
loud handoff outage is the correct trade; the rollout below orders the settings
so it cannot happen by following instructions.

**Necessary, but not sufficient.** `is_enforced()` is a configuration-*presence*
check: a resolver that admits every registered tenant domain satisfies it while
leaving hosts unmapped, and an unmapped host is by definition not gated. So an
allowlist-**admitted** destination that matches no gating entry files a
suppressed `auth:handoff_group_token_unmapped` incident under `enforce` — the
feed writes the gating map the way it already writes the allowlist. **Gating is
complete only when the allowlist and the gating map are co-extensive per tenant
zone.**

`monitor` never requires the prerequisite. That is what makes step 2 of the
rollout a safe rehearsal.

#### Fail-closed table

| Condition | `monitor` | `enforce` |
|---|---|---|
| mode `off` | sources not read at all; plain JWT | — |
| allowlist enforcement missing | n/a | **400 on every handoff**, misconfiguration incident |
| no `redirect_uri` | plain JWT | plain JWT (the allowlist already required one) |
| destination matches no entry | plain JWT | plain JWT + `..._unmapped` incident |
| destination host suspicious/unparsable | report, plain JWT | **400**, no code |
| any defective entry in the map | report, plain JWT | **400 on every handoff** until fixed |
| two entries → one host, different groups | report, plain JWT | **400**, no code |
| resolver raises / won't import / junk type | report, plain JWT | **400**, no code |
| unknown group uuid | report, plain JWT | **400**, no code |
| group inactive, or an ancestor is | report, plain JWT | **400**, no code |
| visitor is not a direct active member, or is a superuser | report `would_refuse`, plain JWT | **403**, no code |
| gated destination is also this request's own host | misconfiguration incident, plain JWT | misconfiguration incident, **400** |
| gated + eligible | report, plain JWT | code stamped with `gid` |
| **OAuth** `/begin` or `/complete`, destination gated or suspicious | report, proceed to JWT | **400**, no provider exchange |

Every enforce-mode handoff refusal reuses the existing
`"redirect_uri is not permitted for auth handoff"`, so gated-versus-unlisted-
versus-misconfigured is not an oracle. The non-member case is a distinct `403`
with a comprehensible message, because it is read by a real human at the auth
origin and discloses only what the visitor already knows about their own
membership.

#### Never list an auth-page origin

A gated destination that equals the request's own `HTTP_HOST` is a
configuration error: the auth page short-circuits a same-origin redirect to
direct navigation, so no code is ever minted and the JWT already lives in that
origin's storage. It files a level-2 misconfiguration incident.
`HTTP_HOST` is `ALLOWED_HOSTS`-validated, so it is a trustworthy signal — but a
**white-label `auth_domain` on a different host cannot be detected this way**.
Do not gate a host you serve auth pages from.

#### What a gated exchange does, and deliberately does not do

`group_token_login` is a sibling of `jwt_login`, not a branch inside it — the
JWT pair is never constructed. The mint runs **before any side effect**, so a
refused mint leaves no `last_login` and no login event behind.

| Side effect | Gated exchange |
|---|---|
| geofence enforcement (`scope="auth"`) | yes, first |
| `last_login` | yes |
| `UserLoginEvent` with the device | yes, `source="handoff:grouptoken"` |
| `USER_LOGIN_HANDLER` | **yes** — this IS a login; an auth path that fires no handler is an invisible one |
| `orig_webapp_url` / `last_webapp_url` | **no** |
| `user.org.metadata["access_token_expiry"]` | **ignored** — that knob tunes JWT lifetimes; `GROUP_TOKEN_TTL` governs here |

The declined `webapp_url` write is the point of the feature, not an oversight:
`webapp_base_url` and `HTTP_ORIGIN` on an exchange call come from the gated
destination's **own backend**, so honoring them would let a tenant origin
overwrite protected metadata on the platform account of anyone who visits its
site.

**Superusers can never receive a group token**, so a superuser cannot sign into
a gated destination through this flow at all — they get a `403` at the mint.
Correct and fail-closed, but surprising the first time an operator hits it.

**Realtime/WebSocket is out.** `mojo/apps/realtime/auth.py` calls bearer
handlers with `request=None` and `group_token` fails closed on exactly that. A
gated app has no group-token WebSocket story yet.

#### The OAuth leg refuses instead of delivering

`POST /api/auth/oauth/<provider>/complete` ends at `jwt_login` and hands a full
pair to whichever origin posted to it. It cannot deliver a scoped token
instead: `_find_or_create_user` can create a brand-new account, add a
`GroupMember` from the client-supplied `state.group_uuid`, fire
`USER_REGISTERED_HANDLER` and persist provider tokens **before** a membership
pre-flight could fail — a `403` after four side effects — and auto-joining the
visitor to avoid that would be a membership grant driven by a URL.

So under `enforce`, a gated site that runs its **own** OAuth callback page
loses OAuth login and must move that flow to the hosted auth pages, where OAuth
already works and already routes through the gated handoff. This is the one
place where turning gating on can break a working consumer integration —
`monitor` names every such destination in the feed
(`auth:handoff_group_token_oauth_refused`) before enforcement binds.

Checked at `/begin` (both branches — the `else` one derives the landing URL
from the **unvalidated** `Origin` header) and, authoritatively, at `/complete`
before any provider call. Not checked at `/callback`, which only 302s the
browser with a provider code that is useless without your client secret.
**Same-host is exempt**: the hosted pages' OAuth buttons set the landing URL to
the page's own URL and are served white-label on the tenant's own host, so
without the carve-out a zone-wide entry would refuse a tenant's own Google
button.

> **The OAuth half is a deny-list, and its allow-list backstop is only as
> good as your entries.** `_validate_redirect_uri` now matches a parsed URL —
> so with `ALLOWED_REDIRECT_URLS = ["https://gated.example.com"]` an
> attacker-registered `gated.example.com.evil.tld` is refused by the allowlist
> before gating is consulted. (It used to be a bare `startswith`, which
> admitted it; gating then correctly reported *that* host as not gated, so the
> headline property survived literally while the tenant-facing intent did
> not.) The residual risk is ordinary allowlist hygiene: every host you list is
> a place tokens can land, so keep that list minimal and treat it as
> security-critical. Gating entries cover a host **and all its subdomains**;
> allowlist entries do not, which is the asymmetry that keeps a deny rule from
> being narrowed by a missing star.

#### Rolling gating out

1. **Deploy the whole fleet first.** An older server ignores `gid`.
2. **Set `AUTH_HANDOFF_GROUP_TOKEN_MODE = "monitor"`** with your host map.
   Nothing binds. Watch `auth:handoff_group_token_preview` — it names every
   destination that would be gated and, per visitor outcome, whether they
   would have been **refused** (not a member, or a superuser). Watch
   `auth:handoff_group_token_oauth_refused` for gated sites running their own
   OAuth callback; those must move to the hosted pages first.
3. **Turn destination enforcement on** (`AUTH_HANDOFF_ALLOWED_URLS` or
   `AUTH_HANDOFF_RESOLVER`) — see the rollout above. `enforce` refuses every
   handoff without it, so this step comes **before** the next one.
4. **Set the mode to `enforce`.** Watch `auth:handoff_group_token_unmapped`
   and close the gap between the allowlist and the gating map.

**Gating is forward-only.** Turning it on does **not** evict platform JWTs that
are already sitting in a newly-gated origin's `localStorage`, and a destination
app running its own token manager will keep trading its stale refresh token for
fresh pairs until that token dies. The levers that actually evict are
`POST /api/auth/sessions/revoke` (rotates `auth_key`, killing every JWT and
group token that user holds) and letting `JWT_REFRESH_TOKEN_EXPIRY` run out.
Plan for one of those when you flip a live destination.

#### What gating is, and is not

It is a **least-privilege** control for a cooperating tenant, and a
**containment** control for a compromised one: XSS on the tenant's page reaches
a group token confined to that tenant, not a platform JWT good for every group
the visitor belongs to.

It is **not** a defense against a malicious tenant. A tenant that hosts its own
login form collects the password itself, which is strictly worse and is already
out of scope (same reasoning as the handoff enforcement section above). An
oversold property is a security liability; this one is worth having and narrow.

## API Keys

Long-lived JWTs restricted by IP allowlist.

**Generate own key:** `POST /api/auth/generate_api_key`
- Required: `allowed_ips` (list), `expire_days` (max 360)

**Admin generate for another user:** `POST /api/auth/generate_api_key`
- Required: `allowed_ips`, `expire_days`, `uid`
- Requires: `manage_users` permission

## Group-Scoped Tokens

A third bearer scheme alongside `bearer` (JWT) and `apikey`:

```
Authorization: grouptoken gt1.<b64payload>.<sig>
```

It authenticates as a **real user** but authorization resolves **only** through
that user's membership in one signed group. No other group, no descendants, no
platform-global grants. Implementation:
`mojo/apps/account/services/group_token.py`.

**What it is for.** A JWT grants everything its account can reach in every group
it belongs to. Handing one to JavaScript on a page whose content a tenant
controls means the tenant receives a platform credential for every visitor who
signs in. A group token is the credential a gated-site flow can hand to a tenant
origin instead.

### Format and signing

`gt1.<b64payload>.<sig>`, where the payload is
`{"u": user_id, "g": group_id, "e": epoch, "iat": unix_ts}` and the signature is
`crypto.sign("gt1." + b64payload, user.get_auth_key())` — full 64-char
HMAC-SHA256 hex, verified with `hmac.compare_digest`.

The version tag is signed **inside** the HMAC, not merely prefixed to the wire
format. `mojo/apps/account/utils/tokens.py` signs a bare b64 payload with the
same per-user key; the domain tag keeps the two families apart.

The secret is `user.get_auth_key()`, which buys two properties for free:
rotating `auth_key` (what `POST /api/auth/sessions/revoke` already does) kills
that user's group tokens, and a token forged against one user's key cannot be
replayed as another.

**Deliberately not a JWT.** A JWT signed with the same key would be accepted
verbatim under `Authorization: bearer` and by `on_refresh_token` as a
`refresh_token` — trading the scoped token for a full unscoped pair.
`jwt.decode` cannot parse the `gt1.` format at all, so "a group token cannot be
upgraded into a JWT" holds by construction.

### Minting

Service-level only. No REST endpoint mints one; no login or handoff behavior
changes.

```python
from mojo.apps.account.services import group_token

token = group_token.mint(user, group)          # raises PermissionDeniedException on refusal
token = group_token.mint(user, group, issued_at=1234567890)   # tests / deterministic clock
```

`mint` refuses: a superuser, an inactive user, an effectively-inactive group (it
or any ancestor), and anything but **direct** active membership
(`check_parents=False` — delegation must not exceed the delegator, so a member
of the parent gets no token for a child group).

### Revocation

| Lever | Scope | How |
|---|---|---|
| Epoch bump | every token for one group | `group.bump_group_token_epoch()`, or `POST /api/group/<pk>` with `{"revoke_group_tokens": true}` |
| `auth_key` rotation | every token for one user | `POST /api/user/me {"revoke_sessions": true}`, or set `user.auth_key` |
| Membership removal | that user in that group | delete the `GroupMember` row |
| Group (or ancestor) deactivation | every token for the subtree | `group.is_active = False` |
| User deactivation | every token for that user | `user.is_active = False` |

The epoch lives in `group.metadata["protected"]["group_token_epoch"]` — no
migration. The `protected` root key is REST-guarded by
`MojoModel._can_edit_protected_json` (superuser or
`RestMeta.PROTECTED_JSON_PERMS`, which on `Group` is `admin_compliance` /
`admin_verify`), so a tenant group admin holding `manage_group` cannot rewind
the epoch to un-revoke tokens. Absent reads as `0`; a payload whose epoch does
not match fails closed.

Read-modify-write on the epoch is **not** atomic: two simultaneous bumps can
land `N+1` instead of `N+2`. Both callers intend "invalidate everything issued
before now", and anything that survives the race expires within
`GROUP_TOKEN_TTL`. Documented and accepted.

### What a group token can and cannot do

Confined by `is_group_allowed` — **strict equality with the signed group, no
descendants** (unlike an ApiKey, which covers its subtree).

Refused:

- any group other than the signed one — via `?group=`/`group_uuid=`, a
  `group-<id>` metrics account, a `room_id`, `group/<pk>/member`, or a detail
  row whose owning tenant differs;
- **`Group` records entirely** — detail *and* list, own group included. The
  token's own `has_permission` is always `False`, so `Group` is opaque; tenant
  UI reads branding from `public_auth_config`, not the `Group` row;
- every groupless model (`group_token.groupless_denied`), including under
  `RestMeta.ALLOW_API_KEY_GLOBAL` — that flag is an ApiKey-specific opt-in;
- `@md.requires_global_perms`, **including with `allow_api_keys=True`**;
- every `@md.denies_key_backed_session` endpoint — passkeys, TOTP, user API
  keys, email/phone change, and `POST /api/auth/handoff`;
- every write to the platform account (`POST /api/user/me`, password/email
  change, `revoke_sessions` and every other POST_SAVE_ACTION, `DELETE`);
- WebSocket auth (`mojo/apps/realtime/auth.py` calls handlers with
  `request=None`; the handler fails closed on the first check).

Allowed:

- `GET /api/user/me` — the documented client bootstrap stays read-only self;
- group-scoped rows in the signed group, resolved through the user's
  `GroupMember` grants **in that group** (`check_user=False`, so the member's
  untenanted global dict is never consulted).

Every failure returns the same `401 {"error": "Invalid group token"}` — no
account or group-state oracle. The one exception is expiry, which reports
`"Group token expired"` so a client can re-mint instead of prompting a full
re-auth.

### Registration — opt-in per deployment

Both entries are required, and **both settings replace their defaults
wholesale** (`middleware/auth.py` reads them with `settings.get_static` and a
default dict — there is no merge). Always write the complete NAME_MAP:

```python
AUTH_BEARER_HANDLERS = {
    "grouptoken": "mojo.apps.account.services.group_token.validate_token",
}
AUTH_BEARER_NAME_MAP = {
    "bearer": "user",
    "apikey": "user",
    "grouptoken": "user",
}
```

A deployment that declares only the `grouptoken` entry in `AUTH_BEARER_NAME_MAP`
silently un-maps `bearer` and `apikey`: `request.user` never populates and every
request degrades to anonymous 403s with no diagnostic. A deployment that
registers neither entry rejects `Authorization: grouptoken …` with
`401 Invalid token type`, exactly as it does today.

### Writing group-token-safe endpoints

Model security (`@md.uses_model_security(Model)` + `RestMeta`) and
`@md.requires_perms` / `@md.requires_group_perms` are confined already — nothing
to do.

An endpoint that authorizes against a **caller-named group** instead (a
`group-<id>` account param, a `room_id`, a pk in the path) never gets model
security's instance re-bind, so it must apply the bound itself:

```python
from mojo.helpers.request import identity_allows_group, is_override_user_session

if not identity_allows_group(request, group):        # True for ordinary users
    raise merrors.PermissionDeniedException()
```

An endpoint that short-circuits on the caller's **global** permission dict must
skip that read for a session that assumes a member:

```python
check_user = not is_override_user_session(request)
if not group.user_has_permission(request.user, perms, check_user):
    raise merrors.PermissionDeniedException()
```

`restricted_identity(request)` returns the ApiKey or GroupScopedToken behind the
request, or `None`. Both duck-type `is_group_allowed`, `has_permission`,
`get_groups` and `get_groups_with_permission`, so one code path covers both.

**Known gap.** These helpers cover the framework's own gates. A third-party app
that reads `request.user.has_permission(...)` directly, with no group in the
question, remains the deployment's responsibility — under a group token that
read returns the visitor's untenanted platform grants. Use the two helpers, or
route the endpoint through model security.

## Current User

**Endpoint:** `GET /api/user/me`

Returns the authenticated user's own record using their `pk`.

## Middleware Auth Flow

1. `Authorization: Bearer <token>` header parsed
2. Bearer type looked up in handler registry
3. Default handler: `User.validate_jwt(token)` → returns `(user, error)`. A
   disabled user (`is_active=False`) is rejected here with the generic
   `"Invalid token user"` error — same message as a missing user, so a stale
   token never discloses account state. This applies to `user_api_key`
   tokens too. See the [kill switch](disable_lifecycle.md#kill-switch-disable-is-instant-dm-042)
   (DM-042).
4. `request.user` set to the resolved user (or anonymous)
5. `request.group` set if `group` param present, the group is **effectively** active (it and every ancestor — DM-048), and user is a member (an inactive group's id — including an active child under a deactivated ancestor — resolves to no group, same as a nonexistent one)

**Malformed headers don't error.** If the `Authorization` value isn't exactly `<scheme>
<token>`, the middleware treats the request as unauthenticated and continues — it does not
500. A bare scheme-less single token is exposed on `request.auth_token` (prefix `"raw"`) so a
public endpoint can read/validate it; an empty or 3+-part value is ignored. `request.bearer`
stays unset in all these cases, so anything requiring auth still rejects.

## Custom Bearer Handlers

Register additional token types via settings:

```python
# settings.py
MOJO_BEARER_HANDLERS = {
    "ApiKey": "myapp.auth.validate_api_key",
}
```

The handler receives the raw token string and must return `(user_or_None, error_or_None)`.

## User CRUD Endpoint

```python
@md.URL('user')
@md.URL('user/<int:pk>')
def on_user(request, pk=None):
    return User.on_rest_request(request, pk)
```

- **List:** requires `view_users` or `manage_users`
- **Create:** handled via invite flow or direct admin creation
- **Get/Update own record:** allowed via `owner` permission (`OWNER_FIELD = "self"`)
- **Update others:** requires `manage_users`

## Registration / Onboarding Patterns

### Built-in Registration

The framework provides a built-in registration endpoint gated by the `ALLOW_USER_REGISTRATION` setting (default `False`).

**Enable it:**

```python
# settings.py
ALLOW_USER_REGISTRATION = True
```

**Endpoint:** `POST /api/auth/register`

Fields and which identity channel is required depend on the server's `AUTH_REGISTER_FIELDS` setting. Default config requires `email` + `password`. Phone-as-identity projects configure `phone` (with optional `verify: "sms"` pre-verification) as the identity field instead.

| Param | Required | Description |
|---|---|---|
| `email` | Conditional | Required when `email` is the configured identity field |
| `phone` | Conditional | Required when `phone` is the configured identity field |
| `password` | Yes | Password (strength validated) |
| `first_name` | Conditional | Required when configured as such |
| `last_name` | Conditional | Required when configured as such |
| `dob` | Conditional | ISO `yyyy-mm-dd`. Required when configured; age-gated by `AUTH_MIN_AGE_YEARS` |
| `verified_phone_token` | Conditional | Required when the schema marks `phone` with `verify: "sms"` |

**Behavior depends on `REQUIRE_VERIFIED_EMAIL`:**

- **`REQUIRE_VERIFIED_EMAIL = True`** — Account is created, response includes `requires_verification: true`. No JWT is issued. Email users must verify before logging in.
- **`REQUIRE_VERIFIED_EMAIL = False`** (default) — User is logged in immediately with a JWT. A verification email is sent as a nudge when the user has an email on file.

The registration page is served by the bouncer at `/auth/register` and uses `MojoAuth.register()` on the frontend.

**Phone-identity existing-account behavior:**

When `identity_field` is `phone` and the schema marks the phone field with
`verify: "sms"`, a submitted phone that already belongs to an account is treated
as a login rather than a duplicate error. The `verified_phone_token` proves
phone ownership (the same proof SMS login uses), so the existing account is
returned. Profile fields in the request body are ignored. Without
`verify: "sms"` on the phone field, an existing phone is still a hard
duplicate error (ownership unproven).

If `group_uuid` is supplied and the existing account is not yet a member of
that group, a `GroupMember` is created and `USER_REGISTERED_HANDLER` fires for
that group. If the account is already a member, `USER_REGISTERED_HANDLER` does
not fire. Email-identity registration is unchanged — an existing email is always
a duplicate error.

**Token restore on failure (`phone_register.restore()`):**

The `verified_phone_token` is consumed before `USER_REGISTERED_HANDLER` fires. If
the handler raises, `on_register` calls `phone_register.restore(verified_token, phone)`
to re-mint the token so the caller can retry the same `/api/auth/register` POST
without re-verifying the phone. The restore is scoped to the handler-firing step only —
post-handler failures (JWT issuance, email send) keep the token consumed, preventing
double-firing of the handler on retry. This applies to both the existing-account login
path and new-user creation.

**Protections:**

- Rate limited: 5 requests per IP per 5 minutes
- Bouncer token required (bot detection)
- Password strength validated via `check_password_strength()`
- Content guard validates username and name fields on save
- Duplicate email returns a clear error
- Duplicate phone without `verify: "sms"` returns a clear error

### Pattern A — Invite-only

For projects that need tighter control, create accounts server-side and send invite links. The user sets their password on first visit.

```python
# In your project's admin, management command, or REST endpoint:
from mojo.apps.account.models import User

user = User(email="alice@example.com")
user.username = user.generate_username_from_email()
user.set_unusable_password()
user.save()
user.send_invite()  # builds token URL, sends invite email
```

`send_invite()` accepts an optional `request` kwarg for multi-tenant URL resolution (see [Token URLs](#token-urls) below).

User clicks the link → `POST /api/auth/invite/accept` with the token → JWT issued, email verified.

### Pattern B — Custom registration

For projects that need registration logic beyond the built-in endpoint (domain restriction, approval queues, CAPTCHA, etc.), add your own endpoint:

```python
# In your project's REST layer:
@md.POST("auth/register")
@md.public_endpoint()
@md.strict_rate_limit("register", ip_limit=5, ip_window=300)
@md.requires_params("email", "password")
def on_register(request):
    from mojo.apps.account.models import User
    from mojo import errors as merrors

    email = request.DATA.email.lower().strip()
    if User.objects.filter(email=email).exists():
        raise merrors.ValueException("Email already registered")
    user = User(email=email)
    user.username = user.generate_username_from_email()
    user.set_new_password(request.DATA.password)
    user.save()
    # Trigger the framework's verify-send flow via internal call or redirect
    # user.send_template_email("email_verify_link", ...) or POST to /api/auth/email/verify/send
    return JsonResponse({"status": True, "message": "Check your email to verify your account."})
```

With `REQUIRE_VERIFIED_EMAIL = True`, the user cannot log in until they click the verification link — no additional gate logic needed.

### Registration Extension Hooks

Three settings let consumer apps plug custom logic into the register and login flows without forking the built-in endpoints.

#### `PRE_REGISTER_VALIDATOR`

Dotted-path callable invoked before the user record is created. Raise `ValueException` to reject the request with a 400. The plaintext password is intentionally **not** passed.

```python
# settings.py
PRE_REGISTER_VALIDATOR = "myapp.validators.check_registration"
```

```python
# myapp/validators.py
from mojo import errors as merrors

def check_registration(*, email, group, request, extra):
    if not email.endswith("@acme.com"):
        raise merrors.ValueException("Only @acme.com addresses may register.")
```

#### `USER_REGISTERED_HANDLER`

Fires inside the register `transaction.atomic()` block, and also from the OAuth path when a new user is created. Raising an exception rolls back the entire registration transaction.

```python
# settings.py
USER_REGISTERED_HANDLER = "myapp.handlers.on_user_registered"
```

```python
# myapp/handlers.py
def on_user_registered(*, user, request, group, source, extra):
    # source ∈ {"password", "oauth", "sms"}
    # "sms" fires for: new phone-identity accounts AND existing accounts
    #   joining a new group via the phone register flow.
    # Avoid slow I/O here — this is inside a DB transaction
    user.metadata["registration_source"] = source
    user.save(update_fields=["metadata"])
```

#### `USER_LOGIN_HANDLER`

Fires from every successful `jwt_login()` call, across all login paths. Errors are caught, logged, and swallowed — they never affect the login response.

```python
# settings.py
USER_LOGIN_HANDLER = "myapp.handlers.on_user_login"
```

```python
# myapp/handlers.py
def on_user_login(*, user, request, source, is_new_user):
    pass
```

`source` values: `"password"`, `"magic"`, `"oauth"`, `"email_verify"`, `"invite"`, `"password_reset"`, `"email_change"`, `"sessions_revoke"`, `"totp"`, `"totp_mfa"`, `"totp_recovery"`, `"passkey"`, `"sms"`, `"sms_mfa"`, `"handoff"`, `"handoff:grouptoken"`.

**Error contract asymmetry:**

| Hook | Raises | Effect |
|---|---|---|
| `PRE_REGISTER_VALIDATOR` | `ValueException` | Rejects with 400, propagates |
| `USER_REGISTERED_HANDLER` | Any exception | Rolls back registration transaction |
| `USER_LOGIN_HANDLER` | Any exception | Caught + logged; login succeeds anyway |

**Guidance:** Enqueue background jobs from `USER_LOGIN_HANDLER` rather than performing slow I/O inline. For `USER_REGISTERED_HANDLER`, keep work minimal — it is inside a database transaction.

### Registration Extra Fields

The built-in `POST /api/auth/register` endpoint accepts an optional `group_uuid` body param to associate the new user with a group at registration time. It also forwards an allowlisted set of arbitrary key/value pairs to the `USER_REGISTERED_HANDLER` via the `extra` argument.

```python
# settings.py — keys not in this list are silently dropped
REGISTRATION_EXTRA_FIELDS = ["referral_code", "plan"]

# Require group_uuid to be present and valid
REQUIRE_GROUP_ON_REGISTRATION = True   # default False
```

### Framework primitives available

| Need | How |
|---|---|
| Enable built-in registration | `ALLOW_USER_REGISTRATION = True` |
| Create a user | `User(...).save()` + `user.save_password()` |
| Send invite link | `user.send_invite(request=request)` |
| Accept invite + set password | `POST /api/auth/invite/accept` |
| Send email verify link | `POST /api/auth/email/verify/send` |
| Confirm email verify | `POST /api/auth/email/verify` |
| Require verified email before login | `REQUIRE_VERIFIED_EMAIL = True` |
| OAuth auto-registration | Built into `auth/oauth/<provider>/complete` — gate with `OAUTH_ALLOW_REGISTRATION` |
| Block OAuth new-user creation | `OAUTH_ALLOW_REGISTRATION = False` in settings |

---

## Token URLs

Transactional token links (invite, magic login, password reset, email verify) are built as:

```
{base_url}{auth_path}?flow={flow}&token={token}
```

The frontend dispatches on `flow=` so only one auth path needs to be configured per tenant.

**Resolution order** (first non-empty wins):

1. `request.DATA["webapp_base_url"]` — per-request override (useful for multi-tenant admin portals)
2. `group.metadata["webapp_base_url"]` — tenant config, traverses parent chain
3. `user.org.metadata["webapp_base_url"]` — user's primary org
4. `WEBAPP_BASE_URL` setting
5. `user.metadata["protected"]["orig_webapp_url"]` — URL recorded at the user's first login
6. `HTTP_ORIGIN` header
7. `BASE_URL` setting (legacy fallback)

Auth path follows the same precedence with `group.metadata["webapp_auth_path"]` and `WEBAPP_AUTH_PATH` (default `"/auth"`).

Configure per tenant without a deploy:

```python
group.metadata["webapp_base_url"] = "https://app.acme.com"
group.metadata["webapp_auth_path"] = "/login"  # optional, default /auth
group.save()
```

## Failed Login Protection

The login endpoint applies a layered, bypass-resistant throttle stack. Each tier is independent — tripping one does not bypass any other.

| Tier | Scope | Limit | Window | Notes |
|---|---|---|---|---|
| 1 | IP | 100 req | 60 s | First line; always active |
| 2 | muid (server-set cookie) | 10 req | 300 s | Server-controlled; cannot be rotated by the client |
| 3 | per-account (user.id) | 10 req | 900 s | Applied after username resolves; `LOGIN_USERNAME_LIMIT` / `LOGIN_USERNAME_WINDOW` |
| 4 | `invalid_password` ruleset | 5 events | 15 min | Level-5 events → IP block fleet-wide for 30 min |
| 5 | MFA verify endpoints (IP) | 10 req | 60 s | TOTP and passkey verify; `MFA_VERIFY_IP_LIMIT` / `MFA_VERIFY_IP_WINDOW` |

**muid** is an `HttpOnly` cookie set by mojo middleware; unlike `duid` (client-supplied), the client cannot manufacture or cycle it. `duid` is still checked as an additive tier.

**Per-account counter** is cleared automatically on a successful password match, so one legitimately mistyped password does not permanently penalise an account. On block the endpoint returns a standard 429 with `Retry-After`.

**`invalid_password` ruleset** — the `ensure_auth_rules()` call during startup registers a bundled incident rule: 5 `invalid_password` events at level >= 5 from the same IP within 15 minutes triggers a fleet-wide IP block for 30 minutes. The rule name is `"Auth - Password Brute Force"`. This rule is idempotent — calling `ensure_auth_rules()` more than once is safe.

### Settings

| Setting | Default | Purpose |
|---|---|---|
| `LOGIN_USERNAME_LIMIT` | `10` | Max failed attempts per account per window |
| `LOGIN_USERNAME_WINDOW` | `900` | Window in seconds for per-account counter |
| `MFA_VERIFY_IP_LIMIT` | `10` | Max TOTP/passkey verify attempts per IP per window |
| `MFA_VERIFY_IP_WINDOW` | `60` | Window in seconds for MFA verify IP counter |

### Admin — releasing a stuck account

When a user is locked out by tier 3, an admin with `manage_users` can clear the counter. This endpoint (and `GET /api/auth/manage/throttle`) is gated with `@md.requires_global_perms` — the grant must be global on the User, not a group/member-scoped permission:

```
POST /api/auth/manage/clear_rate_limit
{
  "username": "alice@example.com"
}
```

Or by user ID:

```
POST /api/auth/manage/clear_rate_limit
{
  "user_id": 42
}
```

When `username` or `user_id` is provided without an explicit `key`, the key defaults to `"login"`. To clear a specific bucket:

```
POST /api/auth/manage/clear_rate_limit
{
  "username": "alice@example.com",
  "key": "login"
}
```

The endpoint also accepts `ip`, `duid`, and `muid` to clear other tiers independently.

## Incident Reporting

Failed login attempts, unknown usernames, and invalid password resets are automatically reported to the incident system with appropriate severity levels. `invalid_password` events are emitted at level 5 once the username resolves (level 1 from `set_new_password`).

