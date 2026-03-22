# Django-Mojo Bouncer: Server-Gated Bot Detection

**Type**: request
**Status**: resolved
**Date**: 2026-03-21
**Resolved**: 2026-03-22

## Description

Move the Bouncer bot detection system into django-mojo as a first-class security feature of the
`account` app. The core architectural shift is: **Django gates the login page itself**, not the
client-side JS. Bots never receive the login form, its field names, or the auth API endpoints.
The client-side `mojo-bouncer.js` becomes a behavioral signal collector only — the actual gate
is server-side.

## Context

### Why This Exists

The current Bouncer lives in `apps/mojoverify/bouncer/` as a mojo-verify-specific app. It works
as follows today:

1. Django serves `auth/index.html` + `mojo-bouncer.js` to **everyone**, including bots
2. Client-side JS collects signals, shows a challenge, POSTs to `/api/bouncer/assess`
3. If bot: fullscreen block overlay (but the HTML/JS/endpoints are already in their hands)
4. If human: login form becomes usable

This has a fundamental flaw: **the gate is made of the same material it is guarding**. A
determined bot operator reads `mojo-bouncer.js` once and builds evasion for it. They can also
bypass the gate entirely by blocking the bouncer endpoints and hitting the login API directly.

### Why django-mojo Is the Right Home

django-mojo already has:
- GeoIP intelligence
- `account.Device` — device tracking
- Rate limiting (`@md.rate_limit`)
- The login handler itself — enforcement is trivial when you own the code
- The incident/reporting system
- Security-first design philosophy

The bouncer is not mojo-verify-specific. Any django-mojo project handling authentication
(login, registration, password reset) needs bot protection. Centralising it in `account`
means every project gets it.

### Prior Work

`mojo-verify` already has a working bouncer implementation to reference:
- `apps/mojoverify/bouncer/services/scoring.py` — scoring engine + pluggable analyzers
- `apps/mojoverify/bouncer/rest/assess.py` — Stage 1 assessment endpoint
- `apps/mojoverify/bouncer/rest/submit.py` — Stage 2 form signal endpoint
- `apps/mojoverify/bouncer/services/token_manager.py` — HMAC token signing + Redis nonces
- `apps/mojoverify/bouncer/models/device_session.py` — device reputation model
- `public/js/mojo-bouncer.js` — client-side signal collection + challenge UI

---

## The Architecture

### Login Page Serving Model

The login page is a **Django template that renders the mojo-auth webapp** — not a Django
form. `account/templates/account/login.html` is `auth/index.html` converted to a Django
template with only the host injected — all API paths are standard across every mojo
project and need no configuration:

```html
<script>
  const API_BASE = "{{ api_base }}";   {# host only, e.g. https://api.example.com #}
  const ON_SUCCESS = "{{ success_redirect }}";
</script>
<script src="{% static 'account/mojo-auth.js' %}"></script>
<script src="{{ bouncer_js_path }}"></script>  {# versioned/hashed path #}
```

- `API_BASE` is the only runtime injection — it is just the host, not endpoint paths.
  All mojo projects use the same API paths (`/api/login`, `/api/auth/passkeys/...`, etc.)
  so `mojo-auth.js` hardcodes them relative to `API_BASE` with no per-project config.
- `mojo-auth.js`, `mojo-auth.css` live in `account/static/account/` as static files
- `mojo-bouncer.js` is served at a hashed path injected by Django at render time
- All auth flows are REST API calls from `mojo-auth.js` — Django does no form processing
- Projects override the template via standard Django template override (`account/login.html`)

**The real gate is not URL obscurity — it is the token requirement on the endpoint.**
Since API paths are standard and known, a bot could call `/api/login` directly without
touching the login page. `@md.requires_bouncer_token()` on `on_user_login` closes this:
even with the correct URL, a valid bouncer token is required. The token is only issued
after clearing the challenge. The login page URL (`BOUNCER_LOGIN_PATH`) is noise
reduction only — bots scanning `/login` hit the decoy, bots that find `/access` still
cannot reach the API without a token.

### URL Obfuscation

The login page URL is configurable via `BOUNCER_LOGIN_PATH` (default `'access'`).
Deliberately not `/login`, `/signin`, or `/auth` — those are the first paths mass-scanning
bots try.

**`/access` vs `/mlogin`**: Prefer `'access'` — shorter, not branded, looks like a
generic resource to a human inspector. `/mlogin` reads as "mojo login" and would be
guessed quickly. Let projects configure their own.

**URL obscurity is additive, not load-bearing.** The real security property is that the
login form fields and API endpoint URLs are never in the DOM until after the challenge.
URL obscurity just reduces the noise floor.

