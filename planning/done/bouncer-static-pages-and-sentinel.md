# Bouncer Expansion — Static-Page Gating + Continuous In-Session Sentinel

**Type**: request
**Status**: planned
**Date**: 2026-05-19
**Priority**: medium

## Description

Expand the existing django-mojo bouncer from a login-only gate into a layered bot-detection system that also protects (a) static webpages served from non-Django origins (e.g. nginx on EC2) and (b) authenticated in-session activity (e.g. gameplay, scraping-prone APIs). Both extensions reuse the existing `BouncerDevice`, `BouncerSignal`, scoring plugin system, and event endpoint — no parallel infrastructure.

Three deliverables:

1. **Move + modernize `mojo-bouncer.js`** from the legacy `mverify_api` location into `mojo/apps/account/static/account/`. Point it at the django-mojo bouncer REST endpoints, drop the apikey/multi-tenant auth header, drop the legacy two-stage submit handshake, and serve it via the existing static endpoint pattern.
2. **Add `verify_pass` endpoint + nginx `auth_request` recipe** so static webpages can be gated through nginx using the same `mbp` pass cookie the Django bouncer already sets. Missing/invalid cookie redirects to the Django bouncer challenge, then back.
3. **Add `mojo-sentinel.js` + streaming scorer + gradient enforcement** — a separate, lightweight (~5KB) client that does in-session telemetry only (no gate UI, no fingerprinting), feeding `/api/account/bouncer/event`. Server-side adds a sliding-window analyzer system (`@register_stream_analyzer`) parallel to the existing one-shot `@register_analyzer`, a session-risk rolling score in Redis, and a gradient enforcement helper that maps score bands to `BouncerDevice.risk_tier` + per-user flags.

## Context

The current bouncer gates the Django-rendered auth/contact pages effectively. Two gaps remain:

- **Static pages** (marketing sites, kiosks, third-party content hosted on nginx) have no path to the bouncer today. The legacy `mojo-bouncer.js` in `mverify_api/apps/mojoverify/bouncer/static/js/` was built for this but was never migrated when bouncer moved into django-mojo, and its endpoints (`/api/bouncer/*`) and apikey auth no longer match the django-mojo contract.
- **Post-login bot activity** — a user can pass the gate, then run a macro/automation. The gate sees ~3 seconds of behavior at login; gameplay or repeated API hits run for hours. The model + endpoint primitives for continuous detection exist (`BouncerSignal.stage='event'`, `POST /api/account/bouncer/event`, `BouncerDevice.risk_tier`) but no streaming scorer, no enforcement gradient, and no lightweight client to feed them.

Gaming platforms built on django-mojo (e.g. wmx_api) are the proximate consumer of #3. Companion request in that repo will register game-domain analyzers against the framework primitives this request adds.

Trust order followed: framework owns the primitives (transport, identity, scoring engine, universal analyzers, enforcement gradient). Consumer apps own domain analyzers and the meaning of enforcement flags.

## Acceptance Criteria

### A) Move + modernize `mojo-bouncer.js`

- [ ] Copy `mojo-bouncer.js` and `mojo-bouncer.css` from `/Users/ians/Projects/mojo/mojo-verify/mverify_api/apps/mojoverify/bouncer/static/` into `mojo/apps/account/static/account/`.
- [ ] Default `gateUrl` updated to `/api/account/bouncer/assess` (was `/api/bouncer/assess`).
- [ ] Default `eventUrl` updated to `/api/account/bouncer/event` (was `/api/bouncer/event`).
- [ ] `submitUrl` and the entire submit-stage call removed. Token returned by `assess` is the only validation handshake; downstream auth/action endpoints validate via `@md.requires_bouncer_token`.
- [ ] Remove the `Authorization: apikey <key>` header from all outbound fetches. The django-mojo bouncer endpoints are `@md.public_endpoint` and rate-limited; no apikey is required or accepted.
- [ ] Remove `data-api-key` attribute parsing. The auto-init block uses `data-api-base` (cross-origin embed) and `data-page-type` (login | registration | password_reset | public_message | embed). When `data-api-base` is missing, fall back to same-origin (current default behavior).
- [ ] Add `credentials: 'include'` to every fetch so the `mbp` HttpOnly pass cookie is set cross-origin.
- [ ] DuidManager keeps the existing `mojo_device_uid` localStorage key so identity stitches with `mojo-auth.js` and the Django challenge page.
- [ ] Add `static.py` endpoints to serve the new files alongside `mojo-auth.*`:
  - `GET /account/static/mojo-bouncer.js`
  - `GET /account/static/mojo-bouncer.css`
- [ ] Bump the JS file header `@version` to `2.0.0` and add a top-comment block stating: "Embeds in any page (Django or static). Defaults to same-origin; set `data-api-base` for cross-origin embedding."
- [ ] Delete the legacy bouncer code in `mverify_api/apps/mojoverify/bouncer/` (`models/`, `services/`, `rest/`, `static/`, `migrations/`). Verified out of scope for this repo — call it out in the request's `Out of Scope` note so the deletion is tracked separately in mverify_api.

### B) CORS for cross-origin bouncer use

- [ ] Wrap the bouncer endpoints (`/api/account/bouncer/assess`, `/event`, `/message`, plus the new `/verify_pass` below) with a `BOUNCER_ALLOWED_ORIGINS` allowlist. Default empty (same-origin only). When set, respond with the matching `Access-Control-Allow-Origin`, `Access-Control-Allow-Credentials: true`, `Access-Control-Allow-Headers: Content-Type`, `Vary: Origin`.
- [ ] Handle `OPTIONS` preflight returning `204 No Content` with the same headers.
- [ ] Origin not on the list → omit CORS headers entirely (browser blocks; no leakage). Do not 403 — preserve fail-open posture for in-domain calls.
- [ ] Settings doc entry: `BOUNCER_ALLOWED_ORIGINS = ['https://marketing.example.com', 'https://kiosk.example.com']`.

### C) nginx `auth_request` gate — `verify_pass` endpoint

- [ ] New endpoint `GET /api/account/bouncer/verify_pass` in `mojo/apps/account/rest/bouncer/assess.py` (or split file if it grows). Reads `mbp` cookie via the existing `verify_pass_cookie(cookie_value, ip)`. Returns `200` if valid, `401` otherwise.
- [ ] No body in either response (nginx `auth_request` discards the body). Headers only.
- [ ] On 200, set response header `X-Bouncer-Muid: <muid>` so the upstream can log/correlate.
- [ ] Endpoint is `@md.public_endpoint` and rate-limited (`@md.rate_limit('bouncer_verify_pass', ip_limit=600)` — higher than assess because this fires on every static page hit until the cookie short-circuits the redirect).
- [ ] Add an nginx recipe to `docs/web_developer/account/bouncer.md` showing:
  - `location /_bouncer_check { internal; proxy_pass https://auth.example.com/api/account/bouncer/verify_pass; ... }`
  - `auth_request /_bouncer_check;` on protected static locations
  - `error_page 401 = @bouncer_redirect;` + a `return 302 https://auth.example.com/auth?redirect=$scheme://$host$request_uri;` block
