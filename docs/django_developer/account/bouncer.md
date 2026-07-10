# Bouncer — Django Developer Reference

Server-gated bot detection for django-mojo. Bots never receive the login form,
field names, or auth API endpoint URLs. The challenge is server-rendered; all
signals are scored server-side before any auth surface is exposed.

See also: [Auth Pages](auth_pages.md) for the login/registration page setup,
branding, OAuth configuration, and nginx setup.

---

## How It Works

```
Request → GET BOUNCER_LOGIN_PATH (default: /auth)
              ↓
     1. Redis signature cache (IP, subnet, UA, fingerprint)
        matched → serve decoy immediately
              ↓
     2. Pass cookie present and valid
        valid  → serve full login page
              ↓
     3. Server-side pre-screen (headers + GeoIP scoring)
        clearly bot → serve decoy
              ↓
     4. Serve challenge page (randomized per render)
              ↓
     mojo-bouncer.js collects signals, user clicks target
              ↓
     POST /api/account/bouncer/assess
        allow/monitor → signed token + HttpOnly pass cookie
        block         → no token, BotLearner job queued
              ↓
     JS stores token, redirects to login URL
     (redirect, next, returnTo, and back params forwarded through)
              ↓
     GET BOUNCER_LOGIN_PATH again — pass cookie present
              ↓
     Serve full login page (mojo-auth.js webapp)
              ↓
     mojo-auth.js attaches bouncer_token to every auth API call
              ↓
     @md.requires_bouncer_token('login') on login endpoint validates token
```

---

## Opt-In Setup

All bouncer features are opt-in via settings. Existing projects are unaffected.

```python
# settings.py

# Path for the real login page (avoids common bot-scan paths)
BOUNCER_LOGIN_PATH = 'auth'

# Path for the registration page
BOUNCER_REGISTER_PATH = 'register'

# Decoy honeypot paths (hardcoded): /login, /signin, /signup

# Where to redirect after successful login
BOUNCER_SUCCESS_REDIRECT = '/dashboard/'

# Branding overrides (optional — defaults render without logo/accent)
BOUNCER_LOGO_URL = 'https://yourproject.com/logo.svg'
BOUNCER_ACCENT_COLOR = '#3b82f6'

# Token TTL in seconds (default 900 = 15 min)
BOUNCER_TOKEN_TTL = 900

# Pass cookie TTL in seconds (default 86400 = 24h)
BOUNCER_PASS_COOKIE_TTL = 86400

# Token enforcement: False = log-only (safe for gradual rollout)
# True = reject with 403 if token missing or invalid
BOUNCER_REQUIRE_TOKEN = False

# Adaptive learning settings
BOUNCER_LEARN_ENABLED = True
BOUNCER_LEARN_MIN_SCORE = 80        # minimum score to learn from
BOUNCER_LEARN_SUBNET_THRESHOLD = 5  # blocks per /24 per hour to flag subnet
BOUNCER_LEARN_UA_THRESHOLD = 5      # blocks per UA per hour to flag UA
BOUNCER_LEARN_FP_THRESHOLD = 3      # blocks per fingerprint to flag it
BOUNCER_LEARN_CAMPAIGN_THRESHOLD = 5  # cross-IP signal_set matches to detect campaign
BOUNCER_LEARN_SUBNET_TTL = 86400    # 24h auto-block TTL for subnets
BOUNCER_LEARN_UA_TTL = 604800       # 7d auto-block TTL for UAs
BOUNCER_LEARN_SIGNAL_SET_TTL = 2592000  # 30d campaign signature TTL

# Score weights per signal (any signal missing from this dict contributes 0)
BOUNCER_SCORE_WEIGHTS = {
    'webdriver_flag': 25,
    'playwright_artifacts': 30,
    'puppeteer_artifacts': 30,
    'outer_size_zero': 20,
    'headless_ua': 20,
    'languages_empty': 15,
    'screen_zero': 20,
    'chrome_runtime_missing': 20,
    'document_focus_never': 15,
    'no_interaction': 20,
    'first_interaction_too_fast': 15,
    'rapid_click': 20,
    'mouse_straightness': 15,
    'geo_vpn': 10,
    'geo_tor': 35,
    'geo_proxy': 15,
    'geo_datacenter': 15,
    'geo_known_attacker': 40,
    'geo_known_abuser': 30,
    'header_missing_accept': 10,
    'header_missing_accept_language': 10,
    'header_headless_ua': 20,
    'signal_contradiction': 20,
    'history_blocked_device': 60,
    'history_high_risk_device': 30,
    'history_high_event_count': 10,
    'gate_honeypot_filled': 50,
    'gate_click_too_fast': 20,
    'gate_no_interaction_desktop': 25,
    'gate_excessive_attempts': 15,
    'form_instant_fill': 30,
    'form_no_focus': 20,
}

# Decision thresholds
BOUNCER_THRESHOLDS = {
    'block': 60,
    'monitor': 40,
}

# Per-page-type threshold overrides
BOUNCER_THRESHOLDS_OVERRIDES = {
    'login': {'block': 65, 'challenge': 35},
    'registration': {'block': 55, 'challenge': 25},
    'password_reset': {'block': 50, 'challenge': 20},
}
```

