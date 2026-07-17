---
# id is assigned by /scope on pickup — leave it blank
id: DM-043
type: feature
title: Enforce login-flow geofencing after credential verification
priority: P2
effort: M
owner: backend
opened: 2026-07-17
depends_on: []
related: []       # web-mojo planning/inbox/user-geofence-whitelist-toggle.md (UI toggle for bypass_geofence)
links: []
---

# Enforce login-flow geofencing after credential verification

## What & Why

Geofencing on auth endpoints runs **pre-auth** today (`@md.requires_geofence(scope="auth")`
blocks before the view body), which means:

1. **Per-user whitelisting doesn't work at login.** The engine's `bypass_geofence`
   short-circuit (`engine.py:394`) only fires for an authenticated user; a login
   request is anonymous, so a whitelisted user in a blocked country cannot log in.
2. **Block evidence has no user.** `request.user` is anonymous at decorator time,
   so `geofence_block` events carry `uid=None` — we can't answer "which user was
   geofenced."

**Decided flow (Ian):** verify credentials first; only on success run the geofence
check with the now-verified user. Blocked → the standard geofence 403. Credential
failure → the normal invalid response, unchanged. This makes `bypass_geofence`
work at login, attributes blocks to a verified user, and has no enumeration
exposure (behavior is identical until the password/code is proven).

**Accepted tradeoff (explicitly acknowledged):** a caller in a blocked geo holding
valid stolen credentials can distinguish 403 (creds valid, geo blocked) from 401
(creds invalid) — a credential-validation oracle. Accepted: geofencing is not a
credential-testing defense; bouncer + rate limits still run first.

## Acceptance Criteria

- [ ] On every identity-bearing auth endpoint, geofence is evaluated **after**
      credential verification with the verified `user` passed to
      `GeoFenceEngine.check` — a user holding `bypass_geofence` completes login
      from a blocked geo.
- [ ] Invalid credentials from a blocked geo return the normal 401 invalid
      response (no geofence signal); valid credentials from a blocked geo return
      the standard geofence 403 body (same leak-scrubbed shape as today).
- [ ] Password login: the check runs after `check_password` succeeds and
      **before** the MFA challenge is returned (`mfa_required_response`) — a
      geofenced session never receives an `mfa_token`.
- [ ] MFA-finish / token flows are covered via the common choke point (see
      Investigation) — a stolen `mfa_token`/magic-link/reset-code replayed from
      a blocked geo is still blocked at completion.
- [ ] `geofence_block` events for post-credential blocks carry the verified
      user (`uid`); `tests/test_geofence/evidence_plane.py` asserts it.
- [ ] Identity-less auth endpoints (register, forgot-password start, magic-link
      send, OTP send, passkey/oauth begin, phone-register) keep the current
      pre-auth decorator behavior unchanged.
- [ ] `GET /api/geo/rules` `enforced_endpoints` remains truthful — deferred
      endpoints stay registered in `SECURITY_REGISTRY` (don't silently drop
      them from the audit surface).
- [ ] `tests/test_geofence/decorator.py` ordering assertions updated to the new
      contract; regression coverage for: blocked-geo + valid creds → 403,
      blocked-geo + invalid creds → 401, blocked-geo + `bypass_geofence` user →
      login succeeds.

## Investigation

**What exists**
- Enforcement mechanism: `mojo/decorators/geofence.py:52-84` — wrapper runs
  `GeoFenceEngine.check` + `evidence.report_block` pre-view, 403s on block.
- Engine already accepts `user=` and checks `bypass_geofence` first
  (`mojo/apps/account/services/geofence/engine.py:382-401`); no engine change
  needed for the bypass itself.
- Common token-issuance choke point: `jwt_login`
  (`mojo/apps/account/rest/user.py:621`) — every successful auth flow funnels
  through it.

**Endpoints to move to post-credential enforcement** (13 — all resolve + verify
a User mid-flow and reach `jwt_login`):
- `on_user_login` (user.py:152; password verified :181; jwt :202 or MFA :201)
- `on_auth_exchange` (user.py:233; handoff code :241)
- `on_user_password_reset_code` (user.py:770) / `_token` (user.py:803)
- `on_magic_login_complete` (user.py:866)
- `on_email_verify` (user.py:908)
- `on_invite_accept` (user.py:926)
- `on_totp_verify` (totp.py:145) / `on_totp_recover` (totp.py:180) /
  `on_totp_login` (totp.py:221)