- [ ] Add the same recipe summary to `docs/django_developer/account/bouncer.md` under a new "Static-Page Gating" section.

### D) `mojo-sentinel.js` (new file)

- [ ] New file `mojo/apps/account/static/account/mojo-sentinel.js`. Hard target: under 5KB minified-ish (no minifier; just keep it small by hand — current `mojo-bouncer.js` is 1169 lines, sentinel should be a fraction).
- [ ] Reuses the same `DuidManager` storage key (`mojo_device_uid`). Do NOT copy the full DuidManager — read the existing localStorage value; generate-and-persist only if missing, using the same UUID format.
- [ ] No UI. No overlay. No fingerprinting. No gate logic. Pure telemetry.
- [ ] Passive auto-collected signals (sentinel runs these on its own without app integration):
  - `visibility_transitions` — count of `document.visibilitychange` fires
  - `focus_blur_count` — count of `window` focus/blur events
  - `paste_events` — count of `paste` events on inputs (not contents — just count + target tag)
  - `devtools_open_heuristic` — `window.outerWidth - window.innerWidth > 160` heuristic, sampled
  - `click_coord_set_size` — distinct (x,y) click coordinates rounded to 8px buckets, reported as the size of the set since last batch
  - `inter_action_interval_ms` — array of intervals between user-initiated events
  - `page_lifetime_ms` — wall-clock since first event
  - `idle_gaps_count` — count of idle periods > 60s
- [ ] Public API: `MojoSentinel.observe(category, payload)` — pushes a custom event into the outbound batch. `category` is a short string the app chooses (e.g. `'game_action'`, `'api_call'`). `payload` is a flat object of primitives.
- [ ] Batching: events are buffered and flushed every `data-flush-interval-ms` (default 15000) OR when the buffer hits `data-flush-size` (default 25), whichever comes first. Final flush on `pagehide` via `navigator.sendBeacon`.
- [ ] Outbound: `POST /api/account/bouncer/event` with body `{duid, msid?, page_type, events: [...]}`. `credentials: 'include'`. Fail-silent.
- [ ] Auto-init pattern matches `mojo-bouncer.js`:
  ```html
  <script src="https://auth.example.com/account/static/mojo-sentinel.js"
          data-api-base="https://auth.example.com"
          data-page-type="gameplay"
          data-context="lobby"
          defer></script>
  ```
- [ ] `data-page-type` becomes the `page_type` field on every outbound event (so server-side analyzers can scope to e.g. `gameplay`).
- [ ] `data-context` is a free-form string passed through on every event for app-side correlation.
- [ ] Add static-serving endpoint `GET /account/static/mojo-sentinel.js` to `static.py`.

### E) Server-side: `BaseStreamAnalyzer` + `SessionRiskScorer`

- [ ] New module `mojo/apps/account/services/bouncer/stream_scoring.py` with:
  - `BaseStreamAnalyzer` class — `name` class attr, `analyze(muid, signal_window, device) -> (score_delta, triggered_signals)`. `signal_window` is an iterable of recent `BouncerSignal` rows for that muid.
  - `register_stream_analyzer` decorator + `_STREAM_REGISTRY` dict, mirroring the existing `register_analyzer` / `_REGISTRY` pattern in `scoring.py`.
  - `SessionRiskScorer.score(muid, window_seconds=3600)` — runs all registered stream analyzers, accumulates `score_delta`, updates the rolling Redis score, returns `(new_score, triggered, decision)`.
- [ ] Redis key: `bouncer:session_risk:{muid}`. Value: integer 0–100. TTL: `BOUNCER_SESSION_RISK_TTL` (default 86400). Read/write via the existing Redis client pattern in `learner.py`.
- [ ] `POST /api/account/bouncer/event` (existing) updated to:
  - Accept the sentinel batched-event format (`events: [...]` array). Each event becomes one `BouncerSignal(stage='event')` row. Existing single-event payload still accepted.
  - After persisting events, enqueue an async job `mojo.apps.account.services.bouncer.stream_scoring.run_session_scorer` with `{muid: ...}`. Async so the assess-side latency isn't impacted.
- [ ] New async job `run_session_scorer(muid)` — fetches the last N `BouncerSignal` rows (`window_seconds`-bounded), calls `SessionRiskScorer.score`, applies gradient enforcement (see F).

### F) Gradient enforcement helper

- [ ] New module `mojo/apps/account/services/bouncer/enforcement.py` with:
  - `apply_session_response(device, risk_score, user=None)` — maps score bands to action flags. Default bands:
    | Score | Action | Effect |
    |---|---|---|
    | ≥ 90 | freeze | `BouncerDevice.risk_tier='blocked'`; if `user`, call configured freeze callback |
    | ≥ 70 | shadow_ban | set `user.set_protected_metadata('bouncer_shadow_banned', True)` |
    | ≥ 50 | require_step_up | set `user.set_protected_metadata('bouncer_require_step_up', True)` |
    | ≥ 30 | monitor | fire `incident.report_event('security:bouncer:session_suspect', level=6)` |
    | < 30 | noop | nothing |
  - Bands configurable via `BOUNCER_SESSION_BANDS` setting (dict).
  - Optional freeze callback: `BOUNCER_SESSION_FREEZE_HANDLER` — dotted path to a callable `(user, device, risk_score) -> None`. App provides; framework calls. Default None (framework just sets the device tier).
- [ ] Incident events fired by enforcement:
  - `security:bouncer:session_freeze` (level 9) on freeze
  - `security:bouncer:session_shadow_ban` (level 8) on shadow_ban
  - `security:bouncer:session_step_up` (level 6) on require_step_up
  - `security:bouncer:session_suspect` (level 6) on monitor
- [ ] All four categories get default `RuleSet.ensure_bouncer_rules()` entries (extend the existing rules-defaults function).

### G) Universal stream analyzers (ship with framework)

Six app-agnostic analyzers registered in `mojo/apps/account/services/bouncer/stream_analyzers.py`:

- [ ] `endurance_excessive` — session active > `BOUNCER_ENDURANCE_HOURS` (default 12) hours with no `idle_gap` event of ≥ 600s. Score delta +25.
- [ ] `tab_never_hidden` — session lifetime > 4 hours, `visibility_transitions` count == 0 across all events in the window. Score delta +20.
- [ ] `no_idle_period` — `idle_gaps_count` across the window == 0 over a window ≥ 4 hours. Score delta +15.
- [ ] `coordinate_quantization` — `click_coord_set_size` across the window < 5 distinct buckets despite > 100 reported clicks. Score delta +25.
- [ ] `action_interval_regular` — autocorrelation of `inter_action_interval_ms` (lag 1) > 0.9 over ≥ 50 intervals. Score delta +25. Implementation: simple Pearson correlation on `intervals[:-1]` vs `intervals[1:]` — no scipy.
- [ ] `paste_into_sensitive_field` — paste event count > 0 where `event.payload.target_tag == 'input[type=password]'`. Score delta +15.