**The decoy honeypot is the stronger move:**
Predictable paths (`/login`, `/signin`) are registered as Django endpoints that serve a
visually identical login page whose form POSTs to dead endpoints (`/account/session`).
That endpoint logs all submitted credentials and returns `{"error": "Invalid credentials"}`
with a realistic 300ms delay. Bots that find these paths think they found the real login
page — you get their credential sets for free.

**API endpoint URLs are also hidden:**
`mojo-auth.js` in the challenge page contains no login API URLs. The full login page
template injects the API base and endpoint paths as JS config. Since the source only loads
after passing the challenge, bots that don't clear the gate never see the API endpoints.
This is the stronger security property.

**Settings:**
```python
BOUNCER_LOGIN_PATH = 'access'          # the real login page path
BOUNCER_DECOY_PATHS = ['login']        # paths serving the honeypot page
BOUNCER_SUCCESS_REDIRECT = '/'         # where to send after login
BOUNCER_LOGO_URL = None                # optional branding override
BOUNCER_ACCENT_COLOR = None            # optional CSS --ma-primary override
```

### Phase 1: Server-Side Pre-Screening (before any HTML is served)

When a request arrives at `BOUNCER_LOGIN_PATH`, Django runs a fast pre-screen
**before rendering anything**:

```
Request → Django login view (BOUNCER_LOGIN_PATH)
             ↓
         Pre-screen (IP, headers, GeoIP, device cookie, rate)
             ↓
    ┌────────┼────────────┐
  Bot/      Suspicious   Known good
  clear       ↓            ↓
    ↓     Challenge-    Full login
  Decoy    only page      page
  page

Requests to BOUNCER_DECOY_PATHS → always serve decoy (honeypot)
```

**Pre-screen signals (server-side only, no JS needed):**
- IP reputation: datacenter, VPN, Tor, known attacker/abuser ranges
- Header analysis: missing Accept-Language, missing Accept-Encoding, headless UA patterns
  (HeadlessChrome, PhantomJS, Playwright, Puppeteer)
- Rate limiting: request frequency per IP and per device cookie
- Device cookie: known-good devices skip challenge; known-bad devices get blocked
- Request timing: too fast to be human (sub-200ms from cold IP with no history)
- HTTP/2 fingerprint anomalies (JA3/JA4 if available at the proxy layer)

**Three outcomes:**
1. **Clearly bot** → serve decoy page (visually identical to login, POSTs to dead
   endpoint). Do NOT reveal that detection fired.
2. **Suspicious / unknown** → serve the challenge-only page (see Phase 2)
3. **Known good device** (valid pass cookie + no threat signals) → serve full login page,
   bouncer JS runs in background for signal collection only

### Phase 2: The Challenge Page

If pre-screening is inconclusive, Django renders a minimal challenge page containing:
- Only the bouncer widget (moving target button, behavioral signal collection)
- **No login form fields**
- **No auth API endpoint URLs**
- A single endpoint: `POST /account/bouncer/challenge`

The challenge completes → backend validates → sets a signed pass cookie → **Django
redirects to the full login page**. The form fields and API endpoints only appear after
this point.

**Randomized challenge renders** — every render is slightly different to break automation,
but identical in effort to a human (always: click/tap a moving target, no puzzles):

Django generates a signed **render context** at page serve time:

```python
render_ctx = {
    'layout': 2,           # 0–3: selects one of 4 distinct DOM structures
    'label_idx': 7,        # 0–9: selects button copy ("Tap to continue", etc.)
    'css_nonce': 'a3f9c2', # random suffix applied to all CSS class names
    'hp_field': 'k7x2mb',  # randomized honeypot field name this render
    'btn_seed': 'e1d4f8',  # seeds button starting position and movement path in JS
    'issued_at': ...,
    'expires_at': ...,     # 5 min TTL
}
render_ctx_signed = hmac_sign(render_ctx)
```

The signed context is embedded in the page (not readable by bots without the key).
`POST /account/bouncer/challenge` must echo it back. Server re-validates signature and:
- Checks `expires_at` — stale renders rejected
- Checks honeypot field (named `hp_field`) — if filled, high-confidence bot signal,
  immediate block + feed to `BotLearner`
- Validates interaction signals against `btn_seed`

**What this breaks for bots:**
- CSS selectors and XPath break every session (class names change per render)
- DOM structure varies across 4 layouts — no stable XPath
- Honeypot field name changes every render — bots trained to skip known names fill it
- Replay attacks fail — render context is signed and short-lived
- Training an ML model on "challenge appearance" requires constant retraining against
  4 layouts × 10 labels × random class names × random button seeds

**What stays constant for humans:**
- Always one action: click/tap a moving target button
- Clear visual call to action, same branding, same theming
- No puzzles, no image recognition, no typing

This means:
- A bot blocking the challenge endpoint never reaches the login form
- A bot that reverse-engineers the challenge JS faces a different DOM every session

