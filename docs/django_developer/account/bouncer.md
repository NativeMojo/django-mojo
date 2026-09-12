# Bouncer — Django Developer Reference

Server-side risk screening for django-mojo's hosted login, registration, and
contact pages, plus a separate embeddable bouncer SDK. Hosted pages use a
bounded recovery check before exposing the real form. The slider is a modest
effort check, not proof that a visitor is human; authentication, token
enforcement, permissions, and rate limits remain separate controls.

See also: [Auth Pages](auth_pages.md) for the login/registration page setup,
branding, OAuth configuration, and nginx setup.

---

## How It Works

```
GET /auth, /register, or /contact
  → current risk/signature/restriction check
  → valid matching _muid + mbp cookies: real page with scoped form descriptor
  → otherwise: Continue / target slider / operator recovery / selected decoy
  → hosted check or slider completion: set mbp
  → separate hosted confirm operation: verify returned cookie and restrictions
  → navigate once to the real page
  → acquire a fresh token before each protected form submission
  → existing @md.requires_bouncer_token decorator validates and consumes token
```

`mojo-hosted-bouncer.js` owns this flow. It does not use the public
`mojo-bouncer.js` SDK or store a challenge token in localStorage.

### Hosted decision policy

| Situation | Result |
|---|---|
| Active signature match or retained current evidence reaches the page's block threshold | Selected decoy, including when the restriction arrives during a check |
| Existing `blocked` device history or streaming freeze, without qualifying current evidence | Operator-recovery guidance; restriction remains |
| Valid pass for the returning `_muid`, with no current restriction | Real page |
| Raw score allows, no pass | One Continue check |
| Recoverable uncertainty | Target slider with tap/click and keyboard alternatives |
| Three wrong answers | 60-second cooldown, then explicit Retry |
| Expired descriptor, missing cookies, unavailable state, or failed request | Recovery/error guidance; no success or automatic navigation |

`services/bouncer/hosted_gate.py` applies this policy on page loads and hosted
operations. `RiskScorer` retains its public score and decision; its private
metadata tracks the uncapped total, recoverable contribution, historical block
contribution, and analyzer failures. Recovery credit is bounded by the actual
positive contribution of explicitly listed built-in analyzer classes. It covers
ordinary interaction, browser-capability, identity/session, privacy-network,
header, and non-blocked-history friction. Plugin contributions, automation
artifacts, headless UA, honeypot completion, known attacker/abuser evidence, and
historical blocks receive no recovery credit. Credit is calculated before the
public score is capped at 100. Analyzer failure ends in recovery.

Hosted outcomes write neutral `BouncerSignal` rows (`decision='log'`) and
`bouncer:hosted:<action>` metrics directly. They do not enter the legacy event,
incident-promotion, or learner paths. Wrong answers, keyboard/touch use, storage
refusal, and transport failures do not promote device reputation. Successful
recovery never clears device tiers, signatures, streaming high-water scores,
user enforcement flags, geofence rules, or throttles. Incorrect legacy blocked
records require operator review; no automatic rehabilitation is performed.

### Hosted descriptor protocol

**POST `/api/account/bouncer/assess`** is public, with the existing IP limit
(60/minute) and returning `_muid` limit (30/minute). A `hosted_gate` object
selects the hosted protocol; invalid hosted input never falls back to legacy
assessment. Hosted controls send same-origin requests with site cookies,
independently of `BOUNCER_API_BASE`. There is no new Origin allowlist; authority
comes from the opaque render-issued descriptor and its server-held binding.

```json
{
  "hosted_gate": {
    "version": 1,
    "descriptor": "<opaque-render-issued-descriptor>",
    "operation": "submit",
    "request_id": "<unique-request-id>",
    "answer": 50
  },
  "signals": {"behavior": {}, "gate_challenge": {}}
}
```

| Operation | Purpose |
|---|---|
| `check` | Continue or obtain the current slider/cooldown state |
| `submit` | Verify a finite numeric slider answer from 0 through 100 |
| `confirm` | Confirm the exact granted pass cookie returned on a separate request |
| `token` | Use a real-page form descriptor and valid pass to issue a fresh token |