- `on_sms_verify` (sms.py:113 — serves both `sms_mfa` finish and standalone)
- `on_passkeys_login_complete` (passkeys.py:214)
- `on_oauth_complete` (oauth.py:332)

**Endpoints that keep the pre-auth decorator** (no verified identity):
`on_auth_handoff`, `on_register` (hybrid — see below), `on_user_forgot`,
`on_magic_login_send`, `on_email_verify_send`, `on_sms_login`,
`on_phone_register_start`/`_verify`, `on_passkeys_login_begin`, `on_oauth_begin`.

**Proposed shape (for /scope to refine)**
1. Enforce inside `jwt_login()` — one call covers all 13 flows' issuance path.
2. Plus one early check in `on_user_login` after `check_password`, before
   `mfa_required_response` (which is side-effect-free — mints `mfa_token` only —
   but a blocked session must not receive a challenge token).
3. Decorator gets a deferred mode (e.g. `@md.requires_geofence(scope="auth",
   after_auth=True)`) that **registers** in `SECURITY_REGISTRY` (keeps
   `enforced_endpoints` truthful, ideally annotated `post_auth`) but does not
   block pre-view. Identity-less endpoints keep the blocking mode.
4. Evidence: `report_block` (`services/geofence/evidence.py:34`) must accept an
   explicit `user=` — `request.user` is not set on these endpoints even after
   in-view verification; the reporter only attributes via `request.user`
   (`mojo/apps/incident/reporter.py:79-80`). Mirror the explicit-attribution
   pattern already used by `report_config_change` (evidence.py:97).

**Constraints / edge cases**
- `on_register` is hybrid: keeps its pre-auth decorator (identity-less), but two
  branches reach `jwt_login` (:417 proven-phone, :558 new user) and will pick up
  the choke-point check — same-request double evaluation is a cached no-op;
  verify no behavior change.
- Password-reset flows: the reset itself succeeds before `jwt_login`; a
  post-reset geofence block means "password changed, no session issued." Decide
  at /scope whether that's acceptable (recommended: yes — the reset was proven
  by the code/token; only auto-login is withheld) or whether to check before
  applying the new password.
- Blocked 403 body must stay the leak-scrubbed shape (`error`, `code`, `reason`,
  `detail` only).
- Decision caching: bypass results are deliberately never cached (revocation is
  immediate) — preserve that.

**Tests to update**
- `tests/test_geofence/decorator.py` — pre-auth ordering assertions
  (blocked-before-credential-logic) flip to the new contract.
- `tests/test_geofence/evidence_plane.py` — already drives login with valid
  creds; add `uid` attribution assertions.
- `tests/test_geofence/config_plane.py:108-111` — `enforced_endpoints`
  membership if the registry annotation changes.

**Docs**
- `docs/django_developer/account/geofence.md` (Decorator section + bypass
  section — the "doesn't work at login" caveat becomes "works at login").
- `docs/web_developer/` auth docs: login can now return geofence 403 *after*
  valid credentials; error shape unchanged.
- `CHANGELOG.md`.

## Plan

**Approved by Ian 2026-07-17** (including the two decisions: source-exempt list for
`sessions_revoke`/`email_change`, and token-proven actions completing before the
session is withheld).

### Goal
Move geofence enforcement on identity-bearing auth endpoints from the pre-auth
decorator to after credential verification, so `bypass_geofence` works at login
and `geofence_block` events carry the verified user — with zero change to the
403 body shape, evidence semantics, or identity-less endpoints.

### Context — what exists

- **Decorator** `mojo/decorators/geofence.py` — `_apply_geofence` registers
  `SECURITY_REGISTRY[key] = {"geofence": {"scope": scope}}` (lines 42-50); the
  wrapper (52-84) calls `GeoFenceEngine.check(request, group=request.group,
  user=request.user, scope=scope)`, and on allow handles two notable paths
  (`lookup_failed` → `evidence.report_block`; `ip_allowlisted` +
  `would_block` → `evidence.report_exempt`), on block calls `report_block` and
  returns `JsonResponse({"error": "geofence_blocked", "code": 403, "reason":
  decision.reason, "detail": decision.detail}, status=403)`. Imports services
  lazily inside the wrapper.