---

## Models

### `BouncerDevice`

Pre-auth device reputation. Separate from `UserDevice` (which requires a logged-in user).

```python
from mojo.apps.account.models import BouncerDevice

device = BouncerDevice.objects.get(duid='...')
device.risk_tier   # unknown | low | medium | high | blocked
device.event_count
device.block_count
device.fingerprint_id
device.linked_duids  # list of duids sharing the same browser fingerprint
```

Risk tiers:
- `unknown` — first seen
- `low` — passed challenge
- `medium` — triggered 1–2 signals
- `high` — triggered 3+ signals or failed challenge repeatedly
- `blocked` — confirmed bot; pre-screen rejects immediately

### `BouncerSignal`

Audit log. One row per assess/submit/event API call. Read-only via REST.

### `BotSignature`

Adaptive learning registry. Auto-populated by `BotLearner` after confirmed
high-confidence blocks. Fully manageable via the operator portal.

```python
from mojo.apps.account.models import BotSignature

# Manual block by subnet
BotSignature.objects.create(
    sig_type='subnet_24',
    value='185.220.101.0/24',
    source='manual',
    confidence=95,
    notes='Known Tor exit node range',
)
# Call refresh_sig_cache() after manual changes to update Redis immediately
from mojo.apps.account.services.bouncer.learner import refresh_sig_cache
refresh_sig_cache()
```

Signature types: `ip`, `subnet_24`, `subnet_16`, `user_agent`, `fingerprint`, `signal_set`

---

## Decorators

### `@md.requires_bouncer_token(page_type)`

Validates the `bouncer_token` field on API requests.

```python
@md.POST('login')
@md.requires_bouncer_token('login')
def on_login(request):
    ...
```

Controlled by `BOUNCER_REQUIRE_TOKEN`:
- `False` (default): missing/invalid tokens are logged; request proceeds
- `True`: missing/invalid tokens return 403

Per-group opt-in: `group.metadata["require_bouncer_token"] = True`

---

## Services

### `TokenManager`

```python
from mojo.apps.account.services.bouncer.token_manager import TokenManager

token = TokenManager.issue(duid, fingerprint_id, ip, risk_score, page_type)
payload = TokenManager.validate(token, request_ip, request_duid)
payload = TokenManager.validate_and_consume(token, request_ip, request_duid)
```

### `RiskScorer`

```python
from mojo.apps.account.services.bouncer.scoring import RiskScorer, ScoringContext

context = ScoringContext(
    client_signals=signals_dict,
    server_signals=server_signals_dict,
    device_session=bouncer_device_or_none,
    page_type='login',
    request=request,
)
result = RiskScorer.score(context)
# result.score           — 0-100
# result.decision        — allow | monitor | block
# result.triggered_signals — list of signal names that fired
```

### Adding a custom analyzer