`version` is 1; `descriptor` is a 32-character URL-safe identifier; `request_id`
is 8–64 alphanumeric, underscore, or hyphen characters. `signals` is optional
and must be an object whose section values are objects. The renderer binds the
descriptor to the lowercased request host, returning `_muid`, resolved group,
and purpose (`login`, `registration`, or `public_message`). Client purpose/group
fields cannot replace that scope. A middleware-generated identity cannot stand
in for a missing returning cookie.

`hosted_challenge.ChallengeStore` uses atomic Redis transitions with a shared
retry budget per host and `_muid`, across purposes, tabs, and reloads. Challenge
descriptors last **5 minutes**; real-page form descriptors last **30 minutes**.
At most eight descriptors are retained per identity; oldest entries are evicted.
The target is 25–75 on a 0–100 scale, with ±8 tolerance. Three wrong answers
within the retry window start a **60-second cooldown**. A new descriptor does
not reset the budget. Retry after cooldown is explicit. Repeating a submission's
`request_id` does not spend another attempt; lost grant responses retain the
original cookie issue time rather than extending its lifetime.

Responses use the normal JSON envelope, with an explicit action:

```json
{"status": true, "data": {"decision": "allow", "next_action": "check_cookie"}}
```

`check_cookie`, `allow`, and `token` carry `decision='allow'`; unresolved
`slider`, `cooldown`, `decoy`, `recovery`, and `error` outcomes carry
`decision='block'`. A slider response also includes `target`, `tolerance`, and
`attempts_remaining`; cooldown includes `retry_after` seconds. `check_cookie`
sets `mbp` but permits no navigation until `confirm` returns `allow`. Only
`token` returns a token. Reasons distinguish `cookies`, `expired`, `restart`,
`operator`, `unavailable`, and `invalid`, without detector details. Invalid
protocol input returns 400; missing cookies/expired state return 409; invalid
form scope or inactive group can return 403; unavailable state returns 503.
The existing rate limiter can return 429. Clients must validate HTTP status,
JSON shape, and `next_action`, and never treat an arbitrary 200 as a pass.
Recovery/error responses may include an opaque `reference` for the operator to
find the corresponding recovery log entry; it is not a descriptor or credential.

Assessment bodies and descriptor-bearing hosted HTML responses are redacted by the shared sensitive-body logging policy.
Descriptors, answers, credentials, and reset tokens are not stored in hosted
outcome audit payloads.

### Form tokens and deployment

`auth_base.html` installs an optional async `bouncerTokenProvider` in
`MojoAuth.init()`. `MojoAuth.getBouncerToken(purpose, context)` always returns a Promise.
The provider runs before `login` (`login`), `register` and
`startPhoneRegister` (`registration`); `contact.html` awaits it explicitly with
`public_message`. Each protected submission, including a retry after wrong
credentials, gets a fresh single-use token. Provider failure stops submission
and shows recovery guidance. Other MojoAuth calls keep their existing behavior;
without a provider, the legacy token lookup and request behavior are unchanged.
The provider receives `(purpose, context)`, where `context.duid` carries the
protected request's device ID when present. The hosted token request forwards
that ID so the token and its consuming request share the existing device binding.

The hosted provider uses the descriptor's server-held purpose, not the caller's
argument. It checks the returning pass and current restrictions, then uses the
existing `TokenManager`, token format, exact-IP binding, nonce store, and TTL.
A configured external `BOUNCER_API_BASE` still needs compatible signing keys and
shared nonce infrastructure with the page origin. Hosted verification does not
follow that external base. CORS permission alone does not make tokens portable
between installations.

### Recovery and selected decoys

The slider supports drag-and-release, tap/click positioning plus Confirm, and
arrow keys plus Confirm, with visible focus and live status. Requests have an
8-second client timeout. Network/JSON failures show an explicit Retry; cookie,
expired-check, and operator restrictions explain the next step without claiming
verification. Help stays on the ungated shell and points to the operator's usual
support channel. Embedded contact pages with unavailable cookies can open the
same contact page in a top-level tab.