- **Engine** `mojo/apps/account/services/geofence/engine.py` —
  `GeoFenceEngine.check` order: enabled kill-switch (389) → **bypass perm check
  (395-401, no cache write)** → no-rules fast path (411) → cache lookup (419).
  Cache key is `geofence:dec:{ip}:{group_id}` (`cache.py:14`) — user-independent;
  bypass short-circuits BEFORE the cache read, so a cached anonymous block can
  never mask a bypass holder. No engine change needed.
- **`jwt_login`** `mojo/apps/account/rest/user.py:621-684` — the common
  issuance choke point. Success side effects all happen early: `user.last_login`
  + `user.track()` (634-635), `UserLoginEvent.track` (637-642),
  `fire_user_login` / `USER_LOGIN_HANDLER` (670-671). Returns `JsonResponse`
  normally; the `legacy=True` path returns a plain dict (legacy is only set from
  `on_user_login` when path is `account/jwt/login`). **Every one of the 18 call
  sites does `return jwt_login(...)`** — a 403 JsonResponse returned from inside
  propagates cleanly (dispatcher accepts both dict and JsonResponse).
- **`jwt_login` callers** (file:line → source):
  login flows — user.py:202 password, :249 handoff, :417 register-proven-phone
  (sms), :558 register-new-user, :798/:822 password_reset, :878 magic, :920
  email_verify, :942 invite, totp.py:169/:210/:258 (totp_mfa/recovery/totp),
  sms.py:153 (sms_mfa|sms), passkeys.py:249 passkey, oauth.py:372 oauth.
  NON-login re-issues — user.py:1202 `on_user_change_email`
  (source="email_change"), user.py:1522 `on_sessions_revoke`
  (source="sessions_revoke", `@requires_auth` + `@requires_fresh_auth`).
- **MFA branch** `on_user_login` user.py:199-202: `get_mfa_methods(user)` → if
  any, `return mfa_required_response(user, mfa_methods)` (defined :577-589 —
  pure: mints a Redis mfa_token via `mfa_service.create_mfa_token`, mfa.py:21-36,
  no other side effects) — this branch never reaches jwt_login.
- **Evidence** `mojo/apps/account/services/geofence/evidence.py` —
  `report_block` (:34) / `_report_block` (:42) records metrics + dedupes per
  (ip, reason)/hour + calls `reporter.report_event(..., request=request,
  geofence_scope=scope, ...)`. No user attribution today: the reporter
  (`mojo/apps/incident/reporter.py:44-86`) pops `uid` from kwargs (:56) but only
  overwrites from `request.user` when authenticated (:79) — login requests are
  anonymous, so an explicit `uid=` kwarg survives. `report_config_change`
  (evidence.py:97-123) is the existing explicit-`user=` pattern to mirror.
  `report_exempt` sits alongside report_block in the same file.
- **`enforced_endpoints`** `mojo/apps/account/rest/geofence.py:149-159` — reads
  `SECURITY_REGISTRY`, includes any entry whose `entry["geofence"]` is a
  non-None dict, reads only `.get("scope")`. Extra keys are harmless.
- **Only other `GeoFenceEngine.check` callers**: `rest/geofence.py:79,:86`
  (`GET /api/geo/check`). Nothing hidden.
- **OAuth doc error**: `docs/django_developer/account/geofence.md:214` heading
  says `/complete` is not decorated — wrong; `/complete` IS decorated
  (oauth.py:332) and calls jwt_login (:372). The undecorated redirect endpoint
  is `/callback` (oauth.py:263, returns `HttpResponseRedirect`, no user, no
  jwt_login). The body text (:216) is correct about `/callback`; only the
  heading's endpoint name is wrong.

### Changes — what to do