Each analyzer must include a one-line `__doc__` explaining the heuristic and a unit test that asserts both the trigger condition AND the non-trigger condition.

### H) Tests

Tests follow `docs/django_developer/testit/Overview.md`. `@th.django_unit_test()`, `def test_xxx(opts)`, no mocks of server-side state (server is a separate process).

- [ ] `tests/test_bouncer/test_static_pages.py`:
  - `test_verify_pass_valid_cookie_returns_200` — set a valid mbp cookie via assess, call verify_pass, assert 200 + `X-Bouncer-Muid` header.
  - `test_verify_pass_missing_cookie_returns_401` — no cookie → 401.
  - `test_verify_pass_invalid_signature_returns_401` — tampered cookie → 401.
  - `test_verify_pass_expired_cookie_returns_401` — cookie issued beyond TTL → 401.
- [ ] `tests/test_bouncer/test_cors.py`:
  - Origin in `BOUNCER_ALLOWED_ORIGINS` → response has CORS headers + `Vary: Origin`.
  - Origin not in list → no CORS headers in response.
  - `OPTIONS` preflight returns 204 + headers.
- [ ] `tests/test_bouncer/test_sentinel_endpoint.py`:
  - Batched event payload (`events: [...]`) persists N `BouncerSignal` rows with `stage='event'`.
  - Single-event legacy payload still works.
  - Enqueues `run_session_scorer` job (assert via job queue inspection).
- [ ] `tests/test_bouncer/test_stream_scoring.py`:
  - `test_endurance_excessive_triggers` — fabricate a session of `BouncerSignal` rows spanning 13h with no idle gaps, run scorer, assert `endurance_excessive` in triggered + score delta applied.
  - `test_action_interval_regular_triggers` — fabricate signals with constant 1000ms intervals (autocorrelation 1.0), assert trigger.
  - `test_coordinate_quantization_triggers` — many click events all in 3 buckets, assert trigger.
  - `test_paste_into_password_field_triggers`.
  - `test_no_triggers_on_human_pattern` — randomized timing, multiple buckets, idle gaps → no analyzer triggers; score stays at 0.
- [ ] `tests/test_bouncer/test_enforcement.py`:
  - Score 95 → device tier blocked + freeze handler called (test handler).
  - Score 75 → shadow_ban metadata set on user.
  - Score 55 → require_step_up metadata set.
  - Score 35 → monitor incident fired, no metadata changed.
  - Score 10 → noop.
  - Bands respect `BOUNCER_SESSION_BANDS` override via `th.server_settings(...)`.

### I) Docs

- [ ] `docs/django_developer/account/bouncer.md` — new top-level section "Continuous Detection" covering:
  - The `stage='event'` flow
  - `BaseStreamAnalyzer` plugin pattern with the same template as the existing `BaseSignalAnalyzer` example
  - `SessionRiskScorer` and its Redis rolling-score model
  - Gradient enforcement table + how to register a freeze handler
  - The six universal analyzers and what they detect
- [ ] `docs/django_developer/account/bouncer.md` — new section "Static Page Gating" covering:
  - The `verify_pass` endpoint
  - nginx `auth_request` recipe (full snippet)
  - When to use the JS sentinel vs the nginx redirect-gate
  - `BOUNCER_ALLOWED_ORIGINS` CORS configuration
- [ ] `docs/web_developer/account/bouncer.md` — new section "Embedding on Static Pages":
  - The mojo-bouncer.js cross-origin embed snippet
  - The mojo-sentinel.js embed snippet
  - `MojoSentinel.observe(category, payload)` API contract
  - How identity (`mojo_device_uid`) stitches across both
- [ ] `CHANGELOG.md` entry: "Bouncer: cross-origin embed support, nginx auth_request endpoint, continuous in-session telemetry (mojo-sentinel.js + streaming scorer + gradient enforcement)."

## Endpoints

| Method | Path | Description | Permission |
|---|---|---|---|
| GET | `/account/static/mojo-bouncer.js` | Serve the modernized embeddable client | public |
| GET | `/account/static/mojo-bouncer.css` | Serve overlay stylesheet | public |
| GET | `/account/static/mojo-sentinel.js` | Serve the in-session telemetry client | public |
| GET | `/api/account/bouncer/verify_pass` | nginx auth_request endpoint — validates `mbp` cookie | public + rate-limited |
| OPTIONS | `/api/account/bouncer/*` | CORS preflight | public |
| POST | `/api/account/bouncer/event` | (existing — extended) accepts batched `events: [...]` payload | public + rate-limited |

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `BOUNCER_ALLOWED_ORIGINS` | `[]` | Origins permitted to call bouncer endpoints cross-origin |
| `BOUNCER_SESSION_RISK_TTL` | `86400` | TTL for the rolling Redis session-risk score |
| `BOUNCER_SESSION_BANDS` | (see §F) | Score → action band mapping |
| `BOUNCER_SESSION_FREEZE_HANDLER` | `None` | Dotted path to app-defined freeze callback |
| `BOUNCER_ENDURANCE_HOURS` | `12` | Threshold for `endurance_excessive` |

## Out of Scope

- Deleting the legacy `mojo-verify/mverify_api/apps/mojoverify/bouncer/` directory — that cleanup belongs to the mverify_api repo and will be tracked there as a separate request once this lands.
- Game-domain stream analyzers (reaction time, accuracy percentiles, action-grammar replay, win-rate anomalies) — those are app-specific and live in consumer apps (wmx_api, etc.) via `@register_stream_analyzer`.
- Defining what shadow-ban / freeze / step-up mean in a specific app — the framework sets the metadata flag and fires the incident; the app reads the flag and acts on it.
- Minifier / bundler for the JS. Files ship as hand-readable source.
- ML-based anomaly detection. v1 uses simple thresholds and registered analyzers. Population baselines and per-user baselining are future work.
- Server-truth event writes from app backends — apps write `BouncerSignal(stage='event')` directly using the existing model; no framework wrapper needed for v1.

## Plan

**Status**: planned
**Planned**: 2026-05-19

### Objective

Add continuous in-session bot detection (mojo-sentinel.js + inline streaming scorer + gradient enforcement) and unlock cross-origin / static-page bouncer use (modernized mojo-bouncer.js + verify_pass endpoint + credentialed CORS) — all reusing the existing BouncerDevice / BouncerSignal / event-endpoint primitives. Real consumer day-one: wmx_test_client (separate-origin SvelteKit SPA against wmx_api, nginx-routed at playground.wmwx.io).