```python
from mojo.apps.account.services.bouncer.scoring import BaseSignalAnalyzer, register_analyzer

@register_analyzer
class MyAnalyzer(BaseSignalAnalyzer):
    name = 'my_custom'

    @classmethod
    def analyze(cls, context):
        score = 0
        triggered = []
        if context.client_signals.get('my_signal'):
            score += 30
            triggered.append('my_signal')
        return score, triggered
```

Add `'my_signal': 30` to `BOUNCER_SCORE_WEIGHTS` in settings.

---

## Adaptive Bot Signature Learning

After every confirmed block with `risk_score >= BOUNCER_LEARN_MIN_SCORE`, the
`learn_from_block` background job:

1. Marks the `BouncerDevice` as `risk_tier='blocked'`
2. Increments subnet /24 counter in Redis; creates `BotSignature` when threshold hit
3. Increments UA counter; creates `BotSignature` for repeated identical UAs
4. Increments fingerprint counter; creates `BotSignature` for repeat fingerprints
5. Hashes triggered signal set; detects coordinated campaigns across IPs
6. Rebuilds the Redis signature cache used by pre-screen

The Redis cache is also rebuilt by the scheduled `refresh_bouncer_sig_cache` job.

---

## Per-Group Branding (White-Label Auth)

The bouncer supports white-label auth pages per group. When a request arrives
on a custom auth domain — or includes a `?group_uuid=<uuid>` query param —
the bouncer resolves the group and applies its scoped `AUTH_*` settings.

### Group detection order

1. **Hostname** — `Group.resolve_by_auth_domain(hostname)` looks up the active
   group whose `auth_domain` matches the request host. Result is Redis-cached
   (24h for hits, 1h for misses).
2. **`?group_uuid=<uuid>` query param** — fallback for platforms that share a
   domain. The group UUID is preserved through the challenge redirect and the
   OAuth round-trip.

The bouncer reads `?group_uuid=` (not `?group=`) because the framework's URL
dispatcher (`mojo/decorators/http.py`) reserves `?group=` for integer-ID lookup
and returns `400 Invalid group ID` for any non-integer value before this view
runs. The dispatcher's UUID slot is `?group_uuid=`, which is what the bouncer
reads and emits.

### Query params forwarded through the challenge

`_serve_challenge()` preserves these params when building the post-challenge
login redirect URL: `group_uuid`, `redirect` (and aliases `next`, `returnTo`),
and `back`. Any param missing from the original request is omitted from the
forwarded query string.

### Configuring a white-label group

```python
from mojo.helpers import settings
from mojo.apps.account.models import Group

group = Group.objects.get(uuid='...')

# Assign the custom auth hostname
group.auth_domain = 'auth.clientbrand.com'
group.save()

# Set the group's auth config (branding + offered methods)
group.metadata = group.metadata or {}
group.metadata["auth_config"] = {
    "theme": {
        "app_title": "Client Brand",
        "logo_url": "https://cdn.client.com/logo.svg",
        "success_redirect": "/client-dashboard/",
    },
    "login": {"methods": ["password", "google"]},
}
group.save(update_fields=["metadata"])
```

The auth config resolves per group: code defaults ← the global `AUTH_CONFIG`
setting ← `metadata["auth_config"]` deep-merged down the parent chain
(root → leaf). The flat `AUTH_*` settings are retired — see the migration
table in [Auth Config](auth_config.md).

### Challenge page branding

The bouncer challenge page uses the configured default branding. To override it
for a specific group (opt-in only):

```python
settings.set('BOUNCER_CHALLENGE_LOGO_URL', 'https://cdn.client.com/logo.svg', group=group)
settings.set('BOUNCER_CHALLENGE_BRAND', 'CLIENT BRAND', group=group)
```

`BOUNCER_CHALLENGE_LOGO_URL` and `BOUNCER_CHALLENGE_BRAND` only take effect
when a group is resolved. Requests with no group always use the default branding.

### OAuth round-trip

`group_uuid` is embedded in the OAuth state so branding survives the
provider redirect. The callback reconstructs `?group_uuid=<uuid>` and appends
it to the frontend redirect URI before handing back to the auth page (using
`group_uuid` rather than `group` so the framework dispatcher accepts the
next request through the rest of the flow).