1. **New `mojo/apps/account/services/geofence/enforcement.py`** — the single
   shared enforcement routine (KISS, ~40 lines):

   ```python
   def enforce(request, scope=None, user=None):
       """Evaluate geofence for this request; return None when allowed or the
       blocked 403 JsonResponse. Mirrors the decorator's exact behavior,
       including evidence emission for allowed-but-notable outcomes."""
       from mojo.helpers.response import JsonResponse
       from mojo.apps.account.services.geofence import GeoFenceEngine, evidence
       decision = GeoFenceEngine.check(
           request, group=getattr(request, "group", None),
           user=user if user is not None else getattr(request, "user", None),
           scope=scope)
       if decision.allowed:
           request.geofence_decision = decision
           if decision.reason == "lookup_failed":
               evidence.report_block(request, decision, scope, user=user)
           elif decision.reason == "ip_allowlisted" and decision.get("would_block"):
               evidence.report_exempt(request, decision, scope, user=user)
           return None
       evidence.report_block(request, decision, scope, user=user)
       return JsonResponse({
           "error": "geofence_blocked", "code": 403,
           "reason": decision.reason, "detail": decision.detail,
       }, status=403)
   ```
   Export from `services/geofence/__init__.py` alongside the engine.

2. **`mojo/decorators/geofence.py`** —
   - `requires_geofence(scope=None, after_auth=False)`; thread `after_auth`
     through `_apply_geofence`.
   - Registry entry becomes `{"scope": scope, "after_auth": True}` when
     deferred (keeps `enforced_endpoints` truthful; consumer unaffected).
   - Blocking wrapper body: replace lines 57-82 with a call to
     `enforcement.enforce(request, scope=scope)` — `None` → call view, else
     return the response. Byte-identical 403 body.
   - `after_auth=True` wrapper: no engine call — return `func(request, ...)`
     directly (enforcement happens in jwt_login / the MFA branch).

3. **`mojo/apps/account/services/geofence/evidence.py`** — add `user=None`
   param to `report_block`/`_report_block` and `report_exempt`/its inner; when
   `user` is not None pass `uid=user.id` (and `username=user.username` into
   metadata kwargs) to `reporter.report_event`. No other changes — metrics,
   dedupe, levels untouched.

4. **`mojo/apps/account/rest/user.py`** —
   - Top of `jwt_login` (before line 634), lazy import:
     ```python
     GEOFENCE_EXEMPT_SOURCES = ("sessions_revoke", "email_change")
     # inside jwt_login, first statement:
     if source not in GEOFENCE_EXEMPT_SOURCES:
         from mojo.apps.account.services.geofence import enforcement
         blocked = enforcement.enforce(request, scope="auth", user=user)
         if blocked is not None:
             return blocked
     ```
     Enforce-by-default: any future source is geofenced unless explicitly
     exempted (fail-closed). The two exemptions are authed re-issues — a user
     in a blocked geo must still be able to revoke their own sessions.
   - `on_user_login` MFA branch (line 199-202): inside `if mfa_methods:`,
     before `mfa_required_response`, run the same
     `enforcement.enforce(request, scope="auth", user=user)`; return the 403 if
     blocked (a blocked user never receives an mfa_token). Non-MFA path checks
     exactly once, inside jwt_login.
   - Switch decorator to `@md.requires_geofence(scope="auth", after_auth=True)`
     on: `on_user_login` (:152), `on_auth_exchange` (:233),
     `on_user_password_reset_code` (:770), `on_user_password_reset_token`
     (:803), `on_magic_login_complete` (:866), `on_email_verify` (:908),
     `on_invite_accept` (:926).
   - Leave BLOCKING decorator unchanged on: `on_auth_handoff` (:212, authed —
     bypass already works pre-view), `on_register` (:256 — must block BEFORE
     account creation; its two jwt_login branches re-check post-auth, which is
     a cached no-op and can only be more permissive via bypass),
     `on_user_forgot` (:690), `on_magic_login_send` (:828),
     `on_email_verify_send` (:888).

5. **`mojo/apps/account/rest/totp.py`** — `after_auth=True` on `on_totp_verify`
   (:145), `on_totp_recover` (:180), `on_totp_login` (:221).

6. **`mojo/apps/account/rest/sms.py`** — `after_auth=True` on `on_sms_verify`
   (:113) only. `on_sms_login` (:163), `on_phone_register_start` (:194),
   `on_phone_register_verify` (:232) keep blocking (identity-less / OTP send).