### Phase 3: Full Login Page (post-challenge)

Once the pass cookie is set, Django renders the full `account/login.html` with:
- The full `mojo-auth.js` webapp (login form, OAuth, passkey, password reset, magic link)
- `mojo-bouncer.js` in **passive mode** (signal collection only, no gate overlay)
- The bouncer token included in every auth API call (`bouncer_token` field)
- `API_BASE` injected by Django (host only; all endpoint paths are standard mojo paths)

The login endpoint validates the bouncer token server-side before processing credentials.
API paths are standard across all mojo projects — security comes from the token requirement
on the endpoint, not from hiding URLs.

---

## Security Considerations

### Token Design

The bouncer pass token must be:
- **HMAC-signed** with a server-side secret (tamper-proof)
- **IP-bound** — token issued to IP X is invalid from IP Y
- **Device-bound** — tied to the `duid` stored in localStorage/cookie
- **Single-use nonce** — stored in Redis with a TTL, consumed on use to prevent replay
- **Short-lived** — 10–15 minutes max. Long enough to complete login, short enough to
  limit replay window
- **Scoped** — token encodes what it permits (`page_type: login`) so a token earned on
  a registration page cannot be replayed on the login page

**Token payload:**
```json
{
  "duid": "...",
  "fingerprint_id": "...",
  "ip": "1.2.3.4",
  "risk_score": 12,
  "page_type": "login",
  "issued_at": 1710000000,
  "expires_at": 1710000900,
  "nonce": "..."
}
```

### Pass Cookie Design

The pass cookie (skips challenge for known good devices) must be:
- **HttpOnly** — not accessible to JS (prevents theft via XSS)
- **Secure** — HTTPS only
- **SameSite=Strict** — prevents CSRF-based replay
- **Short TTL** — 24h default, configurable per group
- **Signed** — HMAC of `(duid, ip_prefix, issued_at)` so it cannot be forged
- **Not a bypass of scoring** — even with a valid pass cookie, the backend still
  runs IP/header checks on every login attempt. The cookie only skips the
  interactive challenge, not the risk assessment.

### Fail-Open vs Fail-Closed

This is a critical design decision. Two failure modes:

**Fail-open** (current approach): if bouncer errors, let the user through.
- Pro: real users never get locked out due to infrastructure failure
- Con: bouncer outage = open door for bots

**Recommended: Fail-open for the gate, fail-closed for the token.**
- If the bouncer assessment endpoint is down → serve the full login page anyway
  (fail open, log the incident, alert oncall)
- If the login endpoint receives a request with `bouncer_token` missing or invalid
  → reject with 403 (fail closed)
- The distinction: infrastructure failures should not lock out users. But an
  explicit missing/bad token is not an infrastructure failure — it is suspicious.

**Configurable per group:**
```python
BOUNCER_REQUIRE_TOKEN = False          # global default (false until enforced)
# group.metadata["require_bouncer_token"] = True  # opt-in per group
```

This allows a gradual rollout: log missing tokens before hard-blocking.

### Decoy / Honeypot Strategy

When clearly-bot traffic is detected, do not reveal detection. Instead:
- Serve a page that looks identical to the login page but:
  - Form POSTs to a dead endpoint (`/account/session` instead of real login)
  - The dead endpoint logs the attempt and always returns a plausible-looking error
    (`{"error": "Invalid credentials"}`) with a realistic delay (200–400ms)
  - This wastes bot operator time and gives you rich data on their credential sets
- Never 403/404 immediately — that reveals the detection threshold

### Rate Limiting Layers

Multiple independent layers, each with its own limit:

| Layer | Scope | Limit |
|-------|-------|-------|
| Pre-screen view | IP | 60 req/min |
| Pre-screen view | IP (failed challenges) | 5/10min, then 1hr ban |
| Challenge endpoint | IP | 20 req/min |
| Login endpoint | IP | 20 req/min |
| Login endpoint | Username | 10 req/min (credential stuffing) |
| Login endpoint | duid | 10 req/min |

Limits are additive — exceeding any one is sufficient to escalate risk score.

### Device Reputation

`account.Device` should track:
- `risk_tier`: unknown / low / medium / high / blocked
- `event_count`: total assessments seen
- `block_count`: times blocked
- `last_seen_ip`: for IP drift detection
- `last_seen_at`
- `fingerprint_id`: browser fingerprint hash for stitching multiple duids

Tier escalation rules:
- First seen → `unknown`
- Passes challenge → `low`
- Triggered 1–2 signals → `medium` (challenge required every time)
- Triggered 3+ signals or failed challenge repeatedly → `high` (elevated scoring)
- Explicit block from scoring → `blocked` (pre-screen rejects immediately)

