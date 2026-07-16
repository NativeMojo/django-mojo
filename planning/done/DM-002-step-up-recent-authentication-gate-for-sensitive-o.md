---
id: DM-002
type: feature
title: Step-up "recent authentication" gate for sensitive operations
priority: P2
effort: L
owner: backend
opened: 2026-06-07
depends_on: []
related: []            # session_revoke, user-self-service actions (test_user_actions)
links: []
---

# Step-up "recent authentication" gate for sensitive operations

## What & Why

Sensitive account operations (change username/email/phone, set/change password,
revoke sessions, enable/disable TOTP, regenerate recovery codes, add/remove
passkeys, deactivate/delete account, and admin credential edits on *other* users)
are currently gated only by **authorization** (model SAVE security: owner or
`users`/`manage_users`). A valid access token (default 6h TTL) is sufficient.

This leaves a **leaked-JWT** gap: an attacker who steals an access *or* refresh
token can perform account-takeover-grade actions for the life of the token —
change the recovery email/phone, disable 2FA, or revoke the real user's sessions.

We recently **removed** the `current_password` gate from the self-service actions
(it locked out passwordless passkey/SMS-OTP users and was a weak proxy anyway).
The correct replacement is **step-up / "recent authentication"** (a la GitHub
"sudo mode" / OIDC `auth_time`): sensitive ops require the *current token* to have
been minted from a genuine authentication event within the last `N` seconds.
A stolen long-lived token is "stale" for sensitive ops, forcing a re-auth the
attacker cannot complete. This works for **all** auth methods (password, passkey,
SMS/OTP, OAuth) and **applies to admins too** (an admin token has higher blast
radius). The feature must be **optional** (configurable window; `0`/off = today's
behavior).

## Acceptance Criteria

- [ ] A genuine authentication event stamps an `auth_time` (epoch seconds) claim
      into the issued token(s). Every login entry point sets it (none missed).
      **Stamping is unconditional / always-on** (harmless extra claim, no client
      impact) — it is NOT gated by the window. Only *enforcement* is gated.
- [ ] `auth_time` **survives a silent token refresh unchanged** — it is NOT reset
      to `now` on refresh, and NOT dropped.
- [ ] A `@requires_fresh_auth(seconds=...)` decorator (mojo decorator style) and/or
      a model-action equivalent rejects requests whose token `auth_time` is older
      than the window by raising a new **`ReauthRequiredException`** →
      `{status: False, error: "reauth_required", code: 440}` at **HTTP 440**.
      This is a DISTINCT third state from `403` (authorized: no/permission denied)
      and `401` (token invalid/expired) so the UI can branch to step-up — it must
      NOT reuse 401 (would trip the client 401→refresh path; a refresh preserves
      the stale `auth_time` and cannot fix freshness). The UI keys on code `440` /
      `error == "reauth_required"`.
- [ ] The window is configurable via `settings.get("FRESH_AUTH_WINDOW", ...)`;
      **default is `0` = disabled (off by default)** — on upgrade, behaves exactly
      as today; operators opt in once their UI handles the 440 step-up flow.
      **Global system-wide setting only — no per-group override in v1.** [DECIDED]
- [ ] The gate applies uniformly to the actor's own token, **including admins**
      acting on other users.
- [ ] A step-up re-auth path lets an already-logged-in user re-prove a factor
      (passkey assertion / fresh OTP / password) and receive a token with a fresh
      `auth_time` **without a full logout**, then retry the op.
- [ ] The defined "sensitive set" is enforced; read/non-sensitive ops are
      unaffected.
- [ ] `check_password` / `current_password` is NOT reintroduced as the gate
      (passwordless accounts must remain fully functional).
- [ ] Tests: stale token blocked, fresh token allowed, refresh preserves freshness
      (no reset), disabled-window bypasses gate, admin-on-other gated, step-up
      re-auth mints a fresh token and unblocks the op.