### Design Decisions

- **Inline scoring, no jobs queue.** `score_session(muid)` is a synchronous helper called from the event endpoint and from app backends after they write `BouncerSignal` rows. Scoring is ~10–50ms (indexed query + 6 analyzers in memory). Nobody waits on it user-facing (sentinel is fire-and-forget; game backends absorb tens of ms inside webhook handlers). Async only adds latency to the enforcement decision and a queue dependency. Add `jobs.publish` later if real-world latency becomes a problem.
- **Monotonic high-water within TTL.** Redis `bouncer:session_risk:{muid}` only goes up; resets when key TTLs (default 24h). No flapping between enforcement bands.
- **Same-domain + subdomain only.** v1 nginx auth_request requires shared cookie domain (Shape A: same host, or Shape B: subdomains under shared parent via `BOUNCER_PASS_COOKIE_DOMAIN=.example.com`). Cross-domain (Shape C, separate eTLD+1) deferred — needs signed-token redirect dance, separate request.
- **CORS via surgical edit to existing middleware, not new module.** Modify `mojo/middleware/cors.py` to special-case bouncer paths with specific-origin + `Allow-Credentials: true` when origin is in `BOUNCER_ALLOWED_ORIGINS`. All other paths keep current `*` behavior. ~15 lines, no new helper.
- **Identity via existing cookies/localStorage only.** Sentinel reads `mbp` cookie + `mojo_device_uid` localStorage. Same-origin: identity stitches automatically. Cross-origin: best-effort via duid; documented as a known limit. No new identity endpoint.
- **Inline drop of devtools-open heuristic.** Width-diff heuristic produces too many false positives (docked devtools, external windows, multi-monitor DPI). Explicitly out of sentinel; documented "we don't try."
- **Merge endurance + no-idle into one parameterized analyzer.** Both detect "session that doesn't pause." One analyzer with severity scaled by duration (15 / 20 / 25 points for 4h / 8h / 12h continuous-no-idle).
- **Hardcode analyzer score deltas, no parallel weight-config setting.** Stream analyzers are 5 self-contained classes; weight tuning at this scale belongs in source, not settings. (Different from one-shot scoring which has dozens of signals.)
- **Backward-compat for /event single-event payload.** Legacy `{event_type, data}` still accepted; new batched `{events: [...]}` is the additive contract. Detection is presence of `events` key.
- **Enforcement bands are flag-setters + incident-firers, not actuators.** Framework sets `BouncerDevice.risk_tier` and metadata flags (`bouncer_shadow_banned`, `bouncer_require_step_up`); fires incidents. Optional `BOUNCER_SESSION_FREEZE_HANDLER` lets app define what freeze means in its data model. Framework never imports app code.
- **Bouncer-as-a-Service deployment.** Any django-mojo install already serves all bouncer assets + endpoints. With `BOUNCER_ALLOWED_ORIGINS` populated, the bouncer becomes a shared service for arbitrarily many consumer apps. Existing group-resolution (`Group.resolve_by_auth_domain` + `?group_uuid=`) handles per-consumer branding. v1 explicitly documents this as a first-class deployment pattern.
- **nginx drop-in via shared include file.** Ship `docs/web_developer/account/nginx/mojo-bouncer.conf` as a copy-and-include artifact. Consumer sites set two variables (`$mojo_bouncer_host`, `$mojo_bouncer_login`), include the file, then add `auth_request /_mojo_bouncer_check;` on any protected location. Pure nginx, no Lua/no module, no compile step.
- **verify_pass also pre-screens against the signature cache.** Reuse existing `check_signature_cache(ip, ua, fingerprint_id)` from `learner.py`. Known-bad signatures get 401 from verify_pass without the cookie check ever running — nginx then redirects them to the challenge (which itself serves the decoy). Effectively: signature-matched bots blocked at the edge across every protected location.

### Steps

#### 1. mojo-bouncer.js — move + modernize

1. `mojo/apps/account/static/account/mojo-bouncer.js` — copy from `/Users/ians/Projects/mojo/mojo-verify/mverify_api/apps/mojoverify/bouncer/static/js/mojo-bouncer.js`, then modify:
   - Default `gateUrl = '/api/account/bouncer/assess'` (was `/api/bouncer/assess`)
   - Default `eventUrl = '/api/account/bouncer/event'`
   - Remove `submitUrl`, the entire `submitAssessment` two-stage handshake, and `/submit`-related code paths
   - Remove `Authorization: apikey` header and the `apiKey` config field; remove `data-api-key` auto-init attribute parsing
   - Add `credentials: 'include'` to every `fetch` call
   - Add `data-api-base` attribute parsing for cross-origin embed (path = `{api_base}/api/account/bouncer/assess`); when missing, same-origin
   - Bump `@version` header to `2.0.0` with a top-comment block: "Embeds in any page (same- or cross-origin). Defaults to same-origin; set `data-api-base` for cross-origin."
2. `mojo/apps/account/static/account/mojo-bouncer.css` — copy as-is (63 lines, no changes).
3. `mojo/apps/account/rest/bouncer/static.py` — add two static-serving endpoints following the existing `_serve_static` pattern:
   - `@md.GET('account/static/mojo-bouncer.js')` → `_serve_static('mojo-bouncer.js')`
   - `@md.GET('account/static/mojo-bouncer.css')` → `_serve_static('mojo-bouncer.css')`

#### 2. CORS — credentialed origins for bouncer paths

4. `mojo/middleware/cors.py` — modify `__call__` so that when `request.path` starts with `/api/account/bouncer/` or `/account/static/mojo-` AND `Origin` header matches an entry in `settings.get_static('BOUNCER_ALLOWED_ORIGINS', [])`:
   - Set `Access-Control-Allow-Origin: <specific origin>` (not `*`)
   - Set `Access-Control-Allow-Credentials: true`
   - Set `Vary: Origin`
   - Otherwise keep existing `*` behavior unchanged
   - OPTIONS preflight path: detect bouncer-prefix + allowlisted Origin and return 204 with the credentialed headers.

#### 3. verify_pass endpoint + cookie domain

5. `mojo/apps/account/rest/bouncer/assess.py` — add endpoint:
   - `@md.GET('account/bouncer/verify_pass')`
   - `@md.public_endpoint("nginx auth_request — validates mbp pass cookie + signature cache")`
   - `@md.rate_limit('bouncer_verify_pass', ip_limit=600)`
   - **First**: call `check_signature_cache(request.ip, request.user_agent, '')` from `services/bouncer/learner.py`. If matched → return 401 with `X-Bouncer-Reason: signature` header. Known-bot subnets/UAs blocked at the edge before any application logic runs.
   - **Then**: read `mbp` cookie via existing `verify_pass_cookie(cookie_value, request.ip)` helper.
   - Returns 200 with `X-Bouncer-Muid: <muid>` header on cookie-valid, 401 (no body) on missing/invalid cookie or signature match.
   - Body is always empty (nginx `auth_request` discards it); diagnostic info is in headers only.