### Nginx setup for custom auth domains

Each white-label domain needs its own nginx server block pointing at the same
Django backend. Pass the real hostname so the bouncer can resolve the group:

```nginx
server {
    listen 443 ssl;
    server_name auth.clientbrand.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;          # must be the real hostname
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Do not rewrite `Host` to your main domain — the bouncer reads `request.get_host()`
to identify the group.

See [group.md](group.md) for the full `auth_domain` field and `resolve_by_auth_domain()` reference.

---

## Templates

- `account/login.html` — full mojo-auth webapp. Override in your project's templates dir.
- `account/bouncer_challenge.html` — challenge page using default branding; override logo/brand via `BOUNCER_CHALLENGE_LOGO_URL` / `BOUNCER_CHALLENGE_BRAND` per group.
- `account/bouncer_decoy.html` — honeypot login at `/login`, `/signin`.

Static assets in `account/static/account/`:
- `mojo-auth.js` — authentication webapp
- `mojo-auth.css` — stylesheet (CSS variable theming)
- `mojo-bouncer.js` — embeddable bot-detection gate (v2.0.0) for any page
- `mojo-bouncer.css` — overlay stylesheet
- `mojo-sentinel.js` — lightweight in-session telemetry client

All five are served via `/account/static/<filename>` from the bouncer host.

---

## Continuous Detection

The one-shot `RiskScorer` handles the gate. For activity *after* the gate
(in-session, gameplay, sustained API use), the streaming scorer runs every
time new `BouncerSignal(stage='event')` rows are written and accumulates a
session-level risk score per `muid`.

Three pieces:

1. **`mojo-sentinel.js`** — client-side telemetry. Auto-collects passive
   signals (visibility transitions, focus/blur, paste events, click coordinate
   buckets, inter-action timing, page lifetime, idle gaps). Exposes
   `MojoSentinel.observe(category, payload)` so the host app pushes its own
   events. Batched flushes (default every 15s or 25 events) POST to
   `/api/account/bouncer/event` with `credentials: 'include'`.

2. **Streaming scorer** — `score_session(muid)` walks the last ~1k
   `BouncerSignal` rows in the window, runs every registered stream analyzer,
   accumulates `score_delta`, and writes a Redis high-water value at
   `bouncer:session_risk:{muid}` with TTL `BOUNCER_SESSION_RISK_TTL` (default
   24h). Inline, ~10–50ms; called automatically by the `/event` endpoint
   after batched-event persist and by app backends after their own
   `BouncerSignal.objects.create(...)`.

3. **Gradient enforcement** — `apply_session_response(device, score, user)`
   maps the new score to one of four bands. The framework sets flags and
   fires incidents; apps decide what each flag means.

### Stream analyzer plugin pattern

Same shape as the one-shot `@register_analyzer`:

```python
from mojo.apps.account.services.bouncer.stream_scoring import (
    BaseStreamAnalyzer, register_stream_analyzer,
)

@register_stream_analyzer
class MyDomainAnalyzer(BaseStreamAnalyzer):
    """Domain-specific heuristic (e.g. game reaction-time floor)."""
    name = 'my_domain_signal'

    @classmethod
    def analyze(cls, muid, signal_window, device):
        # signal_window is a list of BouncerSignal rows (newest first).
        # Read only from these rows; don't query the DB here.
        score_delta = 0
        triggered = []
        for sig in signal_window:
            raw = sig.raw_signals or {}
            if raw.get('event_type') == 'my_event_kind':
                score_delta += 20
                triggered.append(cls.name)
                break
        return score_delta, triggered