A **selected decoy** uses a local rejection sink: no credential-bearing form
submission, no named credential inputs, and disabled controls until its script
initializes. Clicking Sign In clears the password and displays a generic error
locally. It neither calls scanner endpoints nor emits `honeypot_post` events.
Explicit `/login`, `/signin`, and `/signup` scanner honeypots retain their
existing POST and logging behavior.

### Legacy API and SDK compatibility

Bodies without `hosted_gate` keep the existing `allow` / `monitor` / `block`
assessment contract, scoring/persistence/learning, cross-origin token issuance,
and pass-cookie behavior. `mojo-bouncer.js`, `mojo-sentinel.js`, `/event`, and
nginx `/verify_pass` keep their existing semantics. The hosted recovery protocol
does not harden the SDK's existing fail-open behavior or establish a universal
human-attestation boundary. The sections below describe those shared or legacy
facilities where applicable.

---

## Opt-In Setup

Token enforcement is opt-in via settings. The hosted auth/contact pages always
use their page gate; `BOUNCER_REQUIRE_TOKEN=False` does not disable it.

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

Risk tiers (hosted recovery does not change these legacy assessment tiers):
- `unknown` — first seen
- `low` — passed challenge
- `medium` — triggered 1–2 signals
- `high` — triggered 3+ signals or failed challenge repeatedly
- `blocked` — retained restriction; hosted recovery requires operator review
  unless independent current evidence selects a decoy. Older rows do not prove
  how the device acquired the tier.

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

After a legacy assessment block with `risk_score >= BOUNCER_LEARN_MIN_SCORE`, the
`learn_from_block` background job:

1. Marks the `BouncerDevice` as `risk_tier='blocked'`
2. Increments subnet /24 counter in Redis; creates `BotSignature` when threshold hit
3. Increments UA counter; creates `BotSignature` for repeated identical UAs
4. Increments fingerprint counter; creates `BotSignature` for repeat fingerprints
5. Hashes triggered signal set; detects coordinated campaigns across IPs
6. Rebuilds the Redis signature cache used by pre-screen

The Redis cache is also rebuilt by the scheduled `refresh_bouncer_sig_cache` job.
Hosted recovery outcomes do not invoke this job.

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

`_serve_challenge()` preserves `group_uuid`, `redirect` (and aliases `next`,
`returnTo`), `back`, `force_reauth`, and valid theme/appearance selections when
building the post-challenge login or registration URL. For `/auth` and
`/register`, it also preserves values whose names are declared by the resolved
`registration.extra_fields` schema. Resolution includes deployment-wide
`AUTH_CONFIG` and inherited group config; string and object entries normalize
to the same name allowlist. Legacy `REGISTRATION_EXTRA_FIELDS` alone does not
make a query parameter eligible for a browser hop.