6. `mojo/apps/account/rest/bouncer/assess.py:_set_pass_cookie` (line 241) — add `domain` kwarg to `response.set_cookie` call when `BOUNCER_PASS_COOKIE_DOMAIN` setting is non-empty.
6b. **nginx drop-in include file** — `docs/web_developer/account/nginx/mojo-bouncer.conf` (new file, shipped as a documentation artifact, not loaded by Django):
   ```nginx
   # Shared bouncer plumbing — include once per server { } block.
   # Required vars: $mojo_bouncer_host, $mojo_bouncer_login

   location = /_mojo_bouncer_check {
       internal;
       proxy_pass         https://$mojo_bouncer_host/api/account/bouncer/verify_pass;
       proxy_set_header   Host         $mojo_bouncer_host;
       proxy_set_header   Cookie       $http_cookie;
       proxy_set_header   X-Real-IP    $remote_addr;
       proxy_set_header   User-Agent   $http_user_agent;
       proxy_pass_request_body off;
       proxy_set_header   Content-Length "";
   }

   location @mojo_bouncer_redirect {
       return 302 https://$mojo_bouncer_host$mojo_bouncer_login?redirect=$scheme://$host$request_uri;
   }
   ```
6c. **Example consumer config** — `docs/web_developer/account/nginx/example-protected-site.conf` (new file, documentation artifact):
   ```nginx
   server {
       listen 443 ssl;
       server_name app.example.com;

       set $mojo_bouncer_host  "auth.example.com";
       set $mojo_bouncer_login "/auth";
       include conf.d/mojo-bouncer.conf;

       # Protected static page — bouncer-gated
       location /vip/ {
           auth_request /_mojo_bouncer_check;
           error_page 401 = @mojo_bouncer_redirect;
           try_files $uri $uri/ /index.html;
       }

       # Unprotected — public marketing
       location / {
           try_files $uri $uri/ /index.html;
       }
   }
   ```

#### 4. mojo-sentinel.js — new lightweight telemetry client

7. `mojo/apps/account/static/account/mojo-sentinel.js` — new file, hand-keep under ~250 lines:
   - `DuidManager` reads from existing `mojo_device_uid` localStorage key (no copy of the bouncer.js DuidManager class — minimal inline read + generate-if-missing using same UUID v4 format)
   - Auto-collected signals (running with no app integration):
     - `visibility_transitions` — `document.addEventListener('visibilitychange', ...)` counter
     - `focus_blur_count` — `window.addEventListener('focus'/'blur', ...)` counter
     - `paste_events` — `document.addEventListener('paste', e => log target_tag)` accumulator
     - `click_coord_set_size` — Set of `(round(x/8), round(y/8))` strings from click events
     - `inter_action_interval_ms` — array of ms-deltas between any user interaction events
     - `page_lifetime_ms` — `Date.now() - bootMs`
     - `idle_gaps_count` — counter of gaps > 60s in the interval array
   - **NOT included:** devtools-open heuristic (explicit non-feature)
   - Public API: `MojoSentinel.observe(category, payload)` — pushes onto outbound buffer
   - Batched flushes: every 15000ms OR when buffer ≥ 25 events; final flush via `navigator.sendBeacon` on `pagehide`
   - Outbound: `POST {api_base}/api/account/bouncer/event` with `{duid, page_type, context, events: [...]}`; `credentials: 'include'`; fail-silent
   - Auto-init: `<script src="...mojo-sentinel.js" data-api-base="..." data-page-type="gameplay" data-context="game:slug" defer></script>`
8. `mojo/apps/account/rest/bouncer/static.py` — add `@md.GET('account/static/mojo-sentinel.js')` → `_serve_static('mojo-sentinel.js')`.

#### 5. /event endpoint — batched payload + inline scoring

9. `mojo/apps/account/rest/bouncer/event.py` — rewrite `on_bouncer_event`:
   - Detect batched format: `events = request.DATA.get('events')` — if truthy list, iterate; otherwise fall back to existing single-event handling for backward-compat.
   - For each event: persist one `BouncerSignal(stage='event', page_type=<from outer payload>, raw_signals={'event_type', 'data', 'context'})`. Use `bulk_create` for batches > 1.
   - After all events persisted, call `score_session(muid)` synchronously (from step 6 below). Wrap in try/except — scoring failure must not break the endpoint.
   - Existing single-event risk_action / incident-firing logic is preserved for backward-compat path.

#### 6. Streaming scorer + analyzer registry + universal analyzers