Blocked devices should remain blocked for a configurable TTL (default 24h) with
exponential backoff on repeated blocks.

### Signal Weighting Architecture

Scoring should be pluggable and settings-driven so operators can tune without
a code deploy:

```python
BOUNCER_SCORE_WEIGHTS = {
    'webdriver_flag': 25,
    'playwright_artifacts': 30,
    'puppeteer_artifacts': 30,
    'outer_size_zero': 20,
    'headless_ua': 20,
    'geo_datacenter': 15,
    'geo_tor': 35,
    'geo_known_attacker': 40,
    'gate_honeypot_filled': 50,
    'gate_click_too_fast': 20,
    'form_instant_fill': 30,
    ...
}

BOUNCER_THRESHOLDS = {
    'block': 60,
    'challenge': 30,
    'monitor': 15,
}
```

Per-page-type overrides:
```python
BOUNCER_THRESHOLDS_OVERRIDES = {
    'login': {'block': 65, 'challenge': 35},
    'registration': {'block': 55, 'challenge': 25},
    'password_reset': {'block': 50, 'challenge': 20},
}
```

Per-group overrides via `group.metadata["bouncer_thresholds"]` for tenants with
different risk tolerances.

### Device Identity (duid) Design

The device UUID (`duid`) is a persistent cross-session identifier that ties signals,
tokens, and reputation to a specific device. It is shared between `mojo-bouncer.js`
and `Rest.js` so the backend sees a consistent identity across all requests from the
same browser.

**Client-side storage (priority order):**
1. `localStorage('mojo_device_uid')` — canonical key, shared with `Rest.js`
2. `localStorage('mojo_duid')` — legacy bouncer key, auto-migrated on load
3. Cookie `mojo_device_uid` — fallback when localStorage is unavailable

On load, `DuidManager` reads in priority order, migrates legacy keys to the canonical
key, and generates a new UUID only if none exists. The value is then persisted to both
localStorage and a 1-year cookie.

**The duid is passed in every bouncer API call:**
- Stage 1 assess payload — always included
- Stage 2 submit payload — always included
- Event reporter calls — always included
- Auth API calls (`mojo-auth.js`) — `_withDevice()` helper appends both `duid` and
  `bouncer_token` to every login, passkey complete, OAuth complete, magic link, and
  password reset call

**Backend must record duid on every call, not just token validation.** Even requests
that fail the challenge or never reach the token stage carry a duid. Recording it
enables:
- Cross-session signal correlation: same device, multiple IP hops, multiple sessions
- IP drift detection: duid X was seen from IP A for 30 days, now suddenly IP B (VPN
  rotation, account sharing, or session hijack signal)
- Velocity patterns: how many assess calls per duid per hour (automated retry loops)
- Reputation bootstrapping: a new duid from a datacenter IP range starts at `medium`
  rather than `unknown` — IP context informs the initial tier before any signals fire
- Long-term blocklist: `blocked` tier devices remain blocked across sessions and IPs

**duid is not a security guarantee** — it can be cleared by the user. The backend
should treat a missing duid as `unknown` tier (not trusted, not blocked), never as a
bypass. A duid that has never been seen before from a datacenter IP is more suspicious
than one with 90 days of clean history from a residential ISP.

### Adaptive Bot Signature Learning

Once a bot is confirmed blocked at high confidence, the system registers its fingerprint
so future requests from the same campaign are caught at pre-screen — before any scoring
runs. This makes the gate measurably harder to evade over time.

**What "confirmed" means:**
Learning only fires when `decision = 'block'` AND `risk_score >= BOUNCER_LEARN_MIN_SCORE`
(default 80). Borderline blocks don't teach the system. Only high-confidence cases where
multiple independent signals aligned trigger learning.

**Signature types and escalation thresholds:**

| Type | Trigger | Default TTL |
|------|---------|-------------|
| `fingerprint` | 3+ confirmed blocks from same browser fingerprint | 7d |
| `user_agent` | 5+ confirmed blocks with identical UA string in 1h | 7d |
| `subnet_24` | 5+ confirmed blocks from same /24 in 1h | 24h |
| `subnet_16` | 20+ confirmed blocks from same /16 in 1h | 24h |
| `signal_set` | Same signal combination fires 5+ times from different IPs/duids | 30d |

**New model: `BotSignature`**
- `sig_type` — ip, subnet_24, subnet_16, user_agent, fingerprint, signal_set
- `value` — the actual string: CIDR notation, UA string, fingerprint hash, signal_set hash
- `source` — `auto` (from learner) or `manual` (admin-entered)
- `confidence` — 0–100, based on hit count and signal strength
- `hit_count` — how many times this signature has matched at pre-screen
- `block_count` — how many confirmed blocks generated or reinforced this signature
- `first_seen`, `last_seen`
- `expires_at` — null = permanent (manual only); auto entries always have TTL
- `is_active`
- `notes` — admin-only field for manual entries