Each extra value must be one non-empty scalar string of at most 512 characters
with no ASCII control character. Repeated query values, list-shaped values,
empty strings, controls, and oversize values are dropped rather than truncated.
Canonical/identity names (`first_name`, `last_name`, `email`, `phone`, `dob`,
`password`, `username`, `phone_number`); tenancy, navigation, and display names
(`group`, `group_uuid`, `redirect`, `next`, `returnTo`, `back`,
`webapp_base_url`, `redirect_uri`, `force_reauth`, `auth_theme`,
`auth_appearance`); credential/device names (`token`, `code`, `state`,
`auth_code`, `bouncer_token`, `verified_phone_token`, `session_token`,
`mfa_token`, `access_token`, `refresh_token`, `recovery_code`,
`current_password`, `new_password`, `duid`, `muid`, `fp`); and OAuth/passkey
names (`client_id`, `response_type`, `scope`, `code_challenge`,
`code_challenge_method`, `code_verifier`, `grant_type`, `resource`,
`challenge_id`, `credential`) cannot be extras.
All values are encoded with `urlencode`; they cannot alter the server-selected
root-relative destination path. Undeclared parameters (including `utm_*`) are
not forwarded. Contact and passkey pages and OAuth-consent destinations do not
receive registration extras. Password-reset tokens retain the same-tab
`mat_reset_token` handoff, with a confirmed-cookie reload of the original URL
when storage is unavailable; see [Auth Pages](auth_pages.md#a-reset-link-survives-a-cold-bouncer-challenge).
Redirect canonicalization is unchanged. Contact challenges preserve a valid
`kind` from the public-message schema.

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
provider redirect. The callback merges `code`, `state`, and `group_uuid` into
the frontend redirect URI's query with `&` — not a naive appended `?` — so any
query the frontend URI already carries (e.g. `?redirect=`) is preserved rather
than clobbered (using `group_uuid` rather than `group` so the framework
dispatcher accepts the next request through the rest of the flow). See
[OAuth § Per-Request redirect_uri](oauth.md#per-request-redirect_uri) for the
full merge mechanics.

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
- `account/bouncer_challenge.html` — hosted Continue/slider/recovery shell; override logo/brand via `BOUNCER_CHALLENGE_LOGO_URL` / `BOUNCER_CHALLENGE_BRAND` per group.
- `account/bouncer_decoy.html` — selected safe sink or explicit scanner honeypot, selected by server context.
- `account/_bouncer_selected_decoy.html` — non-submitting credential UI shared by initial and post-assessment selected decoys.

Static assets in `account/static/account/`:
- `mojo-auth.js` — authentication webapp
- `mojo-auth.css` — stylesheet (CSS variable theming)
- `mojo-bouncer.js` — embeddable bot-detection gate (v2.0.0) for any page
- `mojo-hosted-bouncer.js` — hosted descriptor client, recovery controls, and form-token provider
- `mojo-bouncer.css` — overlay stylesheet
- `mojo-sentinel.js` — lightweight in-session telemetry client

These assets are served via `/api/account/static/<filename>` from the bouncer host.

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
| C — Separate eTLD+1 | `marketing.com` | `auth.example.com` | **Token flow only.** The credentialed `assess`/`event`/`message` calls work by default (see [Cross-Origin Embedding](#cross-origin-embedding)), so `@md.requires_bouncer_token` is usable from a tenant-owned domain. The `mbp` pass cookie still is not: `_set_pass_cookie` uses `samesite='Lax'`, so browsers refuse to store it cross-site and the repeat-visit challenge skip never engages. |

### nginx drop-in

The shared include + worked example ship in this repo at
[`docs/web_developer/account/nginx/`](../../web_developer/account/nginx/). See the
"nginx Drop-in Protection" section in the web_developer bouncer doc for the
exact config.

---

## Cross-Origin Embedding

**Credentialed cross-origin access to the public bouncer API is ON by default.**
This is the legacy API contract. The hosted client uses same-origin requests
and render-issued descriptors; it introduces no new CORS allowlist.
This is a REST API platform: anybody can call it, and third-party callers are the
point. Out of the box the CORS middleware echoes any well-formed `http(s)`
request `Origin` back with `Access-Control-Allow-Credentials: true` on the three
public bouncer endpoints, so an SPA or a tenant-owned domain can
`fetch(..., credentials: 'include')` with no server-side wiring at all.

OPTIONS preflights take the identical decision, so JS clients work without
ceremony.

### Restricting it: `BOUNCER_ALLOW_ANY_ORIGIN = False`

To limit the public bouncer endpoints to a curated set of origins, opt out:

```python
BOUNCER_ALLOW_ANY_ORIGIN = False    # default True
BOUNCER_ALLOWED_ORIGINS = [
    'https://app.example.com',
    'https://playground.example.com',
]
```

With the flag `False`, the middleware sets `Access-Control-Allow-Origin: <origin>`
(specific origin, not `*`) and `Access-Control-Allow-Credentials: true` only for
requests whose `Origin` matches `BOUNCER_ALLOWED_ORIGINS` AND whose path is a
bouncer path (`/api/account/bouncer/*` or `/account/static/mojo-*`). Everything
else keeps the wildcard fallback.

The allowlist is consulted **first and unconditionally**, in both modes. It is
therefore additive under the default and authoritative under the opt-out, and
entries that are not well-formed http(s) origins — `chrome-extension://…`,
`capacitor://localhost` — keep working either way, because the well-formedness
guard applies only to the any-origin echo branch.

### What the default does and does not cover

| Path | Any-origin echo? |
|---|---|
| `/api/account/bouncer/assess` | **Yes** |
| `/api/account/bouncer/event` | **Yes** |
| `/api/account/bouncer/message` | **Yes** |
| `/api/account/bouncer/verify_pass` | No — server-to-server (nginx `auth_request`), and the sole carrier of `X-Bouncer-Muid` |
| `/api/account/bouncer/device`, `/signal`, `/signature` | No — permission-gated admin endpoints returning fingerprints, IPs, muids and geo |
| `/account/static/mojo-*` | No — `<script src>` is not a CORS request, and the `*` fallback already serves it |

Those exclusions are not a restriction on legitimate third-party callers; the
admin endpoints are permission-gated regardless, and no browser client calls
`verify_pass`. An operator who lists an origin in `BOUNCER_ALLOWED_ORIGINS` can
still reach every bouncer path, exactly as before.

Also refused, in both modes:

- `Origin: null` — a sandboxed iframe, `data:` document or `file://` page. No
  real client sends it, and it names no origin to attribute the call to.
- Anything that is not a bare `http(s)://host[:port]` — a path, query, fragment,
  a non-http scheme, or any value carrying CR, LF, tab or a space.
- `Access-Control-Allow-Origin: *` is never sent together with
  `Access-Control-Allow-Credentials: true`.

The flag is read with `settings.get_static(..., kind='bool')` — **file-based
settings only**. A DB/Redis-backed `Setting` row (writable over REST with
`manage_settings`) cannot change the origin policy at runtime, in either
direction. An uncoercible value such as `"maybe"` degrades to the declared
default — which is now `True`, i.e. permissive — and logs a coercion warning, so
a typo'd opt-out does not silently take effect.

### What changes when you flip it

Setting the flag `False` stops the three public endpoints answering credentialed
cross-origin requests from origins you have not listed; they keep answering
non-credentialed ones through the `*` fallback, which is unchanged and has always
been there. Setting it back to `True` restores the echo.

Neither direction takes effect instantly for clients that have already
preflighted: `Access-Control-Max-Age: 86400` means a browser may keep acting on a
cached preflight decision for up to 24 hours.

Two things the flag does not affect at all. Mojo REST auth is header-based
(`Authorization`, never a cookie) and every mojo cookie is `SameSite=Lax`, so a
cross-*site* caller's `credentials: 'include'` request carries no cookies
regardless of this setting — in practice the origin check only ever gated whether
cookies could ride along on same-site-different-origin calls. And the public
endpoints are reachable cross-origin either way via the `*` fallback with
`credentials: 'omit'`.

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

1. Nothing, for CORS — the public `assess`/`event`/`message` endpoints answer
   credentialed cross-origin calls from any well-formed origin by default, which
   is what a consumer bringing its own domain needs. Only if this deployment has
   opted out with `BOUNCER_ALLOW_ANY_ORIGIN = False` do you add the consumer's
   origin to `BOUNCER_ALLOWED_ORIGINS`. See "Cross-Origin Embedding" above.
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
     current restrictions → matching pass → decoy / recovery / challenge / page
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

**Tenant-less messages need a GLOBAL grant.** `PublicMessage.group` is null
whenever the submission did not resolve to a white-label group — which is every
message submitted on the main platform domain, so null is the normal state, not
an edge case. A row with no tenant is reachable only through the flat
`request.user.has_permission` check, so triaging those messages (read, status
update, delete) requires `support` / `view_support` / `manage_support` as a
**global** grant; the same permission held only on a `GroupMember` row will not
reach them. Notification emails carry the full message content regardless, so a
group-scoped admin still sees submissions — they just cannot triage tenant-less
ones through the API. See [REST permissions](../rest/permissions.md) (maestro
item 953).

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