10. `mojo/apps/account/services/bouncer/stream_scoring.py` — new module:
    - `class BaseStreamAnalyzer` with classmethod `analyze(muid, signal_window, device) -> (score_delta:int, triggered:list[str])`
    - `_STREAM_REGISTRY = []` + `register_stream_analyzer` decorator (mirror of existing `register_analyzer` in `scoring.py`)
    - `score_session(muid, window_seconds=3600)` — function:
       1. Read `BouncerSignal.objects.filter(muid=muid, created__gte=cutoff).order_by('-created')[:1000]`
       2. Fetch `device = BouncerDevice.objects.filter(muid=muid).first()`
       3. Iterate `_STREAM_REGISTRY`, accumulate `score_delta` + triggered list (each analyzer wrapped in try/except — one bad analyzer can't break the run)
       4. Read current Redis score from `bouncer:session_risk:{muid}` (default 0)
       5. New score = `min(max(redis_score, redis_score + delta), 100)` — high-water within session, capped at 100
       6. Write back with TTL `BOUNCER_SESSION_RISK_TTL` (default 86400)
       7. Call `apply_session_response(device, new_score, user=device.user if device else None)` from enforcement module
       8. Return `(new_score, all_triggered)` for callers that want the result
11. `mojo/apps/account/services/bouncer/stream_analyzers.py` — new module, five analyzers registered:
    - `ExtendedSessionNoIdleAnalyzer` — read `idle_gaps_count` and `page_lifetime_ms` from raw_signals across the window. If lifetime > 4h AND no idle gaps > 600s observed in window: tiered score by duration (4h-8h: +15, 8h-12h: +20, 12h+: +25).
    - `TabNeverHiddenAnalyzer` — lifetime > 4h AND sum of `visibility_transitions` across window == 0: +20.
    - `CoordinateQuantizationAnalyzer` — total click count > 100 AND combined distinct `click_coord_set` size < 5: +25.
    - `ActionIntervalRegularAnalyzer` — concatenate `inter_action_interval_ms` arrays across window; if length ≥ 50, compute lag-1 autocorrelation (simple Pearson on `intervals[:-1]` vs `intervals[1:]`, no scipy) — if > 0.9: +25.
    - `PasteIntoSensitiveFieldAnalyzer` — scan event payloads for `paste_event` entries with `target_tag == 'input[type=password]'`. Any occurrence: +15.
    - Each analyzer's `__doc__` = one-line heuristic explanation.

#### 7. Gradient enforcement

12. `mojo/apps/account/services/bouncer/enforcement.py` — new module:
    - `apply_session_response(device, risk_score, user=None)` — maps score to band:
       - `≥ 90`: set `device.risk_tier = 'blocked'`, increment `device.block_count`; if `user`, lookup `BOUNCER_SESSION_FREEZE_HANDLER` dotted-path setting and call it with `(user, device, risk_score)`; fire `incident.report_event('security:bouncer:session_freeze', level=9, ...)`
       - `≥ 70`: if `user`, `user.set_protected_metadata('bouncer_shadow_banned', True)`; fire `security:bouncer:session_shadow_ban` (level 8)
       - `≥ 50`: if `user`, `user.set_protected_metadata('bouncer_require_step_up', True)`; fire `security:bouncer:session_step_up` (level 6)
       - `≥ 30`: fire `security:bouncer:session_suspect` (level 6); no flag changes
       - else: noop
    - Bands configurable via `BOUNCER_SESSION_BANDS` setting (dict with keys `freeze`, `shadow_ban`, `require_step_up`, `monitor`).
13. `mojo/apps/account/rest/bouncer/assess.py:_ensure_bouncer_defaults` — extend the bootstrap to ensure the four new incident rule categories (`security:bouncer:session_freeze`, `:session_shadow_ban`, `:session_step_up`, `:session_suspect`) are present in `RuleSet`. Reuse existing `RuleSet.ensure_bouncer_rules()` pattern — add the four new defaults to the same helper.

#### 8. Tests (testit; @th.django_unit_test()) — separate server process

14. `tests/test_security/bouncer_verify_pass.py` — new test module:
    - `test_verify_pass_valid_cookie_returns_200` — assess clean request → use returned cookie on verify_pass → 200 + X-Bouncer-Muid header
    - `test_verify_pass_missing_cookie_returns_401`
    - `test_verify_pass_invalid_signature_returns_401`
    - `test_verify_pass_expired_cookie_returns_401` — via `th.server_settings(BOUNCER_PASS_COOKIE_TTL=1)` + sleep
    - `test_verify_pass_signature_match_returns_401_with_reason_header` — seed a BotSignature for the test IP, refresh_sig_cache(), call verify_pass even with a valid cookie → 401 with `X-Bouncer-Reason: signature`
    - `test_pass_cookie_domain_attribute` — `th.server_settings(BOUNCER_PASS_COOKIE_DOMAIN='.example.com')` → cookie set with domain
15. `tests/test_security/bouncer_cors.py`:
    - `test_bouncer_path_with_allowlisted_origin_returns_credentialed_cors`
    - `test_bouncer_path_with_non_allowlisted_origin_returns_wildcard`
    - `test_options_preflight_for_allowlisted_returns_204_credentialed`
    - `test_non_bouncer_path_unchanged` — regression: existing `*` behavior preserved
16. `tests/test_security/bouncer_sentinel_endpoint.py`:
    - `test_batched_events_persist_N_signal_rows` — POST `{events: [3 events]}` → 3 BouncerSignal rows with stage='event'
    - `test_single_event_legacy_format_still_works` — backward-compat
    - `test_batched_events_trigger_scorer_inline` — after POST, `bouncer:session_risk:{muid}` is set in Redis
    - `test_scorer_failure_does_not_break_endpoint` — patch scorer to raise → endpoint still returns 200
17. `tests/test_security/bouncer_stream_scoring.py`:
    - `test_extended_session_no_idle_triggers_at_4h` (+15), `at_8h` (+20), `at_12h` (+25) — seed BouncerSignal rows with lifetime + zero idle gaps
    - `test_tab_never_hidden_triggers`
    - `test_coordinate_quantization_triggers`
    - `test_action_interval_regular_triggers`
    - `test_paste_into_password_field_triggers`
    - `test_human_pattern_no_triggers` — varied timing, multiple buckets, idle gaps → score 0
    - `test_high_water_monotonic` — score 50, then a quiet scorer run with empty window → still 50 (no decrease)
    - `test_score_capped_at_100` — analyzer deltas totaling > 100 cap at 100
    - `test_score_persists_within_ttl_resets_after`
18. `tests/test_security/bouncer_enforcement.py`:
    - `test_band_freeze_calls_handler_and_sets_blocked` — use `th.server_settings(BOUNCER_SESSION_FREEZE_HANDLER='...')` pointing at a test-side handler module
    - `test_band_shadow_ban_sets_metadata`
    - `test_band_step_up_sets_metadata`
    - `test_band_monitor_fires_incident_no_metadata`
    - `test_band_below_threshold_noops`
    - `test_custom_bands_setting_respected`
    - `test_freeze_handler_failure_does_not_break_scoring` — handler raises → score still saved, incident still fires

Existing test helper to follow (per `.claude/rules/testing.md`): `@th.django_unit_test()`, `def test_xxx(opts)`, setup deletes records before creating them. Run via `bin/run_tests --agent -t test_security.bouncer_<module>`.

#### 9. Docs

19. `docs/django_developer/account/bouncer.md` — add four new top-level sections:
    - "Continuous Detection" — `BaseStreamAnalyzer` plugin pattern (mirror the existing `BaseSignalAnalyzer` example), `score_session()` semantics, the 5 universal analyzers, Redis rolling-score model, gradient enforcement table, `BOUNCER_SESSION_FREEZE_HANDLER` registration recipe, `BOUNCER_SESSION_BANDS` override.
    - "Static Page Gating" — `verify_pass` endpoint contract (cookie + signature pre-screen behavior, `X-Bouncer-Reason` header), Shape A vs Shape B deployment, `BOUNCER_PASS_COOKIE_DOMAIN` setting, why Shape C is out of v1 scope.
    - "Cross-Origin Embedding" — `BOUNCER_ALLOWED_ORIGINS` setting, how to embed mojo-bouncer.js + mojo-sentinel.js from a different origin, the credentialed-CORS contract, the identity-stitching limit.
    - "Bouncer-as-a-Service Deployment" — the multi-consumer pattern: any django-mojo install can serve as the bot-detection backplane for multiple separate apps. Covers: populating `BOUNCER_ALLOWED_ORIGINS` with each consumer origin; per-consumer branding via `Group.auth_domain` and `?group_uuid=`; pointing consumer nginx blocks at the shared bouncer host via the include file; capacity planning (verify_pass is the highest-volume endpoint and is cookie-validation + Redis lookup, so cheap).
20. `docs/web_developer/account/bouncer.md` — add three new top-level sections:
    - "Embedding on Static Pages or Separate-Origin SPAs" — script-tag examples for both `mojo-bouncer.js` (form/action gating) and `mojo-sentinel.js` (continuous monitoring), `data-api-base` + `data-page-type` + `data-context` attributes documented, fetch-credentials-include note.
    - "MojoSentinel.observe API" — call signature, payload shape recommendations, examples for gameplay / form-fill / API-call contexts.
    - "nginx Drop-in Protection" — copy of the `mojo-bouncer.conf` include file, the two-line variable setup, the `auth_request` + `error_page 401` snippet, the full example consumer config, and how the verify_pass pre-screen blocks known-bad signatures at the edge before reaching the protected content.
21. Ship `docs/web_developer/account/nginx/mojo-bouncer.conf` and `docs/web_developer/account/nginx/example-protected-site.conf` as actual files alongside the markdown (referenced via relative link from the "nginx Drop-in Protection" section).
22. `CHANGELOG.md` — entry: "Bouncer: continuous in-session detection (mojo-sentinel.js + streaming scorer + gradient enforcement). Cross-origin embed support via BOUNCER_ALLOWED_ORIGINS. nginx auth_request via /api/account/bouncer/verify_pass (now also pre-screens signature cache). Shipped nginx drop-in include for one-line protection of arbitrary upstream locations. mojo-bouncer.js modernized (v2.0.0) and served from django-mojo static."

### User Cases

- **wmx_test_client (separate-origin SPA, primary v1 consumer)** — embeds `mojo-sentinel.js` for continuous monitoring across player journeys + `mojo-bouncer.js` for sensitive-action gating (deposit, withdraw, RG changes). Calls bouncer endpoints with `credentials: 'include'`. Operator host added to `BOUNCER_ALLOWED_ORIGINS`. nginx routes `/auth/*` to Django (Shape A on `playground.wmwx.io`).
- **wmx_api gameplay (companion request 34)** — Alea webhook handlers write `BouncerSignal(stage='event')` directly + call `score_session(player.muid)` after each write. wmx_api registers domain analyzers via `@register_stream_analyzer`. wmx_api sets `BOUNCER_SESSION_FREEZE_HANDLER` pointing at its game-session-closer.
- **Same-host Django page** — embeds both JS files via local `/account/static/...` paths. No CORS involved. Identity stitches via shared cookie + localStorage automatically.
- **Subdomain deployment (Shape B)** — Django at `auth.example.com`, app at `app.example.com`. `BOUNCER_PASS_COOKIE_DOMAIN='.example.com'` allows the pass cookie to be shared. nginx auth_request works.
- **Cross-domain deployment (Shape C, e.g. third-party marketing site)** — sentinel still works (events post cross-origin, app's origin in allowlist). Pass cookie cannot be shared. nginx auth_request gating doesn't apply; out of v1 scope.
- **Bouncer-as-a-Service (multi-consumer)** — single django-mojo install serves N consumer apps. Operator A's marketing site, Operator B's player SPA, and an internal admin portal all point at the same `auth.mojoverify.com`. Each gets per-consumer branding via group resolution. `BOUNCER_ALLOWED_ORIGINS` is the list of all consumer origins. nginx blocks on each consumer site `include` the same `mojo-bouncer.conf` with their respective `$mojo_bouncer_host` value.
- **nginx drop-in protection of any static endpoint** — operator's marketing site has a "report incident" form. Site admin sets two variables in the nginx server block, `include`s `mojo-bouncer.conf`, then adds `auth_request /_mojo_bouncer_check;` to the location serving the form page. Three lines of nginx config now gate that page through the bouncer. No application code changes.

### Edge Cases

- **Sentinel and bouncer.js on the same page** — both share `mojo_device_uid`, both post to `/event` and `/assess` respectively. They don't fight. Sentinel emits `category` events, bouncer.js emits assess/token flow. Distinct.
- **mbp cookie with `Domain=.example.com` set via `set_cookie`** — Django's `set_cookie` handles the dotted-domain form; verify_pass_cookie validation is unchanged because it only inspects the cookie *value*, not its scope.
- **Scoring under no muid** — `score_session('')` early-returns; enforcement skipped. Events still persist as audit trail.
- **Stream analyzer crashing** — each call wrapped in try/except (mirror existing `RiskScorer.score` pattern). One broken analyzer cannot block the others or break the request.
- **Freeze handler crashing** — wrapped in try/except inside `apply_session_response`. Logs exception, fires incident anyway, returns. Scoring/persistence still succeeds.
- **High-water never decreases — what about legitimate long-running gameplay?** — TTL on the Redis key (default 24h) naturally resets the window. After idle, score starts fresh. For gameplay sessions > 24h, the score will reset mid-session; the analyzers will recompute it from observed events. Acceptable for v1; tunable via `BOUNCER_SESSION_RISK_TTL`.
- **Backward-compat event format** — old format keeps existing `_risk_action` behavior intact (firing `security:bouncer:event` incidents) including for the legacy mojo-bouncer.js until it's replaced.
- **bulk_create with stage='event' rows and the BouncerSignal post_save hooks** — current model has no `save()` override or `post_save` signal; `bulk_create` is safe. (Verified via reading the model file.)
- **CORS preflight latency** — bouncer endpoints are hit from cross-origin SPAs frequently. Existing middleware already sets `Access-Control-Max-Age: 86400` for the wildcard path; preserve that for the credentialed path too so preflights cache for 24h.
- **Sentinel observe() called before init** — guard with `if (window.MojoSentinel) MojoSentinel.observe(...)` documented; sentinel itself sets up the window global synchronously at script load.
- **sendBeacon size limits** — typical 64KB cap; 25 events fits comfortably. If buffer is much larger at pagehide, sendBeacon still attempts but may silently drop. Document the cap.
- **Multiple tabs same muid** — each tab generates its own session-id; events accumulate against the shared muid. Scoring is global per muid, which is correct (a bot using multiple tabs should aggregate).

### Testing

- Static-page gating → `tests/test_security/bouncer_verify_pass.py`
- CORS → `tests/test_security/bouncer_cors.py`
- Sentinel endpoint → `tests/test_security/bouncer_sentinel_endpoint.py`
- Stream scoring + analyzers → `tests/test_security/bouncer_stream_scoring.py`
- Enforcement bands → `tests/test_security/bouncer_enforcement.py`

Run all five with `bin/run_tests --agent -t test_security.bouncer_verify_pass test_security.bouncer_cors test_security.bouncer_sentinel_endpoint test_security.bouncer_stream_scoring test_security.bouncer_enforcement`. Use `--agent` flag and read `var/test_failures.json` for diagnostics.

### Docs

- `docs/django_developer/account/bouncer.md` — three new sections: "Continuous Detection", "Static Page Gating", "Cross-Origin Embedding"
- `docs/web_developer/account/bouncer.md` — two new sections: "Embedding on Static Pages or Separate-Origin SPAs", "MojoSentinel.observe API"
- `CHANGELOG.md` — single behavior-change entry

### Settings (added by this work)

| Setting | Default | Purpose |
|---|---|---|
| `BOUNCER_ALLOWED_ORIGINS` | `[]` | Origins permitted credentialed CORS access to bouncer endpoints |
| `BOUNCER_PASS_COOKIE_DOMAIN` | `''` | Domain attribute for the mbp pass cookie (e.g. `.example.com` for subdomain sharing) |
| `BOUNCER_SESSION_RISK_TTL` | `86400` | TTL for the Redis rolling session-risk score |
| `BOUNCER_SESSION_BANDS` | `{'freeze': 90, 'shadow_ban': 70, 'require_step_up': 50, 'monitor': 30}` | Score-to-action thresholds |
| `BOUNCER_SESSION_FREEZE_HANDLER` | `None` | Dotted-path to app-defined freeze callable `(user, device, score)` |

### Out of Scope (deferred from earlier passes)

- Deleting the legacy `mojo-verify/mverify_api/apps/mojoverify/bouncer/` directory — tracked in mverify_api separately once this lands and consumers are migrated.
- Cross-domain (Shape C) static-page gating via signed-token redirect — separate request when first concrete consumer appears.
- ML-based anomaly detection / per-user baselines — v1 uses fixed thresholds. Population baselining is v2.
- Devtools-open detection — intentionally not done; too unreliable in 2026 browsers.
- Game-domain stream analyzers (reaction time, accuracy percentiles, action-grammar replay, win-rate anomalies) — owned by consumer apps via `@register_stream_analyzer`, tracked in `wmx_api/planning/requests/34-bouncer-session-game-detection.md`.

---

## Resolution

**Status**: Resolved — 2026-05-19

### What Was Built

Continuous in-session bot detection + static-page protection landed in two commits on `main`:

1. **`account/bouncer: continuous in-session detection + static-page protection`** — primary implementation (3,808 insertions, 25 files)
2. **`account/bouncer: post-review polish — batch cap, doc index, version`** — security-review follow-up (43 insertions, 6 files)

### Files Changed

**Server-side (Python):**
- `mojo/middleware/cors.py` — credentialed CORS for bouncer paths via `BOUNCER_ALLOWED_ORIGINS`
- `mojo/apps/account/rest/bouncer/assess.py` — new `verify_pass` endpoint with sig-cache pre-screen; `BOUNCER_PASS_COOKIE_DOMAIN` support
- `mojo/apps/account/rest/bouncer/event.py` — batched `{events: [...]}` payload format + inline `score_session()` call + 200-row hard cap
- `mojo/apps/account/rest/bouncer/static.py` — three new static-serving endpoints
- `mojo/apps/account/services/bouncer/stream_scoring.py` (new) — `BaseStreamAnalyzer`, `register_stream_analyzer`, `score_session`
- `mojo/apps/account/services/bouncer/stream_analyzers.py` (new) — 5 universal analyzers
- `mojo/apps/account/services/bouncer/enforcement.py` (new) — gradient bands + freeze-handler resolver
- `mojo/apps/account/services/bouncer/__init__.py` — re-exports + stream_analyzers import
- `mojo/apps/incident/models/rule.py` — 4 new session-band ruleset defaults

**Client (JS/CSS):**
- `mojo/apps/account/static/account/mojo-bouncer.js` (new, v2.0.0) — modernized embeddable gate
- `mojo/apps/account/static/account/mojo-bouncer.css` (new) — overlay stylesheet
- `mojo/apps/account/static/account/mojo-sentinel.js` (new) — lightweight in-session telemetry

**nginx config artifacts:**
- `docs/web_developer/account/nginx/mojo-bouncer.conf` (new) — drop-in include
- `docs/web_developer/account/nginx/example-protected-site.conf` (new) — worked example

### Tests
- `tests/test_security/bouncer_verify_pass.py` — 5 tests, covers cookie path + sig-cache pre-screen + cookie-domain attribute
- `tests/test_security/bouncer_cors.py` — 4 tests, credentialed/wildcard branches + OPTIONS preflight + regression on non-bouncer paths
- `tests/test_security/bouncer_sentinel_endpoint.py` — 5 tests, batched persist + inline scoring + legacy back-compat + 200-row cap + empty-array
- `tests/test_security/bouncer_stream_scoring.py` — 10 tests, every universal analyzer (trigger + no-trigger) + high-water + cap-100 + empty-muid
- `tests/test_security/bouncer_enforcement.py` — 7 tests, all 4 bands + custom-band override + freeze-handler failure isolation
- `tests/test_security/_enforcement_helpers.py` (new) — module-loaded helpers for dotted-path resolver tests
- 31 new tests total, all green. Run via `bin/run_tests --agent --full -t test_security.bouncer_verify_pass -t test_security.bouncer_cors -t test_security.bouncer_sentinel_endpoint -t test_security.bouncer_stream_scoring -t test_security.bouncer_enforcement`

### Docs Updated
- `docs/django_developer/account/bouncer.md` — four new sections: Continuous Detection, Static Page Gating, Cross-Origin Embedding, Bouncer-as-a-Service Deployment
- `docs/django_developer/account/README.md` — index entry expanded
- `docs/web_developer/account/bouncer.md` — three new sections: Embedding on Static Pages or Separate-Origin SPAs, MojoSentinel.observe API (including `flush()` and `getDuid()`), nginx Drop-in Protection
- `docs/web_developer/account/README.md` — index entry expanded
- `CHANGELOG.md` — entry under v1.2.20

### Security Review
- No critical findings. The freeze-handler dotted-path is settings-controlled (operator-trusted), the CORS allowlist is exact-match, raw_signals data is stored as JSON and never eval'd/templated, and the new endpoint headers (`X-Bouncer-Muid`, `X-Bouncer-Reason`) are not secrets.
- One concrete hardening applied in the polish commit: 200-row batch cap on `/event` to bound payload-driven bulk_create and scoring-window size.
- One pre-existing concern flagged for separate follow-up: the bouncer challenge page forwards the `redirect` query param through without origin-validation. Not introduced by this work; recommend a dedicated request to validate the post-pass redirect URL against `BOUNCER_ALLOWED_ORIGINS` or a same-host check.

### Follow-up
- **Redirect validation** on the bouncer challenge page (pre-existing; file separate request when surfaced).
- **Cross-domain Shape C support** — signed-token redirect dance for static sites whose eTLD+1 differs from the bouncer host. Out of scope for v1; addressed when a concrete consumer surfaces.
- **mverify_api legacy bouncer cleanup** — delete `mojo-verify/mverify_api/apps/mojoverify/bouncer/` once consumers are migrated. Tracked in that repo separately.
- **Game-domain analyzers** — registered by consumer apps (e.g. wmx_api) via `@register_stream_analyzer`. Tracked in `wmx_api/planning/requests/34-bouncer-session-game-detection.md`.