```

Register the module via your app's `apps.py:ready()` (import the module so
the decorators run).

### Universal stream analyzers (shipped)

| Name | Heuristic |
|---|---|
| `extended_session_no_idle` | 4h+ session with zero idle gaps; scaled severity at 4h/8h/12h |
| `tab_never_hidden` | 4h+ session with zero `visibilitychange` events |
| `coordinate_quantization` | > 100 clicks confined to < 5 coordinate buckets |
| `action_interval_regular` | Lag-1 autocorrelation > 0.9 on ≥ 50 inter-action intervals |
| `paste_into_sensitive_field` | Paste event with target = `input[type=password]` |

Score deltas are hardcoded in the analyzer classes — no parallel
`BOUNCER_STREAM_WEIGHTS` setting.

### Enforcement bands

| Score | Band | Side effects |
|---|---|---|
| ≥ 90 | `freeze` | `device.risk_tier='blocked'`, `block_count++`, fires `security:bouncer:session_freeze` (level 9), calls `BOUNCER_SESSION_FREEZE_HANDLER` if set |
| ≥ 70 | `shadow_ban` | `user.set_protected_metadata('bouncer_shadow_banned', True)`, fires `security:bouncer:session_shadow_ban` (level 8) |
| ≥ 50 | `require_step_up` | `user.set_protected_metadata('bouncer_require_step_up', True)`, fires `security:bouncer:session_step_up` (level 6) |
| ≥ 30 | `monitor` | Fires `security:bouncer:session_suspect` (level 6), no flag changes |
| < 30 | `noop` | nothing |

Override the band thresholds via settings:

```python
BOUNCER_SESSION_BANDS = {
    'freeze': 95,
    'shadow_ban': 80,
    'require_step_up': 60,
    'monitor': 40,
}
```

### Registering a freeze handler

Apps own the meaning of "freeze" in their domain. Point the framework at a
callable via the `BOUNCER_SESSION_FREEZE_HANDLER` dotted-path setting:

```python
# settings.py
BOUNCER_SESSION_FREEZE_HANDLER = 'apps.foo.services.bouncer.freeze_user'

# apps/foo/services/bouncer.py
def freeze_user(user, device, risk_score):
    # Close active gameplay sessions, force-logout, notify compliance, etc.
    user.is_active = False
    user.save(update_fields=['is_active'])