**How pre-screen uses it:**

Redis holds a cache of all active signatures (refreshed async on create/update). Pre-screen
checks Redis before running any scoring:

```
Request → Check Redis signature cache
              ↓
  IP in blocked subnet?  → immediate decoy (no scoring)
  UA in blocked UA list? → immediate decoy
  fingerprint blocked?   → immediate decoy
              ↓
  Run full pre-screen scoring (existing flow)
```

An IP from a blocked /24 never reaches the scorer. This keeps pre-screen fast even as
the signature set grows.

**`BotLearner` service** — runs async after every high-confidence block:
1. Updates `BouncerDevice.risk_tier = 'blocked'`
2. Subnet escalation: count recent blocks per /24 and /16 from Redis counters; create
   `BotSignature` if threshold crossed
3. UA escalation: count recent blocks by exact UA string; create/update signature if
   threshold crossed
4. Fingerprint escalation: count total blocks per fingerprint_id across all duids
5. Signal-set campaign detection: `hash(sorted(triggered_signals))` = campaign_id; if
   this campaign_id fires from 3+ different /24 subnets → elevated incident (coordinated
   attack using shared tooling)
6. Write new/updated signatures to Redis cache
7. Fire incidents (see below)

**Campaign detection:**
`signal_set` signatures fingerprint the *attack tooling*, not the source IP. When the
same combination fires from multiple different IPs and duids, it's a coordinated campaign
with shared evasion code. Future requests that match even a subset of a known campaign's
signals get their base score elevated before any other analyzer runs.

**Conservative by design:**
- Auto-registered signatures always have TTL; manual-promoted signatures can be permanent
- A real user from a flagged /24 is not hard-blocked — their initial score is elevated but
  they can still pass by clearing the challenge
- Only `risk_tier = blocked` at device/fingerprint level is a hard block
- `BOUNCER_LEARN_ENABLED = True` — can be disabled without removing the feature

**Settings:**
```python
BOUNCER_LEARN_ENABLED = True
BOUNCER_LEARN_MIN_SCORE = 80          # minimum score to learn from (not borderline cases)
BOUNCER_LEARN_SUBNET_THRESHOLD = 5    # blocks per /24 per hour to flag subnet
BOUNCER_LEARN_UA_THRESHOLD = 5        # blocks per exact UA per hour to flag UA
BOUNCER_LEARN_FP_THRESHOLD = 3        # blocks per fingerprint to register it
BOUNCER_LEARN_CAMPAIGN_THRESHOLD = 5  # cross-IP signal_set matches to detect campaign
BOUNCER_LEARN_SUBNET_TTL = 86400      # 24h auto-block TTL for subnets
BOUNCER_LEARN_UA_TTL = 604800         # 7d auto-block TTL for UAs
BOUNCER_LEARN_SIGNAL_SET_TTL = 2592000  # 30d campaign signature TTL
```

### Incident Integration

Every block, high-risk assessment, and honeypot hit should fire an incident via
the existing django-mojo incident system:
- Category: `security:bouncer`
- Level: 3 (warning) for monitor, 4 (high) for block
- Level: 5 (critical) for subnet auto-block or campaign detection
- Metadata: `duid`, `ip`, `risk_score`, `triggered_signals`, `page_type`,
  `sig_type` (if a known signature matched), `campaign_id` (if signal_set matched)

Repeated blocks from a CIDR range escalate automatically through `BotLearner` — the
incident system is notified at each escalation tier (device blocked → subnet flagged →
campaign detected).

### Challenge Page Branding

The challenge page and all bouncer UI carry **MojoVerify branding** — this is intentional.
Even though the bouncer lives in django-mojo, it is part of the native mojo product family
and the branded experience is desirable across all projects using it.

**Visual identity (from `mojo-bouncer.js` — `.mbg-*` styles, dark version):**
- Background: `linear-gradient(145deg, #0a0e1a 0%, #121830 40%, #1a1040 100%)`
- Primary: `#6384ff` (indigo) — borders, button accents, progress bar start
- Accent: `#a78bfa` (purple) — progress bar end, hover states
- Logo: `https://mojoverify.com/logo.svg` at 72px with `drop-shadow(0 0 20px rgba(99,132,255,.3))`
- Pulse rings: two concentric circles animating with `mbg-pulse` keyframes
- Wordmark: "MOJO VERIFY" — system font stack, 600 weight, 3.5px letter-spacing, uppercase,
  `rgba(255,255,255,.7)`