7. **`mojo/apps/account/rest/passkeys.py`** — `after_auth=True` on
   `on_passkeys_login_complete` (:214). `on_passkeys_login_begin` (:156) keeps
   blocking.

8. **`mojo/apps/account/rest/oauth.py`** — `after_auth=True` on
   `on_oauth_complete` (:332). `on_oauth_begin` (:196) keeps blocking.
   `/callback` stays undecorated (redirect, no identity).

### Design decisions

- **Return a JsonResponse, don't raise** — no merrors class produces the
  geofence body; `PermissionDeniedException` would fire an extra
  security-incident event (double-reporting alongside `report_block`) and
  change the body shape `decorator.py::test_403_body_omits_signals` asserts on.
  Every jwt_login call site is `return jwt_login(...)`, so a returned 403
  propagates; fine on the legacy-dict path too (dispatcher accepts both).
- **Source-exempt list, not caller opt-in** — enforce-by-default in jwt_login so
  new login flows can't silently skip geofencing; only the two authed re-issues
  are exempt (approved).
- **Token-proven actions complete before session withheld** (approved) — reset
  code/token, email verify, invite accept perform their mutation, then
  jwt_login 403s. The action was proven by the emailed secret; only auto-login
  is geofenced. No pre-mutation second check.
- **Keep `after_auth` endpoints in SECURITY_REGISTRY** — audit surface
  (`GET /api/geo/rules` → enforced_endpoints) must not shrink; annotate with
  `after_auth: True`.
- **No engine changes** — bypass-before-cache and never-caching-bypass already
  give correct semantics; cache key (ip, group) is safe because the only
  user-dependent outcome (bypass) is decided before the cache read.
- **MFA check placed inside the `if mfa_methods:` branch** — exactly one engine
  evaluation per login request on both paths (MFA: pre-challenge; non-MFA: in
  jwt_login).

### Edge cases & risks

- `on_register` double-evaluation (pre-auth decorator + jwt_login): second
  check is cache-hit or bypass; can only be MORE permissive (bypass), never
  stricter — no behavior change for non-bypass users.
- `on_sms_verify` serves both `sms_mfa` finish and standalone `sms` — both
  resolve the user before jwt_login (sms.py:125-133); single decorator swap
  covers both.
- Fail-open `lookup_failed` must still ALLOW login and emit the level-6 event
  (`evidence_plane.py::test_lookup_failed_fail_open_level6` asserts 200) —
  handled because `enforce()` replicates the wrapper's allowed-but-notable
  paths verbatim.
- Fail-closed scopes: `enforce(..., scope="auth")` keeps
  `GEOFENCE_FAIL_CLOSED_SCOPES=["auth"]` behavior
  (`test_scope_fail_closed_level5`).
- Blocked login must produce NO success side effects: check is the first
  statement of jwt_login, ahead of last_login/track/UserLoginEvent/
  USER_LOGIN_HANDLER.
- uid attribution: reporter.py:79 overwrites `uid` from `request.user` only
  when authenticated — login requests are anonymous so the explicit kwarg
  survives; on authed flows (e.g. handoff exchange has no request.user either —
  token consumed in-view) same story.
- Test-mode headers (`X-Mojo-Test-Geo` etc.) flow through unchanged — engine
  reads them from `request`, which enforce() passes straight through.

### Tests

Existing suites stay green by design (all drive login with VALID creds; scope,
levels, dedupe, metrics, 403 body unchanged):
`tests/test_geofence/decorator.py`, `evidence_plane.py`, `config_plane.py`
(asserts only `len(enforced_endpoints) > 0`).

New file `tests/test_geofence/post_auth.py` (testit — `@th.django_unit_test()`,
`def test_xxx(opts):`, header mechanism from `tests/test_geofence/_helpers.py`,
descriptive assert messages, setup cleans before creating):
1. Blocked geo + INVALID password → **401** invalid-credentials (not 403) — the
   new ordering contract.
2. Blocked geo + valid password → 403 `geofence_blocked`; AND `last_login`
   unchanged + no new `UserLoginEvent` row (no success side effects).
3. Blocked geo + valid password + user holds `bypass_geofence` → 200 with
   tokens (the whole point of DM-043).
4. Blocked geo + valid password + MFA-enrolled user → 403, response contains no
   `mfa_token`.