- [ ] Docs updated in both tracks; CHANGELOG entry.

## Investigation

### What exists (grounded recon, file:line)

- **Token builder** — `mojo/apps/account/utils/jwtoken.py`.
  `create_access_token(**kwargs)` (line 44) overrides `exp`/`token_type`/`iat`/`jti`
  but **passes through any other custom claim** → an `auth_time` kwarg is carried
  into the encoded token. `iat` is reset on every create (line 48), so **`iat`
  is unusable for freshness** — a dedicated `auth_time` claim is required.
  `refresh_access_token(refresh_token)` (line 64) does `create_access_token(**decoded)`
  → preserves custom claims. **BUT see the refresh-endpoint caveat below.**

- **`jwt_login`** — `mojo/apps/account/rest/user.py:595–654`. Token payload assembled
  at ~line 617 as `keys = dict(uid=user.id, ip=request.ip)` (+optional `device`),
  passed to `JWToken(...).create(**keys)`. **Single best place to stamp `auth_time`**
  for almost all login paths.

- **All genuine auth entry points** (callers of `jwt_login`, must all carry
  `auth_time`):
  - password login `user.py:194`; magic link `user.py:848`; email verify `user.py:890`;
    invite `user.py:912`; password reset `user.py:768,792`; email change `user.py:1171`;
    sessions revoke `user.py:1502`; auth exchange/handoff `user.py:241`;
    registration auto-login `user.py:400,532`
  - SMS (2FA + standalone) `sms.py:153`; TOTP `totp.py:165,206,254`;
    passkey `passkeys.py:247`; OAuth `oauth.py:357`
  - Centralizing the stamp inside `jwt_login` covers all of these in one edit.

- **⚠️ Refresh endpoint caveat** — `on_refresh_token()` `user.py:118–132` does **NOT**
  use `jwt_login` nor `refresh_access_token`; it calls
  `JWToken(user.get_auth_key()).create(uid=user.id)` **fresh, discarding the old
  token's claims**. To preserve `auth_time` across refresh this handler must decode
  the incoming refresh token, read its `auth_time`, and pass it through to `create()`
  — carrying the ORIGINAL value forward (not `now`). Getting this wrong either drops
  freshness (sensitive ops break after first refresh) or resets it (protection
  defeated). This is the highest-risk part of the change.

- **Auth middleware** — `mojo/middleware/auth.py:21–48`. Decodes/validates the JWT,
  sets `request.user` (line 46) and `request.auth_token = objict(prefix, token)`
  (line 39) — **stores the raw token only; the decoded claims are NOT attached**.
  A fresh-auth check must re-decode (`JWToken(key=user.get_auth_key()).decode(token,
  validate=False)`) to read `auth_time`, OR the middleware could be extended to
  attach the decoded payload (e.g. `request.jwt_claims`) once — preferable, decode-once.

- **Decorator pattern** — `mojo/decorators/auth.py:199–219` (`requires_auth`) and
  `:14–54` (`requires_perms`) show the wrapper style + `SECURITY_REGISTRY`
  registration to mirror for `@requires_fresh_auth(seconds=...)`.

- **Settings** — `from mojo.helpers.settings import settings; settings.get("NAME",
  default, kind="int")` (`helper.py:122–143`). Per-group auth config exists:
  `mojo/apps/account/services/auth_config.py` (`resolve_auth_config(group=...)`,
  ~line 146) with `theme`/`registration`/`login` sections — a `security`/`session`
  section could host a per-group window.

- **Error contract** — `mojo/errors.py`: exceptions carry `(reason, code, status)`;
  the dispatcher emits body `{status: False, error: <reason>, code: <code>}` at HTTP
  `<status>`. There is **no error-code registry** — codes are inline and today the
  body `code` always mirrors the HTTP status (`401/401`, `403/403`, `404/404` only).
  Add a new `ReauthRequiredException(MojoException)` here with `reason="reauth_required",
  code=440, status=440` (DECIDED — see AC). 403 already = permission denied, 401 =
  token invalid/expired (e.g. `sms.py` `"Invalid or expired MFA token", 401, 401`).