- Animated scan line across full width (`mbg-scan` / `mbg-scanline` animation)
- Progress bar: 180px wide, 2px tall, gradient `#6384ff → #a78bfa`
- Status text: `rgba(255,255,255,.35)`, 11px, 0.5px letter-spacing
- Challenge button: 56px circle, indigo border + background, double-pulse halo rings,
  bullseye SVG icon
- CSS prefix: `.mbg-*` throughout — keep consistent with `mojo-bouncer.js`

**The light `.mojo-bouncer-overlay` CSS is legacy — discard.**

**4 layout variants** — all carry identical branding, different DOM structure to break
automation selectors. Class names get a per-render nonce suffix (e.g. `.mbg-card-a3f9c2`):

| Variant | Structure |
|---------|-----------|
| 0 | Centered card on dark bg — logo top, wordmark, progress, challenge below |
| 1 | No card — elements float directly on dark bg, logo inline left of wordmark |
| 2 | Two-column split — branding panel left, challenge area right |
| 3 | Top bar (logo + wordmark compressed), challenge widget fills main area below |

All four variants reference `https://mojoverify.com/logo.svg` and "MOJO VERIFY" wordmark.
Projects cannot override the branding on the challenge page — it is intentionally fixed.

### What mojo-bouncer.js Becomes

In this architecture, the JS is no longer the gate. It becomes:
- A passive behavioral signal collector (mouse, keyboard, timing, environment)
- A device fingerprinting library
- A challenge renderer (when Django decides a challenge is needed)
- A token courier (attaches token to auth API calls)

The JS should be served from a path that is not predictable/guessable by bots
(e.g., a versioned hash path like `/static/js/mbg.a3f9c2.js`) so simply
blocking the script URL is harder. Django renders the correct path into the
template at serve time.

### Future: Cloudflare Turnstile Integration

The architecture is designed to layer Cloudflare Turnstile on top without
restructuring. When added:
- Cloudflare Turnstile runs as an additional challenge step after Phase 1 pre-screening
- A valid Turnstile token is a strong positive signal that reduces the risk score
- `BOUNCER_TURNSTILE_SECRET_KEY` setting gates the feature
- Absence of Turnstile does not break the flow — it is additive scoring only

---

## Acceptance Criteria

- [ ] `account.BounceDevice` model (pre-auth device reputation: duid, fingerprint_id,
      risk_tier, event_count, block_count, last_seen_ip, linked_duids, metadata)
- [ ] `account.BouncerSignal` model (audit log: device FK, session_id, stage, ip,
      page_type, signals, risk_score, decision, triggered_signals, geo_ip FK)
- [ ] `account.BotSignature` model (adaptive learning registry: sig_type, value,
      source, confidence, hit_count, block_count, expires_at, is_active, notes)
      with `RestMeta` VIEW_PERMS/SAVE_PERMS so the operator portal gets full CRUD
      (no Django admin — all models managed via REST API as per project convention)
- [ ] `@md.bouncer_gate()` decorator available for views — runs pre-screen and
      renders challenge page or full page accordingly
- [ ] `@md.requires_bouncer_token()` decorator available for API endpoints —
      validates signed token, rejects 403 if missing/invalid when configured
- [ ] Pass cookie is HttpOnly, Secure, SameSite=Strict, HMAC-signed, short-lived
- [ ] Bouncer token is IP-bound, device-bound, single-use (Redis nonce), scoped to
      page type, short-lived
- [ ] Fail-open on infrastructure failure (bouncer down → serve page, log incident);
      fail-closed on explicit bad/missing token
- [ ] Score weights and thresholds are settings-driven with per-group overrides
- [ ] Every block fires an incident via the django-mojo incident system
- [ ] `BOUNCER_REQUIRE_TOKEN` defaults to False (log-only mode) for safe rollout
- [ ] `mojo-bouncer.js` served at a versioned/hashed path rendered into the template
- [ ] Challenge page contains no login form fields and no auth endpoint URLs
- [ ] Challenge page renders with a signed render context (layout, css_nonce, hp_field,
      btn_seed, label_idx, expires_at) — different every render
- [ ] CSS class names are nonce-suffixed per render — no stable selectors across sessions
- [ ] Honeypot field name randomized per render — filling it is high-confidence bot signal
- [ ] `POST /account/bouncer/challenge` validates render context signature, TTL, and
      honeypot field before scoring
- [ ] Decoy endpoint returns plausible-looking errors with realistic delay for blocked
      traffic — does not reveal that detection fired
- [ ] Settings-driven rate limits per layer (IP, username, duid)
- [ ] `duid` recorded on every bouncer API call (assess, submit, event) and on every
      auth call that includes it — even pre-token calls update device reputation
- [ ] IP drift detection: flag when a known duid appears from a new IP class (residential
      → datacenter, different country) and factor into risk score