5. MFA finish from blocked geo: mint mfa_token from allowed geo (password step),
   then `/api/auth/totp/verify` (or sms verify) with blocked-geo headers → 403.
6. `geofence_block` event from (2) carries `uid == user.id` (and
   `metadata.geofence_scope == "auth"`, `source_ip`).
7. `POST /api/auth/sessions/revoke` with valid token + blocked-geo headers →
   200 (exempt source still works).
8. Password reset via code from blocked geo → password IS changed, response is
   403 (documented accepted behavior).

### Docs

- `docs/django_developer/account/geofence.md`: Decorator section (+`after_auth`
  mode and the jwt_login choke point), `bypass_geofence` section (now works at
  login; remove the pre-auth caveat), fix `:214` heading `/complete` →
  `/callback`, note the exempt sources and the token-proven-actions behavior.
- `docs/web_developer/`: auth/login docs — geofence 403 can now occur after
  valid credentials; body shape unchanged; invalid credentials always return
  the normal 401.
- `CHANGELOG.md`: behavior change entry.

### Open questions
None — both decision points approved (see top of Plan).

## Notes

- **Baseline (2026-07-17, before first edit)**: `bin/run_tests --agent` →
  total 2494 / passed 2438 / failed 0 / skipped 56. All green — no
  pre-existing failures; any post-change failure is attributable to DM-043.
- **Post-build security review (WARNING, fixed)**: `on_oauth_complete` on the
  deferred decorator let a blocked-geo caller with a valid provider code
  provision a NEW account (user + connection + GroupMember + registration
  webhook + stored provider tokens) before the jwt_login check. Fixed:
  `enforce()` now runs in-view right after the provider exchange proves the
  identity, before `_find_or_create_user` — existing users resolve via the
  new lookup-only `_lookup_known_user` (bypass honored), unknown identities
  enforce anonymously. Regression: lookup-only test in `tests/test_oauth/`.
  Full `/complete` endpoint flow is not end-to-end testable (real provider
  network exchange; no test provider exists — same reason existing oauth
  tests exercise `_find_or_create_user` in-process).
- **Security review INFO (accepted, no action)**: (1) in the MFA branch,
  `clear_rate_limits(account_id)` runs before the geofence check — resets a
  brute-force counter for an already-correct password, no access granted;
  (2) a crash inside `enforce()` on after_auth endpoints burns already-
  consumed single-use artifacts (mfa_token, recovery/magic tokens) —
  fail-closed, availability-only, same class as the accepted
  mutation-before-check design.
- **Follow-up from docs sweep**: `enforced_endpoints` now surfaces
  `after_auth: true` (`rest/geofence.py::_enforced_endpoints`) so the audit
  API distinguishes pre-view from post-credential enforcement.
- **Pre-existing bug discovered & filed** (not caused by DM-043): ten
  decorators in `mojo/decorators/auth.py` OVERWRITE `SECURITY_REGISTRY[key]`,
  clobbering the geofence sub-entry whenever they sit above
  `@requires_geofence` — `enforced_endpoints` has been under-reporting most
  geofenced endpoints all along (enforcement unaffected). Filed:
  `planning/inbox/security-registry-decorator-clobbering.md`.