- **Existing step-up** — `mojo/apps/account/services/bouncer/enforcement.py:128–135`
  has a **risk-scored, flag-based** step-up (`bouncer_require_step_up`), fired on
  high risk. It is **orthogonal** (risk-triggered, not time-based) — the new
  time-based `auth_time` gate complements it; reuse the `reauth_required` /
  step-up event vocabulary where sensible. **No existing `auth_time` claim.**

### Constraints

- `request.DATA` only; no Python type hints; `logit` not stdlib logging.
- Do not reintroduce a password requirement (passwordless support is a hard rule).
- `auth_key`/`last_activity` remain non-writable via REST; freshness must live in
  the **token claim**, never in the `User.last_login` row (per-user mutable state
  would let an attacker's stale token pass when the victim logs in elsewhere).
- Backwards compatible: tokens minted before this ships have no `auth_time` —
  decide the treatment (grace/migration) in scope.

### Regression/test feasibility

High. testit can mint tokens with controlled `auth_time` (or monkeypatch the
clock window via settings), call sensitive endpoints, and assert allow/deny +
the refresh-preserves-freshness invariant. Server-isolation rules apply
(`th.server_settings` for `FRESH_AUTH_WINDOW`).

## Plan

### Goal
Add an optional, **off-by-default** "recent authentication" (step-up) gate: sensitive
account operations require the caller's JWT to have been minted from a genuine login
within a configurable window; when stale, return HTTP **440** `reauth_required`.
Works for passwordless accounts and applies to admins acting on others.

### Context — what exists (file:line)
- **Token builder** `mojo/apps/account/utils/jwtoken.py`:
  - `create_access_token(**kwargs)` (l.44–53) sets `exp/token_type/iat/jti` and
    **passes any other kwarg through into the token** → an `auth_time` kwarg lands
    in the encoded claims. `iat` is reset every call (l.48) → **`iat` is unusable
    for freshness**; a dedicated `auth_time` claim is required.
  - `refresh_access_token(refresh_token)` (l.64–68) does `create_access_token(**decoded)`
    — preserves custom claims. **But the REST refresh endpoint does NOT use this.**
- **`jwt_login`** `mojo/apps/account/rest/user.py:595–654`. Claims assembled ~l.617 as
  `keys = dict(uid=user.id, ip=request.ip)` (+optional `device`), then
  `JWToken(...).create(**keys)`. **Single stamp point for all login flows.**
- **All login entry points** route through `jwt_login`: password l.194, magic l.848,
  email-verify l.890, invite l.912, reset l.768/792, email-change l.1171,
  sessions-revoke l.1502, handoff l.241, register l.400/532; `sms.py:153`;
  `totp.py:165/206/254`; `passkeys.py:247`; `oauth.py:357`. Stamping inside
  `jwt_login` covers them all in one edit.
- **⚠️ Refresh endpoint** `on_refresh_token()` `user.py:118–132` does NOT use
  `jwt_login` or `refresh_access_token`; it calls
  `JWToken(user.get_auth_key()).create(uid=user.id)` **fresh, discarding old claims**.
  Must decode the incoming refresh token, read its `auth_time`, and pass that
  ORIGINAL value through to `create()` (carry forward; never reset to now, never drop).
- **Auth middleware** `mojo/middleware/auth.py:21–48`: sets `request.user` (l.46) and
  `request.auth_token = objict(prefix, token)` (l.39) — stores the raw token only;
  decoded claims are NOT attached. The gate helper re-decodes:
  `JWToken(key=request.user.get_auth_key()).decode(token, validate=False).auth_time`.
- **Decorator pattern** `mojo/decorators/auth.py:199–219` (`requires_auth`) and
  `:14–54` (`requires_perms`) — wrapper style + `SECURITY_REGISTRY` registration.
- **Errors** `mojo/errors.py`: `(reason, code, status)` → body
  `{status:False, error:<reason>, code:<code>}` @ HTTP `<status>`. No code registry.
- **Self-service actions** `mojo/apps/account/models/user.py:940+`
  (`on_action_change_username`, `on_action_revoke_sessions`, `on_action_disable_totp`,
  `on_action_confirm_totp`, `on_action_regenerate_totp_codes`) — now authorized purely
  by model SAVE security (owner or `users`/`manage_users`).
- **Endpoint gap (fold-in target)**: dedicated `auth/username/change` `user.py:1417`
  (`@md.requires_params("username","current_password")` + `has_usable_password()` +
  `check_password`) and `auth/sessions/revoke` `user.py:1474`
  (`@md.requires_params("current_password")` + `check_password`) still HARD-require a
  password → lock out passwordless users. (`email/change` l.945 & `phone/change` l.1291
  already treat `current_password` as optional — the good pattern.)
- **Settings** `from mojo.helpers.settings import settings; settings.get("NAME", 0, kind="int")`.

### Changes — what to do
1. **`mojo/errors.py`** — add `class ReauthRequiredException(MojoException)` with
   `__init__(reason="reauth_required", code=440, status=440)`.
2. **`mojo/apps/account/rest/user.py` `jwt_login` (~l.617)** — add
   `auth_time = int(time.time())` into the `keys` claims dict (ensure `time` import).
   **Unconditional** — every login flow now stamps it, regardless of the window.
3. **`mojo/apps/account/rest/user.py` `on_refresh_token` (l.131)** — decode the
   incoming refresh token; if it carries `auth_time`, pass it through to `create(...)`
   unchanged; if absent (legacy token), omit it. Never set to now.
4. **`mojo/apps/account/services/` (new) `fresh_auth.py`** — small service:
   - `token_auth_time(request)` → re-decode current JWT, return `auth_time` or `None`.
   - `is_fresh(request, seconds=None)` → `window = seconds if seconds is not None else
     settings.get("FRESH_AUTH_WINDOW", 0, kind="int")`; if `window <= 0` return `True`
     (gate disabled — full bypass); else `at = token_auth_time(request)`; return
     `at is not None and (int(time.time()) - at) <= window`.
   - `require_fresh(request, seconds=None)` → raise `ReauthRequiredException()` when
     not fresh. Used by both the decorator and the model actions.
   - **API-key / non-JWT auth:** if the request is not JWT-authenticated (no bearer
     JWT), treat as out of scope → return fresh/allow (machine creds bypass). [confirm]
5. **`mojo/decorators/auth.py`** — add `requires_fresh_auth(seconds=None)` mirroring
   `requires_auth`: wrapper checks `request.user.is_authenticated` then
   `fresh_auth.require_fresh(request, seconds)`; register in `SECURITY_REGISTRY`.
6. **Apply the gate to the sensitive set:**
   - **Model actions** `user.py:940+` — call `fresh_auth.require_fresh(self.active_request)`
     at the top of `on_action_change_username`, `on_action_revoke_sessions`,
     `on_action_disable_totp`, `on_action_confirm_totp`, `on_action_regenerate_totp_codes`.
   - **Dedicated endpoints** — decorate with `@md.requires_fresh_auth()`:
     `auth/username/change`, `auth/sessions/revoke`, `email/change`, `phone/change`,
     password change, TOTP enable/disable/regenerate, passkey add/remove, account
     deactivate/delete. (Exact list confirmed at build; reads never gated.)
7. **Fold-in the endpoint gap** — in `auth/username/change` (l.1417) and
   `auth/sessions/revoke` (l.1474): remove `requires_params("current_password")`, the
   `has_usable_password()` block, and the `check_password` gate; rely on
   `@md.requires_fresh_auth()` instead. Now functional for passwordless users.
8. **Step-up re-auth = reuse existing flows (no new endpoint).** Every existing
   verify/login flow already calls `jwt_login`, which now stamps a fresh `auth_time`.
   So "re-auth" is: client re-runs the appropriate existing flow for the logged-in
   user (passkey assertion / `sms/verify` / `totp/verify` / password login), swaps in
   the returned tokens, and retries the op. Document this; confirm each verify flow
   runs for an already-authenticated user and returns a usable token pair.
9. **Setting** — `FRESH_AUTH_WINDOW` default `0` (off). Document.

### Design decisions
- **`auth_time`, not `iat`** — `iat` resets on every refresh (jwtoken l.48), which
  would silently defeat the gate. `auth_time` is set only at genuine logins.
- **Stamp always-on, enforce gated, default OFF** — zero upgrade impact; no flag-day
  mass re-auth; when an operator enables the window later, tokens already carry
  `auth_time`. (Locked.)
- **HTTP 440 / `reauth_required`** — distinct third state vs 403 (no permission) and
  401 (token invalid/expired); avoids the client 401→refresh loop (a refresh keeps the
  stale `auth_time`). (Locked.)
- **Freshness lives in the token claim, never `User.last_login`** — a per-user row
  field would let an attacker's stale token pass when the real user logs in elsewhere.
- **Reuse existing verify flows for step-up** (no `/auth/stepup`). (Locked.)
- **Global system-wide setting only**, no per-group for v1. (Locked.)
- **Fold the username/change + sessions/revoke password gap into this item.** (Locked.)
- **Re-decode in a helper, not middleware** — gated ops are rare; re-decoding once
  per gated request keeps the change localized (no middleware/`validate_jwt` surgery).
- **Legacy tokens without `auth_time`** → treated as stale (fail-closed) once a window
  is enabled, forcing one re-auth. Safe because default-off and tokens rotate within
  the 7-day refresh TTL before anyone enables it.

### Edge cases & risks
- **Refresh dropping/resetting `auth_time`** — highest-risk; explicit test on the
  before/after decode across `on_refresh_token`.
- **Window = 0** must fully bypass (never emit 440).
- **Clock skew** — window is minutes; no leeway needed beyond integer seconds.
- **API-key / non-JWT requests** to a gated op — bypass (machine creds). [confirm]
- **Admin acting on another user** — the ADMIN's own token `auth_time` is checked
  (correct; the admin must be freshly authenticated).
- **Don't gate reads** — only the enumerated sensitive write ops.

### Tests  (testit — see `docs/django_developer/testit/Overview.md`)
- Login stamps `auth_time` in the issued token.
- **Refresh preserves `auth_time`** (decode before/after; not reset, not dropped).
- Window=0: sensitive op allowed even with an old `auth_time`.
- Window set: fresh token allowed; stale token → 440 `reauth_required`.
- Admin-on-other with a stale admin token → 440.
- Legacy token (no `auth_time`) + window set → 440.
- Step-up: after re-running a verify flow, `auth_time` is fresh and the op succeeds.
- Passwordless user can `change_username` and `revoke_sessions` (no password) via both
  the model action and the dedicated endpoint.
- Use `th.server_settings(FRESH_AUTH_WINDOW=...)` to flip the window per the
  server-isolation rule.

### Docs
- `docs/django_developer/` — `FRESH_AUTH_WINDOW` setting, `requires_fresh_auth`
  decorator + `fresh_auth` service, the `auth_time` claim, `ReauthRequiredException`/440,
  the stamp-always/enforce-gated model, step-up-via-existing-flows pattern.
- `docs/web_developer/` — the 440 `reauth_required` contract and how a client handles
  it (re-run a login flow, swap tokens, retry), the list of gated endpoints, and that
  `username/change` + `sessions/revoke` no longer require `current_password`.
- `CHANGELOG.md` entry.

### Open questions
- **API-key (non-JWT) callers** on a gated op: bypass (machine cred) or block? Lean
  bypass — confirm at build.
- **Exact sensitive-set membership** — confirm the final endpoint list at build
  (proposed list in Change #6).

## Notes

### Test baseline (recorded per .claude/rules/build-baseline.md)
- **Default suite** (`bin/run_tests --agent`): GREEN — 2190 passed / 0 failed
  (381 skipped), before and after this change. This is the project's "100% green".
- **`--full` (opt-in `test_security`) is RED at HEAD, pre-existing, NOT from DM-002:**
  - `public_endpoints_security`, `route_security_comprehensive`,
    `generate_security_report` crash on `SECURITY_REGISTRY['…on_user_login']['type']`
    KeyError — `on_user_login` has a geofence-only registry entry (no `type`), added
    by commit `24bbd4e` (geofence engine). Verified ZERO `fresh_auth`-typed registry
    entries exist (this decorator is always inner to `requires_auth`), so DM-002
    provably does not alter the security registry.
  - `pii_anonymize: PII fields are cleared` — separate pre-existing `test_security` issue.
  - `auth/exchange rate-limit` — fails only in the parallel `--full` run (shared
    rate-limit counters); passes default/isolated. Flake, not DM-002.
- DM-002's own tests all pass: `test_auth.fresh_auth`, `test_user_mgmt.username_change`,
  `test_security.session_revoke`, `test_account.test_user_actions`.

### Decisions locked (scoping)
- **Re-auth signal:** new `ReauthRequiredException` → `{error:"reauth_required", code:440}`
  at HTTP 440 — distinct from 403 (no permission) / 401 (token invalid/expired). ✅
- **Default = OFF (`FRESH_AUTH_WINDOW=0`):** no behavior change on upgrade; opt-in. ✅
- **Stamp vs enforce, decoupled:** `auth_time` is stamped unconditionally on every
  token from day one (harmless, no client impact); only enforcement is gated by the
  window. Avoids any transition gap when an operator later enables it. ✅

- **Config scope:** global system-wide setting only — no per-group in v1. ✅
- **Step-up mechanism:** reuse existing verify/login flows — no new `/auth/stepup`. ✅
- **Endpoint gap:** folded in — `auth/username/change` + `auth/sessions/revoke` lose
  their hard `current_password` requirement and move behind the fresh-auth gate. ✅
- **Legacy tokens** without `auth_time`: fail-closed (stale → one re-auth) once a
  window is enabled. ✅

All four scoping decisions are locked. Two minor build-time confirmations remain
(see Plan → Open questions): API-key callers bypass vs block, and final sensitive
endpoint list.

---

Origin: surfaced 2026-06-07 while fixing the `revoke_sessions` / `change_username`
self-service regression (untracked commit `073ac3e "more power to admins"` had
stripped the guards). After removing the broken `current_password` gate, the user
proposed recent-auth as the correct, passwordless-friendly mitigation for leaked
JWTs — explicitly including admins.

## Resolution
- closed: 2026-06-07
- branch: main
- files changed: mojo/errors.py, mojo/apps/account/services/fresh_auth.py (new),
  mojo/decorators/auth.py, mojo/decorators/http.py, mojo/apps/account/rest/user.py,
  mojo/apps/account/models/user.py, mojo/apps/account/rest/passkeys.py,
  mojo/apps/account/rest/totp.py, plus docs (account step_up_auth.md ×2 + READMEs,
  testit/Overview.md, settings_reference.md, email_change.md, phone_change.md,
  user_self_management.md, authentication.md, user.md) and CHANGELOG.md 
- tests added: tests/test_auth/fresh_auth.py (new — unit + over-the-wire: 440 gate,
  refresh-preserves-freshness, admin-on-other, passwordless, API-key bypass);
  rewrote tests/test_security/session_revoke.py and tests/test_user_mgmt/username_change.py
  to the passwordless contract