- [ ] duid velocity check: assess calls per duid per hour fed into scoring
- [ ] Missing duid treated as `unknown` tier, never as a bypass — all signals still run
- [ ] Cloudflare Turnstile integration point stubbed and documented even if not
      implemented in v1
- [ ] `BotLearner` service fires async after every high-confidence block; updates
      `BouncerDevice`, checks subnet/UA/fingerprint/campaign escalation thresholds,
      writes `BotSignature` entries, refreshes Redis signature cache
- [ ] Pre-screen checks Redis signature cache (subnet, UA, fingerprint) before running
      scoring — matched signatures bypass scorer entirely and serve decoy immediately
- [ ] Campaign detection: same `signal_set` hash from 3+ distinct /24 subnets → fire
      critical incident with campaign_id metadata
- [ ] `BOUNCER_LEARN_ENABLED` setting gates all learning (default True)
- [ ] Auto-registered signatures always have TTL; manual signatures can be permanent
- [ ] Full test coverage: known-bot signals → block, known-good cookie → pass,
      token replay → reject, IP mismatch → reject, expired token → reject,
      new duid from datacenter IP → elevated initial score,
      subnet auto-escalation after threshold hits,
      known signature match → immediate decoy at pre-screen

## Constraints

- Must not break existing django-mojo login flow for projects not using bouncer
  (all bouncer features are opt-in via settings)
- Redis is required and always available (clustered with redundancy) — token
  nonces, rate limit counters, and device reputation cache all use Redis directly.
  No DB fallback needed.
- GeoIP data source must be configurable (`GEOIP_PROVIDER` setting) — projects
  may bring their own or use the built-in django-mojo GeoIP
- `mojo-bouncer.js` must remain a single self-contained file with no build step
  (vanilla JS, no dependencies) so it can be served as a static asset or inlined
- The login and challenge pages must be themeable via `BOUNCER_LOGO_URL` and
  `BOUNCER_ACCENT_COLOR` settings without forking the template
- Projects override the template by placing `account/login.html` in their own
  `TEMPLATES` dirs — standard Django template override pattern, no forking required
- The login page is the mojo-auth webapp (vanilla JS SPA), not a Django form — no
  Django form processing, all auth via REST calls from `mojo-auth.js`
- No Python type hints (django-mojo convention)
- Django 4.2+ compatible

## Future Vision

The login gate is v1. The bouncer should grow into a **platform-wide trust layer**,
not a login-only feature. Below are the directions to design toward even if not
built in v1 — the architecture should not preclude them.

### Any Public Submission

The `@md.bouncer_gate()` decorator should work on any view, not just login:

- Contact forms, quote requests, lead capture
- Password reset request (high-value target for enumeration attacks)
- Registration (bot signups for spam/abuse)
- Public API endpoints

The risk profile differs per surface — a contact form tolerates more friction than
a login, a registration is a higher-value target than a contact form. Each gets its
own threshold profile via `page_type` and `BOUNCER_THRESHOLDS_OVERRIDES`.

### Post-Login Continuous Monitoring

Once a user is authenticated, trust should not be static. The bouncer should
support a **session risk score** that evolves throughout the session:

**Signals to monitor post-login:**
- Device/IP drift — user logged in from London, now requests are coming from
  Frankfurt with a different fingerprint (session hijack indicator)
- Behavioral anomaly — sudden shift from human-paced interaction to
  machine-paced (account takeover, session being scripted)
- Rapid sequential actions — scraping patterns, bulk form submissions,
  automated data extraction
- Impossible travel — two authenticated requests from geographically
  impossible locations within a short window
- Headless browser signals arriving mid-session (started human, switched to bot)

**Response options (risk-based step-up):**
- Low anomaly → log and monitor, no user impact
- Medium anomaly → silently flag session, increase logging verbosity
- High anomaly → require step-up authentication (re-enter password or MFA)
- Confirmed compromise → terminate session, notify user, lock account pending review

This maps naturally onto the existing incident system — each anomaly fires an
incident which can trigger automated or manual response.

### Authenticated API Monitoring

For programmatic API consumers (API key holders), the bouncer shifts from
"is this a human" to "is this behaving like an authorized integration":

