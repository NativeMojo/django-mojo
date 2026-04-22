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
on a custom auth domain — or includes a `?group=<uuid>` query param — the
bouncer resolves the group and applies its scoped `AUTH_*` settings.

### Group detection order

1. **Hostname** — `Group.resolve_by_auth_domain(hostname)` looks up the active
   group whose `auth_domain` matches the request host. Result is Redis-cached
   (24h for hits, 1h for misses).
2. **`?group=<uuid>` query param** — fallback for platforms that share a domain.
   The group UUID is preserved through the challenge redirect and OAuth round-trip.

### Query params forwarded through the challenge

`_serve_challenge()` preserves these params when building the post-challenge
login redirect URL: `group`, `redirect` (and aliases `next`, `returnTo`), and
`back`. Any param missing from the original request is omitted from the
forwarded query string.

### Configuring a white-label group

```python
from mojo.helpers import settings
from mojo.apps.account.models import Group

group = Group.objects.get(uuid='...')

# Assign the custom auth hostname
group.auth_domain = 'auth.clientbrand.com'
group.save()

# Set group-scoped auth settings
settings.set('AUTH_APP_TITLE', 'Client Brand', group=group)
settings.set('AUTH_LOGO_URL', 'https://cdn.client.com/logo.svg', group=group)
settings.set('AUTH_SUCCESS_REDIRECT', '/client-dashboard/', group=group)
settings.set('AUTH_ENABLE_GOOGLE', True, group=group)
```

All `AUTH_*` settings resolve per-group using the parent-chain fallback:
group → parent group → global.

### Challenge page branding

The bouncer challenge page uses REDACTED branding by default. To override it
for a specific group (opt-in only):

```python
settings.set('BOUNCER_CHALLENGE_LOGO_URL', 'https://cdn.client.com/logo.svg', group=group)
settings.set('BOUNCER_CHALLENGE_BRAND', 'CLIENT BRAND', group=group)
```

`BOUNCER_CHALLENGE_LOGO_URL` and `BOUNCER_CHALLENGE_BRAND` only take effect
when a group is resolved. Requests with no group always use REDACTED branding.

### OAuth round-trip

`group_uuid` is embedded in the OAuth state so branding survives the
provider redirect. The callback reconstructs `?group=<uuid>` and appends it
to the frontend redirect URI before handing back to the auth page.

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
- `account/bouncer_challenge.html` — REDACTED-branded challenge page by default; override logo/brand via `BOUNCER_CHALLENGE_LOGO_URL` / `BOUNCER_CHALLENGE_BRAND` per group.
- `account/bouncer_decoy.html` — honeypot login at `/login`, `/signin`.

Static assets in `account/static/account/`:
- `mojo-auth.js` — authentication webapp
- `mojo-auth.css` — stylesheet (CSS variable theming)

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