## Resolution
- closed: 2026-07-17
- branch: main
- files changed: .claude/skills/request/SKILL.md,.claude/skills/scope/SKILL.md,AI_DEV.md,CHANGELOG.md,CLAUDE.md,bin/create_testproject,docs/django_developer/account/api_keys.md,docs/django_developer/account/auth.md,docs/django_developer/account/disable_lifecycle.md,docs/django_developer/account/geofence.md,docs/django_developer/account/group.md,docs/django_developer/core/decorators.md,docs/django_developer/core/middleware.md,docs/django_developer/core/mojo_model.md,docs/django_developer/core/permissions.md,docs/django_developer/core/rate_limiting.md,docs/django_developer/helpers/settings_reference.md,docs/django_developer/logging/incidents.md,docs/django_developer/realtime/README.md,docs/django_developer/realtime/architecture.md,docs/django_developer/rest/permissions.md,docs/django_developer/security/README.md,docs/django_developer/security/abuse_hardening.md,docs/django_developer/security/maestro_board.md,docs/web_developer/account/api_keys.md,docs/web_developer/account/authentication.md,docs/web_developer/account/bouncer.md,docs/web_developer/account/group.md,docs/web_developer/account/user.md,docs/web_developer/core/request_response.md,docs/web_developer/logging/reporting_events.md,docs/web_developer/realtime/websocket.md,docs/web_developer/security/README.md,docs/web_developer/security/maestro_board.md,docs/web_developer/security/rate_limits.md,memory.md,mojo/apps/account/models/api_key.py,mojo/apps/account/models/user.py,mojo/apps/account/rest/bouncer/assess.py,mojo/apps/account/rest/bouncer/event.py,mojo/apps/account/rest/geofence.py,mojo/apps/account/rest/group.py,mojo/apps/account/rest/oauth.py,mojo/apps/account/rest/passkeys.py,mojo/apps/account/rest/sms.py,mojo/apps/account/rest/totp.py,mojo/apps/account/rest/user.py,mojo/apps/account/services/disable.py,mojo/apps/account/services/geofence/__init__.py,mojo/apps/account/services/geofence/enforcement.py,mojo/apps/account/services/geofence/evidence.py,mojo/apps/incident/asyncjobs.py,mojo/apps/incident/cronjobs.py,mojo/apps/incident/handlers/event_handlers.py,mojo/apps/incident/migrations/0032_maestroboard_maestroboardlink.py,mojo/apps/incident/models/__init__.py,mojo/apps/incident/models/maestro_board.py,mojo/apps/incident/models/maestro_board_link.py,mojo/apps/incident/models/rule.py,mojo/apps/incident/models/ticket.py,mojo/apps/incident/rest/__init__.py,mojo/apps/incident/rest/event.py,mojo/apps/incident/rest/maestro_board.py,mojo/apps/incident/rest/maestro_webhook.py,mojo/apps/incident/services/__init__.py,mojo/apps/incident/services/maestro_sync.py,mojo/apps/realtime/asgi.py,mojo/apps/realtime/handler.py,mojo/decorators/auth.py,mojo/decorators/geofence.py,mojo/decorators/http.py,mojo/decorators/limits.py,mojo/models/rest.py,planning/.config,planning/.next_id,planning/_template.md,planning/done/DM-001-render-allowlisted-extra-registration-fields-promo.md,planning/done/DM-002-step-up-recent-authentication-gate-for-sensitive-o.md,planning/done/DM-003-register-page-enter-on-phone-otp-field-fires-step-.md,planning/done/DM-004-sign-in-alternate-method-button-row-overflows-clip.md,planning/done/DM-005-phone-register-one-wrong-sms-code-burns-the-sessio.md,planning/done/DM-006-sms-sign-in-with-an-unrecognized-number-dead-ends-.md,planning/done/DM-007-full-test-suite-is-flaky-content-guard-false-posit.md,planning/done/DM-008-phone-signup-may-fail-to-sign-in-an-existing-accou.md,planning/done/DM-009-get-remote-ip-trusts-client-supplied-x-forwarded-f.md,planning/done/DM-010-websocket-ip-resolver-trusts-client-spoofable-sour.md,planning/done/DM-011-ip-storage-fields-assume-ipv4-non-null-ipv6-trunca.md,planning/done/DM-012-auth-middleware-500s-on-a-malformed-authorization-.md,planning/done/DM-013-management-command-to-create-initial-users-admins.md,planning/done/DM-014-var-dev-server-conf-overrides-config-dev-server-co.md,planning/done/DM-015-configurable-outbound-webhook-signature-header-use.md,planning/done/DM-016-group-user-has-permission-crashes-on-apikey-identi.md,planning/done/DM-017-geofence-config-evidence-plane-editable-system-rul.md,planning/done/DM-018-audit-requires-perms-group-fallback-on-global-effe.md,planning/done/DM-019-self-minted-group-apikey-with-arbitrary-permission.md,planning/done/DM-020-geofence-evidence-metrics-dual-write-group-scoped-.md,planning/done/DM-021-geofence-hardening-opt-in-strict-compliance-enforc.md,planning/done/DM-022-member-readable-geofence-policy-events-group-scope.md,planning/done/DM-023-geofence-adjacent-settings-bypass-write-time-valid.md,planning/done/DM-024-same-key-in-query-string-json-body-merges-to-a-lis.md,planning/done/DM-025-dispatcher-numeric-group-resolution-skips-is-activ.md,planning/done/DM-026-github-oauth-login-on-the-bouncer-hosted-auth-page.md,planning/done/DM-027-group-rest-save-collapses-to-the-view-check-any-ac.md,planning/done/DM-028-post-api-group-member-invite-returns-a-raw-500-typ.md,planning/done/DM-029-add-explicit-auth-gates-to-the-permission-check-si.md,planning/done/DM-030-jsonfield-replace-bypasses-protected-json-perms-ma.md,planning/done/DM-031-geofence-test-override-mojo-test-mode-are-db-redis.md,planning/done/DM-032-rest-batch-save-skips-instance-level-permission-ch.md,planning/done/DM-033-fileman-initiated-uploads-can-t-be-completed-or-fk.md,planning/done/DM-034-oauth-login-drops-the-redirect-param-user-lands-on.md,planning/done/DM-035-field-action-level-permission-gates-omit-the-base-.md,planning/done/DM-036-apikey-set-permissions-silently-discards-non-dict-.md,planning/done/DM-037-apikey-validate-token-grants-group-context-without.md,planning/done/DM-038-rest-batch-save-ignores-can-update-can-create-flag.md,planning/done/DM-039-get-api-group-pk-member-resolves-touches-any-group.md,planning/done/DM-040-incident-maestroboard-push-link-tickets-into-a-rem.md,planning/done/DM-041-config-driven-item-id-prefixes-dm-canonical-workfl.md,planning/done/DM-042-authenticated-abuse-doom-loop-hardening-default-pe.md,planning/in_progress/DM-043-enforce-login-flow-geofencing-after-credential-ver.md,planning/inbox/apikey-group-context-ignores-group-is-active.md,planning/inbox/apikey-identity-gate-hardening.md,planning/inbox/apikey-parent-key-inactive-descendant-one-way-door.md,planning/inbox/apikey-suspension-residual-surfaces.md,planning/inbox/batch-ignores-can-update-can-create-flags.md,planning/inbox/filevault-unfiltered-pk-cross-tenant-access.md,planning/inbox/get-member-for-user-parent-walk-ignores-parent-is-active.md,planning/inbox/group-me-member-endpoint-oracle-touch.md,planning/inbox/login-event-snapshot-region-code.md,planning/inbox/maestro-webhook-replay-timestamp.md,planning/inbox/member-perms-ignore-group-is-active.md,planning/inbox/phone-verify-dev-bypass-code-db-settable.md,planning/inbox/security-registry-decorator-clobbering.md,planning/inbox/serializer-reverse-onetoone-graph-emits-empty-list.md,planning/inbox/test-security-full-suite-red.md,planning/inbox/user-is-superuser-unguarded-on-non-user-identity.md,scripts/intake.sh,scripts/ready.sh,testit/client.py,tests/test_account/test_bouncer_limits.py,tests/test_account/test_disable_kill_switch.py,tests/test_account/test_geolocated_ip_aggregation.py,tests/test_account/test_group_me_member_oracle.py,tests/test_assistant/28_test_fk_perm_check.py,tests/test_email/email_change.py,tests/test_geofence/post_auth.py,tests/test_global_perms/apikey_group_inactive.py,tests/test_limits/__init__.py,tests/test_limits/api_throttle.py,tests/test_limits/block_dedup.py,tests/test_limits/traffic_concentration.py,tests/test_maestro_board/__init__.py,tests/test_maestro_board/test_maestro_rest.py,tests/test_maestro_board/test_maestro_service.py,tests/test_models/batch_feature_flags.py,tests/test_oauth/oauth.py,tests/test_realtime/connection_limits.py,tests/test_verification/verification.py,uv.lock
- tests added: tests/test_geofence/post_auth.py (8: blocked+invalid→401,
  blocked+valid→403 w/ zero side effects + uid on event, bypass user logs in,
  MFA blocked pre-challenge, MFA finish blocked at jwt_login, sessions_revoke
  exempt, reset applies but session withheld, after_auth registry annotation);
  tests/test_oauth/oauth.py (_lookup_known_user is side-effect free)