- Request rate patterns (burst vs sustained)
- Endpoint access patterns (accessing endpoints their integration shouldn't need)
- Data volume anomalies (downloading far more than normal)
- Credential stuffing via API (testing username/password combos through the API)

API keys would accumulate a reputation score just like devices do.
Anomalous API keys get rate-throttled before being blocked — sudden throttling
alerts legitimate operators without immediately breaking their integration.

### Trust Score as a Platform Primitive

Long-term, every request in the system should carry a trust context:

```python
request.trust_score    # 0–100, lower is more trusted
request.trust_signals  # list of triggered signal names
request.device         # account.Device instance
request.risk_tier      # unknown / low / medium / high / blocked
```

Any view or endpoint can inspect these and make its own risk decision —
the bouncer provides the data, the application decides what to do with it.
This enables fine-grained decisions like: "this endpoint requires
risk_tier <= 'medium'" without a full challenge flow.

## Notes

- Reference implementation: `apps/mojoverify/bouncer/` in mojo-verify. Port the
  scoring engine, token manager, and device session logic directly — do not redesign
  what works.
- The `mojo-bouncer.js` already implements: `EnvironmentScanner`, `BehaviorWatcher`,
  `MouseAnalyzer`, `FormWatcher`, `FingerprintCollector`, `OverlayController`,
  moving-target challenge. Port as-is, add versioned serving.
- New environment signals already added to the JS (to be ported):
  `playwright_artifacts`, `puppeteer_artifacts`, `outer_size_zero`,
  `connection_missing`, `device_memory_missing`, `document_focus_never`
- `bouncer_token` and `duid` are already being sent in `mojo-auth.js` via `_withDevice()`
  helper on: login, passkey complete, OAuth complete, magic link, and password reset calls.
  The backend just needs to start reading and validating them.
- `mojo-bouncer.js` already passes `duid` in all three of its own API call sites:
  assess payload, submit payload, and `EventReporter.report()`. No JS changes needed.
- `duid` storage: canonical `localStorage('mojo_device_uid')` (shared with `Rest.js`),
  legacy `localStorage('mojo_duid')` (auto-migrated), cookie `mojo_device_uid` (fallback).
  Backend should read the duid from the request body — it is client-supplied and
  corroborated by device reputation, not trusted on its own.
- Cloudflare Turnstile docs: https://developers.cloudflare.com/turnstile/

---

## Resolution
**Status**: Resolved — 2026-03-22

**Files changed**:
- `mojo/apps/account/models/bouncer_device.py` — new `BouncerDevice` model
- `mojo/apps/account/models/bouncer_signal.py` — new `BouncerSignal` audit log model
- `mojo/apps/account/models/bot_signature.py` — new `BotSignature` adaptive learning model
- `mojo/apps/account/models/__init__.py` — export all three models
- `mojo/apps/account/services/bouncer/__init__.py` — service package
- `mojo/apps/account/services/bouncer/environment.py` — `EnvironmentService` header/geo analysis
- `mojo/apps/account/services/bouncer/token_manager.py` — `TokenManager` HMAC token issue/validate/consume
- `mojo/apps/account/services/bouncer/scoring.py` — `RiskScorer`, pluggable `register_analyzer`, 7 built-in analyzers
- `mojo/apps/account/services/bouncer/learner.py` — `BotLearner`, `check_signature_cache`, `refresh_sig_cache`
- `mojo/apps/account/rest/bouncer/__init__.py` — REST package
- `mojo/apps/account/rest/bouncer/assess.py` — `POST /api/account/bouncer/assess`, pass cookie
- `mojo/apps/account/rest/bouncer/event.py` — `POST /api/account/bouncer/event`
- `mojo/apps/account/rest/bouncer/views.py` — login/challenge/decoy page views, decoy POST dead endpoint
- `mojo/apps/account/rest/bouncer_admin.py` — REST CRUD for BouncerDevice, BouncerSignal, BotSignature
- `mojo/apps/account/rest/__init__.py` — include bouncer REST
- `mojo/apps/account/asyncjobs.py` — `refresh_bouncer_sig_cache` scheduled job
- `mojo/decorators/bouncer.py` — `requires_bouncer_token(page_type)` decorator
- `mojo/decorators/__init__.py` — export `requires_bouncer_token`
- `mojo/apps/account/rest/user.py` — `@md.requires_bouncer_token('login')` on `on_user_login`
- `mojo/apps/account/templates/account/bouncer_challenge.html` — randomized 4-layout challenge page
- `mojo/apps/account/templates/account/bouncer_decoy.html` — honeypot decoy page
- `mojo/apps/account/templates/account/login.html` — mojo-auth SPA wrapper
- `mojo/apps/account/static/account/mojo-auth.js` — authentication webapp
- `mojo/apps/account/static/account/mojo-auth.css` — auth stylesheet

**Tests**: `tests/test_accounts/bouncer.py` — 14 tests covering: clean assess → allow + token, headless bot → block, honeypot → elevated score, BouncerDevice created, BouncerSignal logged, token single-use nonce, token IP mismatch, token expiry, token page_type scope, BotLearner skip on low score, sig cache clean IP, requires_bouncer_token log-only, requires_bouncer_token enforce 403, decoy dead endpoint 401.

**Docs**: `docs/django_developer/account/bouncer.md`, `docs/web_developer/account/bouncer.md`

**Validation**: Run `bin/testit.py tests/test_accounts/bouncer.py`