```

The framework wraps the call in try/except — handler failures don't break
scoring or device-tier updates.

---

## Static Page Gating

`GET /api/account/bouncer/verify_pass` is a lightweight endpoint designed
for nginx `auth_request`. It does two checks in order:

1. **Signature cache pre-screen** — known-bot IPs/UAs (from the existing
   `BotSignature` Redis cache) get 401 with `X-Bouncer-Reason: signature`
   at the edge, before the cookie is even consulted. Means nginx blocks
   signature-matched bots across every protected location without the
   request reaching application code.
2. **`mbp` cookie validation** — if the request carries a valid pass
   cookie, returns 200 with `X-Bouncer-Muid: <muid>` for upstream logging.

Otherwise: 401 with `X-Bouncer-Reason: no_cookie` or `invalid_cookie`. Body
is always empty (nginx `auth_request` discards it).

### Deployment shapes

| Shape | App host | Bouncer host | Works? |
|---|---|---|---|
| A — Same domain | `example.com/protected` | `example.com/auth` | Yes — natural cookie sharing |
| B — Subdomains | `app.example.com` | `auth.example.com` | Yes — set `BOUNCER_PASS_COOKIE_DOMAIN='.example.com'` |
| C — Separate eTLD+1 | `marketing.com` | `auth.example.com` | **Not in v1.** Browsers will not share cookies across registrable domains. Would require a signed-token redirect dance — separate request when a real consumer surfaces. |

### nginx drop-in

The shared include + worked example ship in this repo at
[`docs/web_developer/account/nginx/`](../../web_developer/account/nginx/). See the
"nginx Drop-in Protection" section in the web_developer bouncer doc for the
exact config.

---

## Cross-Origin Embedding

The bouncer endpoints are normally same-origin. To allow a separate origin
(SPA at `app.example.com` calling bouncer at `auth.example.com`) to call
the bouncer with credentials, list the SPA origin in `BOUNCER_ALLOWED_ORIGINS`:

```python
BOUNCER_ALLOWED_ORIGINS = [
    'https://app.example.com',
    'https://playground.example.com',
]
```

The CORS middleware sets `Access-Control-Allow-Origin: <origin>` (specific
origin, not `*`) and `Access-Control-Allow-Credentials: true` only for
requests whose `Origin` matches the allowlist AND whose path is a bouncer
path (`/api/account/bouncer/*` or `/account/static/mojo-*`). Non-bouncer
paths and non-allowlisted origins keep the existing wildcard behavior.

OPTIONS preflights from allowlisted origins return 200 with the same
credentialed headers, so JS clients can `fetch(..., credentials: 'include')`
without ceremony.

**Identity stitching across origins** — `mojo_device_uid` localStorage is
per-origin. A static site at `marketing.example.com` embedding sentinel
served from `auth.example.com` cannot read the auth host's localStorage —
each origin generates its own duid. Server-side fingerprint stitching is
the fallback. Same-origin and subdomain (Shape A/B) deployments stitch
automatically via shared cookies and a shared localStorage key.

---

## Bouncer-as-a-Service Deployment

Any django-mojo install can serve as the bot-detection backplane for N
consumer apps. The bouncer host runs Django; consumer apps (static sites,
SPAs, server-rendered pages on other stacks) point at it via:

- `mojo-bouncer.js` and `mojo-sentinel.js` loaded from the bouncer host
- `/api/account/bouncer/{assess,event,verify_pass}` called cross-origin
- nginx `auth_request` against the bouncer host for static-page gating

Per-consumer branding works automatically via the existing group resolution
(`Group.auth_domain` or `?group_uuid=<uuid>`) — the bouncer challenge page
shows the right logo and brand for each consumer with no extra wiring.

Capacity planning: `verify_pass` is the highest-volume endpoint (one hit
per `auth_request`-gated page load until the mbp cookie short-circuits it).
The work is one Redis read for the signature cache and one HMAC verification
for the cookie — sub-millisecond on a healthy instance. The `assess` and
batched `/event` endpoints are heavier (DB writes + scoring) but lower
volume.

To onboard a new consumer:

1. Add its origin to `BOUNCER_ALLOWED_ORIGINS` in the bouncer's settings.
2. (Optional) Create a `Group` with `auth_domain` set to the consumer's
   bouncer-page host, or instruct the consumer to pass `?group_uuid=<uuid>`
   on bouncer redirects. Configure per-group branding via the standard
   `settings.set('AUTH_LOGO_URL', '...', group=group)` pattern.
3. Consumer embeds `mojo-bouncer.js` and/or `mojo-sentinel.js` from the
   bouncer host with `data-api-base="https://<bouncer-host>"`.
4. If the consumer also wants nginx-level gating, install the
   `mojo-bouncer.conf` include and add `auth_request /_mojo_bouncer_check;`
   to the protected locations.

No new database rows, no new permissions, no migrations.

---

## Refreshing the Signature Cache

After manually adding/editing `BotSignature` records:

```python
from mojo.apps.account.services.bouncer.learner import refresh_sig_cache
refresh_sig_cache()
```

Or publish the scheduled job:

```python
from mojo.apps import jobs
jobs.publish('mojo.apps.account.asyncjobs.refresh_bouncer_sig_cache', {})
```

---

## Public Messages (Contact / Support)

Public (unauthenticated) contact / support intake reuses the bouncer gate so the
same bot protection that covers login also covers every inbound message.

```
Request → GET BOUNCER_CONTACT_PATH (default: /contact)
              ↓  (same pipeline as /auth, page_type='public_message')
     signature cache → pass cookie → pre-screen → decoy / challenge / page
              ↓
     POST /api/account/bouncer/message
        @md.requires_bouncer_token('public_message') — single-use token
        @md.strict_rate_limit('public_message_submit', ip_limit=5, ip_window=300)
              ↓
     PublicMessage saved + incident event + metric + notify_admins(msg)
```

### Kinds

Field schemas live in `mojo.apps.account.services.public_message.KIND_SCHEMAS` —
a single dict drives both form rendering and submit validation. v1 ships two:

| Kind | Common fields | Metadata fields |
|---|---|---|
| `contact_us` | name, email, message | company (optional) |
| `support` | name, email, message | category (billing/account/bug/other), severity (low/normal/high) |

Adding a kind means adding one entry to `KIND_SCHEMAS`. No template or validator
changes are required.

### Free-form metadata

Clients can attach an arbitrary tracking payload by POSTing `metadata: {...}`
alongside the normal form fields. The service sanitizes it:

- Primitives only (`str` / `int` / `float` / `bool` / `None`) — nested dicts
  and arrays are dropped.
- Keys match `[A-Za-z0-9_.-]+` and are ≤ 64 chars; strings ≤ 500 chars.
- Max 25 keys.
- Keys owned by the kind schema (e.g. `category`, `severity`, `company`)
  cannot be spoofed via the client `metadata` blob — kind-specific values win.
- Client extras skip `content_guard` — a utm token like `black+friday`
  shouldn't be moderated.

The merged result lives on `PublicMessage.metadata`. Admin UIs should render
kind-known keys with friendly labels and fall through to a generic
`key → value` list for anything else.

### Endpoint

```
GET  /contact?kind=<kind>      — bouncer-gated HTML form page
POST /api/account/bouncer/message  — submit (bouncer token required)
GET/POST /api/account/public_message[/<pk>]  — admin list / detail
```

Unknown `kind` on the page falls back to `contact_us`. Unknown `kind` on the
submit endpoint returns 400.

### Model

```python
from mojo.apps.account.models import PublicMessage

msg = PublicMessage.objects.filter(status='open').latest('created')
msg.kind          # 'contact_us' | 'support'
msg.name, msg.email, msg.subject, msg.message
msg.metadata      # kind-specific fields (company, category, severity, …)
msg.status        # 'open' | 'closed'
msg.group         # set when the bouncer resolved a group for the request
msg.ip_address    # captured at submit
```

RestMeta:
- `VIEW_PERMS = ["view_support", "security", "support"]`
- `SAVE_PERMS = ["manage_support", "security", "support"]`
- `DELETE_PERMS = ["manage_support"]`
- `GROUP_FIELD = "group"` — admins with only group-scoped perms see just their group's messages.

### Notifications

Every flagged user receives a templated email when a message is submitted.
Flag is a single boolean under the `protected` metadata namespace:

```python
user.set_protected_metadata("notify_public_messages", True)
```

- Ungrouped message → every flagged user across the system.
- Group-scoped message → only flagged users who are active members of that group.
- Per-recipient send failures are logged and skipped; the loop continues.

Admin tooling is expected to set this flag — it sits under `protected` so
end-users cannot toggle their own subscription through the standard user REST
graph.

Email template: `public_message_notify` (seed included; override by name in
`EmailTemplate` or via the `PUBLIC_MESSAGE_NOTIFY_TEMPLATE` setting).

### Settings

| Setting | Default | Purpose |
|---|---|---|
| `BOUNCER_CONTACT_PATH` | `contact` | URL path for the gated contact/support page |
| `BOUNCER_PUBLIC_MESSAGE_MAX_LENGTH` | `4000` | Cap on the `message` field at submit time |
| `PUBLIC_MESSAGE_NOTIFY_SUBJECT` | `"New {kind} message"` | `.format(kind=...)` substitution |
| `PUBLIC_MESSAGE_NOTIFY_TEMPLATE` | `public_message_notify` | EmailTemplate name |

### Rollout

The submit endpoint uses `@md.requires_bouncer_token('public_message')`. With
`BOUNCER_REQUIRE_TOKEN=False` (default), missing or invalid tokens are logged
but the request proceeds — safe for gradual rollout behind a marketing site
that may not yet be serving the bouncer gate. Flip to `True` once clients are
updated, or opt-in per-group via `group.metadata['require_bouncer_token']=True`.

The contact page itself is always bouncer-gated — there is no opt-out for the
page pipeline.

### Moderation

The service runs `mojo.helpers.content_guard.check_text` on the name, subject,
and message fields at submit time. A `decision='block'` result raises
`ValueError('<field>:blocked')` which the endpoint maps to 400. Any exception
inside content_guard is swallowed and logged (fail-open) so a broken
moderation engine cannot take contact submissions offline.

