# Failed Login Throttling — Per-Account & Bypass-Resistant Timing Caps

**Type**: request
**Status**: resolved
**Date**: 2026-05-01
**Priority**: high
**Resolved**: 2026-05-01

## Description

Add bypass-resistant timing caps to the login flow so a single attacker cannot grind passwords against a known username indefinitely. Today the `/login` endpoint has only IP- and duid-scoped rate limits (both bypassable) and no per-account lockout, and the `invalid_password` incident category does not trigger any rule. As a result, an attacker who knows one real username can rotate IPs and omit/rotate the client-supplied `duid` to keep guessing without limit.

## Context

Reported by Ian (2026-05-01) as: "check if user failed logins has a timing cap, or can someone keep trying over and over?" The investigation below confirmed that the answer is *yes, they can keep trying* against a known account. This request captures the gaps so they can be planned together rather than patched piecemeal.

The codebase already has the building blocks — `strict_rate_limit`, the incident `RuleSet` engine with IP-block handlers, and `report_incident` calls on every failed login. The fixes are primarily about wiring those pieces together at appropriate thresholds, plus adding a per-account counter that survives IP rotation.

## Acceptance Criteria

- A single attacker with one known username **cannot** make more than a configurable number of failed-password attempts against that account within a configurable window, regardless of IP/duid rotation.
- An attacker spraying many usernames from a single IP cannot exceed the existing IP cap (already enforced; no regression).
- The `duid` rate-limit branch cannot be bypassed by simply omitting `duid` from the request.
- The `invalid_password` event category drives an enforcement rule (IP block, account cooldown, or both) once a threshold is crossed.
- TOTP / passkey verification endpoints enforce a strict rate limit so MFA cannot be brute-forced post-password.
- Legitimate users who mistype a password a small number of times are **not** locked out — the threshold has to be tuned to attack patterns, not casual mistakes.
- `manage_users` admins can clear the per-account counter (mirrors the existing `auth/manage/clear_rate_limit` pattern).
- Behavior is documented in both `docs/django_developer/account/` and `docs/web_developer/account/`, and recorded in `CHANGELOG.md`.

## Investigation

### What exists

- **Login endpoint**: `mojo/apps/account/rest/user.py:60-85` — `on_user_login`. Decorated with:
  - `@md.strict_rate_limit("login", ip_limit=100, duid_limit=10, duid_window=300)` — sliding-window IP and duid caps.
  - `@md.endpoint_metrics("login_attempts", by=["ip", "duid"])`.
  - `@md.requires_bouncer_token('login')` — defaults to log-only (`BOUNCER_REQUIRE_TOKEN=False`).
- **Failed-password reporting**: `mojo/apps/account/rest/user.py:79-81` calls `user.report_incident("...", "invalid_password")` with **default `level=1`**. Class-level call for unknown usernames at line 73-77 uses `level=8`.
- **Rate-limit primitives**: `mojo/decorators/limits.py` — `rate_limit` (fixed window) and `strict_rate_limit` (sliding window) both keyed on IP, duid, and api_key. `_get_dimension` (line 110) reads `duid` from `request.DATA`, so missing/rotating duid skips that branch.
- **Incident rules**: `mojo/apps/incident/models/rule.py:466-489` — `ensure_auth_rules()` ships a `login:unknown` ruleset that IP-blocks at level ≥8 after 15-min bundle. **No ruleset exists for `invalid_password`**, so level-1 events fall through to the catch-all and never trigger enforcement.
- **Block handler**: `block://?ttl=…&fleet_wide=…` already exists and is used by the bouncer/ossec rulesets, so an `invalid_password` rule could reuse it.
- **Admin clear**: `mojo/apps/account/rest/user.py:31-40` — `auth/manage/clear_rate_limit` clears Redis rate-limit keys for an IP/key/duid. No equivalent for clearing a per-account counter (because that counter doesn't exist yet).

### What changes (file-level breakdown)

- **`mojo/apps/account/rest/user.py`** — extend `on_user_login`:
  - After resolving the user but before/after `check_password`, increment a per-username (or per-user-id) counter in Redis with a sliding window. Reject with 429 once threshold crossed.
  - On success, reset the counter.
  - Bump the level on `invalid_password` `report_incident` from default 1 to a value (likely 5–7) that lets a new ruleset match it.
- **`mojo/decorators/limits.py`** — likely add a per-account dimension helper, or add a new `strict_rate_limit` parameter (`username_limit`, `username_window`) so the same Redis-backed sliding-window machinery can key on the resolved user. Decision belongs in the design phase. Also harden the duid branch — either require duid for the login endpoint or fall back to another stable per-client identifier when duid is missing.
- **`mojo/apps/incident/models/rule.py`** — add an `invalid_password` ruleset in `ensure_auth_rules()` (IP block after N events in M minutes, similar shape to the existing `login:unknown` rule). Decide whether `invalid_password` warrants a level bump in the reporter call instead of/in addition to a custom ruleset.
- **`mojo/apps/account/rest/totp.py`** and **`mojo/apps/account/rest/passkeys.py`** — add `@md.strict_rate_limit(...)` to the public verify endpoints (`auth/totp/verify`, `auth/totp/login`, `auth/totp/recover`, `auth/passkeys/login/complete`). Today they have none.
- **`mojo/apps/account/rest/user.py:31-40`** — extend `auth/manage/clear_rate_limit` to also clear the per-account counter when a `username` (or `user_id`) is supplied.
- **Docs**: update `docs/web_developer/account/user_self_management.md` (already lists the auth event categories) and the corresponding `docs/django_developer/` files. Add a short "Failed Login Protection" section explaining the layered controls (IP / duid / per-account / incident-rule). Update `CHANGELOG.md`.
- **Tests**: see the *Tests Required* section.

### Constraints

- **Don't lock real users out for normal mistakes**. Thresholds must be high enough that a legitimate user who mistypes 3–5 times is unaffected, but low enough to stop sustained brute-forcing. The strict_rate_limit sliding window is the right primitive (existing `login:unknown` uses 15-min bundle / 30-min IP block as a reference point).
- **Permission boundary intact**: cleared-counter endpoint stays gated on `manage_users`.
- **Fail-closed on Redis errors only where safe**: today `strict_rate_limit` logs the error and lets the request through (`limits.py:270-272`). For a per-account guard we should keep that behavior to avoid taking auth offline if Redis is down — but this is a design tradeoff worth confirming.
- **Don't leak account existence**: the per-account counter must not change the response shape between "username unknown" and "username known with bad password". Both still return the same generic 401.
- **`muid` vs `duid`**: `request.muid` is server-set and reliable in `_report_token_event`. Consider keying the per-client cap on muid instead of duid to remove client control.
- **Backwards compatibility**: existing IP/duid limits stay; new caps are additive.

### Related files

- `mojo/apps/account/rest/user.py` (login endpoint, admin clear)
- `mojo/apps/account/rest/totp.py` (MFA verification — currently unrate-limited)
- `mojo/apps/account/rest/passkeys.py` (passkey verification — currently unrate-limited)
- `mojo/decorators/limits.py` (rate-limit primitives)
- `mojo/decorators/bouncer.py` (existing layered guard, reference pattern)
- `mojo/apps/incident/models/rule.py` (`ensure_auth_rules`)
- `mojo/apps/incident/models/event.py` (`AUTH_FAILURE_CATEGORIES`, security summaries)
- `mojo/apps/account/models/user.py:608-615` (the other `invalid_password` call site — current-password check on `set_new_password`)
- `tests/test_account/` (existing login tests — pattern reference)

## Endpoints

This request modifies behavior on existing endpoints; no new endpoints are introduced. The admin-clear endpoint gains an optional new parameter.

| Method | Path | Description | Permission |
|---|---|---|---|
| POST | `/login` (and aliases `/auth/login`, `/account/jwt/login`) | Adds per-account failed-attempt cap and missing-duid handling | public (existing) |
| POST | `/auth/totp/verify`, `/auth/totp/login`, `/auth/totp/recover` | Add strict rate limit | public (existing) |
| POST | `/auth/passkeys/login/complete` | Add strict rate limit | public (existing) |
| POST | `/auth/manage/clear_rate_limit` | Accept `username` / `user_id` to clear per-account counter | `manage_users` (existing) |

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `LOGIN_USERNAME_LIMIT` | TBD (suggest 10) | Max failed password attempts per resolved username within window |
| `LOGIN_USERNAME_WINDOW` | TBD (suggest 900) | Sliding window in seconds for the per-username cap |
| `MFA_VERIFY_IP_LIMIT` | TBD (suggest 10) | Max TOTP/passkey verify attempts per IP per minute |
| `MFA_VERIFY_IP_WINDOW` | TBD (suggest 60) | Window in seconds |

Final defaults to be agreed during design.

## Tests Required

- Login: 10 failed attempts in a row from rotating IPs against a single known username → 11th request returns 429 (per-account cap kicks in).
- Login: 10 failed attempts spread across many usernames from a single IP → IP cap (already enforced) trips at 100, per-account cap does not.
- Login: omitting `duid` does not let an attacker exceed the per-account cap.
- Login: a successful login resets the per-account counter.
- Login: legitimate user mistyping password 3–5 times is *not* blocked (regression guard).
- Login: response body for "wrong password (counter not yet tripped)" and "wrong password (counter tripped)" do not leak account existence — both surfaces remain generic 401, except the explicit 429 once the limit is hit.
- Login: `invalid_password` events at the new level trigger the new IP-block ruleset after threshold.
- TOTP: 11 wrong codes from one IP in a minute → 429.
- Passkey: 11 invalid login completes from one IP in a minute → 429.
- Admin: `auth/manage/clear_rate_limit` with `username=...` clears the per-account counter; without `manage_users`, returns 403.
- Redis-down behavior: with Redis unavailable, login still works (no auth outage).

## Open design questions (for `/design`)

1. **Per-account key**: hash username, hash email, or use resolved `user.id`? `user.id` survives username changes but requires resolving the user before the cap check, which the current code already does.
2. **Lockout vs throttle**: should the per-account cap return 429 with `Retry-After` (current `_block` behavior, lets the user back in after window), or set a flag on the User row that an admin must clear? Strongly suggest the former — simpler, no schema change, mirrors existing primitives. Confirm.
3. **Event level for `invalid_password`**: bump to 6–7 in `report_incident` *and* add a custom ruleset, or leave the level alone and write a ruleset that matches on category=`invalid_password` regardless of level? The latter is cleaner.
4. **MFA scope**: include TOTP/passkey rate limits in this request, or split into a follow-up? Recommend including — same risk class, same primitives, low marginal cost.
5. **muid vs duid for client identity**: use server-set `muid` for the second-tier (post-IP) cap so the client can't bypass it by omitting duid?

## Out of Scope

- CAPTCHA / proof-of-work challenges on the login form. The bouncer-token flow already exists for that and is on its own opt-in path.
- Per-user/per-tenant configurable thresholds. Global settings only for now.
- Notifying the user of suspicious login activity (separate concern; partially covered by `UserLoginEvent`).
- Refactoring the existing `rate_limit` / `strict_rate_limit` decorator API beyond what is needed here.
- Reworking the bouncer-token enforcement default (`BOUNCER_REQUIRE_TOKEN`).

## Plan

**Status**: planned
**Planned**: 2026-05-01

### Objective
Add a per-resolved-user failed-login counter (bypass-resistant), replace the client-controlled `duid` tier with the server-set `muid`, wire `invalid_password` events to an IP-blocking ruleset, and add strict rate limits to the previously-unprotected MFA / passwordless verify endpoints.

### Layered defense (final shape)

| Tier | Key | Limit | Where | Effect |
|---|---|---|---|---|
| 1 | IP | 100 / 60s (existing) | login decorator | 429 |
| 2 | `request.muid` (server cookie) | 10 / 300s (new — replaces duid tier) | login decorator | 429 |
| 3 | resolved `user.id` | 10 / 900s (new) | inside view, helper call | 429 |
| 4 | incident rule on `invalid_password` (level≥5) | 5 events / 15min → block IP 30min | RuleSet | fleet-wide IP block |
| 5 | strict_rate_limit IP | 10 / 60s (new) | TOTP / passkey verify decorators | 429 |

### Steps

1. `mojo/decorators/limits.py` — extend `strict_rate_limit` and `rate_limit` so the second-tier dimension can read `request.muid` instead of (or in addition to) `request.DATA.get("duid")`.
   - Add `muid_limit` / `muid_window` params alongside the existing `duid_*` params; both are optional and additive.
   - Add public helper `check_account_attempt(key, account_id, limit, window) -> (count, allowed)` that reuses `_check_sliding` and the same `_block` shape so view-level callers get identical 429 responses (with `Retry-After`, metric, incident).
   - Add `clear_rate_limits(account_id=...)` branch that wipes `srl:{key}:account:<id>` (and `rl:` equivalent) so the admin endpoint can clear it.
   - Keep existing fail-open-on-Redis-error behaviour.

2. `mojo/apps/account/rest/user.py` — rewire `on_user_login`:
   - Replace decorator: `@md.strict_rate_limit("login", ip_limit=100, muid_limit=10, muid_window=300, duid_limit=10, duid_window=300)` (keep duid for clients that still send it; primary trust is muid).
   - After resolving `user` (line 71) but before `check_password`: call `check_account_attempt("login", user.id, settings.LOGIN_USERNAME_LIMIT, settings.LOGIN_USERNAME_WINDOW)`. If not allowed → return the same 429 shape as the decorator.
   - On `check_password` failure: increment per-user counter (one zadd via the helper) and report `invalid_password` at `level=5` (instead of default 1) so the new ruleset matches.
   - On `check_password` success: clear the per-user counter (`r.delete("srl:login:account:<id>")`).
   - Update `endpoint_metrics(by=["ip", "duid"])` → `by=["ip", "muid"]` (duid often missing now anyway).

3. `mojo/apps/account/rest/user.py:31-40` — extend `on_clear_rate_limit`:
   - Accept optional `user_id` and `username`. Resolve username to id and call `clear_rate_limits(account_id=...)`.
   - Permission unchanged (`manage_users`).

4. `mojo/apps/incident/models/rule.py` — add a new ruleset in `ensure_auth_rules()` between the existing two:
   ```python
   cls._create_ruleset(
       category="invalid_password",
       name="Auth - Password Brute Force",
       priority=5,
       match_by=MatchBy.ALL,
       bundle_by=BundleBy.SOURCE_IP,
       bundle_minutes=15,
       trigger_count=5,
       trigger_window=15,
       handler="block://?ttl=1800&fleet_wide=1",
       rules=[{"name": "Level >= 5", "field_name": "level",
               "comparator": ">=", "value": "5", "value_type": "int"}],
   )
   ```
   Safe re-run via `get_or_create`.

5. `mojo/apps/account/models/user.py:608-615` — leave the `set_new_password` call site alone for the failed-login counter (different surface — already-authenticated user changing their password). It still emits `invalid_password`, but at the default low level so it does not feed the new IP-block rule. Add a clarifying one-line comment explaining the level choice.

6. `mojo/apps/account/rest/totp.py` — add decorators to public verify entry points:
   - `on_totp_verify`: `@md.strict_rate_limit("totp_verify", ip_limit=10, ip_window=60)`
   - `on_totp_recover`: `@md.strict_rate_limit("totp_recover", ip_limit=10, ip_window=60)`
   - `on_totp_login`: `@md.strict_rate_limit("totp_login", ip_limit=10, ip_window=60)` and pipe through the same per-account check as `on_user_login` (since this is a passwordless flow).

7. `mojo/apps/account/rest/passkeys.py` — add `@md.strict_rate_limit("passkey_login", ip_limit=10, ip_window=60)` on `on_passkeys_login_complete`. (Begin endpoint stays unrated — it's challenge issuance, not a guess.)

8. `mojo/helpers/settings.py` (or wherever defaults live) — register new settings keys:
   - `LOGIN_USERNAME_LIMIT = 10`
   - `LOGIN_USERNAME_WINDOW = 900`
   - `MFA_VERIFY_IP_LIMIT = 10`
   - `MFA_VERIFY_IP_WINDOW = 60`
   Read via `settings.get(...)` at decorator/helper call time.

9. `docs/django_developer/account/auth.md` — append a "Failed Login Protection" section explaining the five-tier defense, the new settings, and how admins clear a counter. Update the existing "Login" snippet.

10. `docs/web_developer/account/user_self_management.md` — note that the login endpoint now returns 429 with `Retry-After` after sustained failures against one account, separate from existing IP-level 429s.

11. `docs/django_developer/core/rate_limiting.md` — document the new `muid_limit` / `muid_window` params and the `check_account_attempt` helper.

12. `CHANGELOG.md` — entry under unreleased: per-account login throttling, MFA verify rate limits, `invalid_password` enforcement rule.

### Design Decisions

- **Per-account key by `user.id`, not username** — survives username/email/phone changes, and the user is already resolved at this point in the view (no extra query). Redis key: `srl:login:account:<user.id>`.
- **Lockout = sliding-window throttle, not a User schema flag** — no migration, mirrors all other guards, auto-recovers after window. Admin can override via the existing clear endpoint.
- **`muid` replaces `duid` as the second tier** — `request.muid` is HttpOnly, server-set, and re-issued automatically per request when missing, so it's always present and not directly forgeable from JS. `duid` stays as an optional additive tier for legacy clients but is not load-bearing.
- **`invalid_password` level bumped from 1 to 5** — the ruleset matches by level, so a single bump opens enforcement without inventing a new severity convention. `set_new_password` keeps level 1 (different surface, already-authenticated).
- **Per-account check inside the view, not the decorator** — the decorator runs before user resolution and would need to look the user up itself, duplicating work. A helper call from the view keeps responsibility clean.
- **Generic 401 maintained until threshold** — only when the per-account counter is *already over the limit* does the view short-circuit to 429. A first wrong password still returns 401, identical to "unknown user" — no oracle.
- **MFA standalone TOTP login (`/auth/totp/login`)** — gets the same per-account check as password login, since it's also a guess against a known username.
- **Fail-open on Redis error** — preserves the existing contract of all other rate limits. A Redis outage must not lock everyone out of auth.

### User cases

- **Legit user mistypes 3-4 times then gets it right** → IP/muid/account counters all under threshold; success clears the per-account counter; no impact.
- **Legit user mistypes 10+ times** → 429 from the account tier with `Retry-After=900`; admin can clear via `auth/manage/clear_rate_limit?username=...`. (15min wait is the tradeoff for security; documented.)
- **Attacker with rotating IPs against one known username** → tier 3 catches them at 10 attempts; counter resets only on a real success.
- **Attacker spraying many usernames from one IP** → tier 1 (IP=100/min) catches them; tier 4 ruleset blocks the IP fleet-wide once `invalid_password` events accumulate.
- **Attacker omitting cookies entirely** → muid is regenerated each request (so tier 2 doesn't fire on its own), but tier 1 still applies, and as soon as the attacker picks any one real username, tier 3 fires.
- **Attacker using a stolen muid + a real username from many IPs** → tier 2 catches at 10/5min on muid; tier 3 catches at 10/15min on user.id. Whichever fires first.
- **Brute force MFA after correct password** → tier 5 catches at 10/min per IP.
- **Brute force passkey login** → tier 5 catches at 10/min per IP.
- **Admin needs to unblock a stuck user** → `POST /api/auth/manage/clear_rate_limit` with `username` (or `user_id`); manage_users permission required.

### Edge Cases

- **First-request flow when `muid` cookie is missing** — middleware mints a fresh muid before the view runs, so tier 2 always sees a value. New muids start with count=0; not a bypass because tier 1 (IP) and tier 3 (account) still apply.
- **Phone-as-username login** — `lookup_from_request_with_source` resolves the user; counter is keyed on the resolved `user.id` regardless of which field was used.
- **Successful login resets the counter** — explicit `r.delete(...)` after `jwt_login` succeeds. If the user resolves but `check_password` returns True, the previous fails are wiped.
- **MFA challenge response path** — when password is correct but MFA is required (`mfa_required_response`), reset the per-account counter at the same point as a full success. The MFA verify endpoint handles its own (IP) rate limit.
- **`set_new_password` with wrong `current_password`** — keeps `level=1`, does not feed the IP-block rule, does not increment the login counter (different endpoint).
- **Redis unavailable** — both decorator and helper return allow-through. `error.log` records the failure. Documented behaviour, no surprise.
- **Concurrent requests** — sliding-window zadd with unique-per-call timestamp avoids race windows; the existing primitive is already correct under concurrency.
- **Ruleset re-run safety** — `_create_ruleset` uses `get_or_create`; existing deployments that already have the row keep it (no schema-only changes).
- **Account-existence oracle** — pre-threshold response stays 401 generic; over-threshold response is 429. An attacker who pushes a username to 429 *does* learn that user exists, but only after burning ~10 wrong guesses against it from one IP/muid/etc., which is the point of the limit. Acceptable.

### Testing

Tests use testit. Per CLAUDE.md `bin/run_tests --agent -t test_module.filename`. New file:

- `tests/test_auth/login_throttling.py`:
  - `setup_login_throttle_users` — clear rate limits for 127.0.0.1 and known test usernames; create a `throttle_user` and a `spray_user1..N`.
  - `test_per_account_cap_blocks_at_threshold` — 10 wrong passwords for `throttle_user` → 429 on attempt 11. Use `clear_rate_limits(ip=…)` between to neutralize the IP tier so the assertion is about the account tier specifically.
  - `test_per_account_cap_clears_on_success` — N-1 wrong, 1 correct, then N-1 more wrong → still allowed (counter reset).
  - `test_per_account_cap_independent_of_duid` — submit 11 attempts each with a fresh `duid` → still 429 (proves duid isn't load-bearing).
  - `test_legit_mistype_3_passes` — 3 wrong, 1 correct → 200.
  - `test_unknown_username_does_not_lock_account` — login with bogus username → counts toward IP tier only, not any account.
  - `test_invalid_password_level_emits_event_for_rule` — make 5 wrong attempts, query `Event.objects.filter(category="invalid_password", level__gte=5).count() >= 5`.
  - `test_response_shape_pre_threshold_unchanged` — first wrong password returns 401 with the existing message body (no leakage).
  - `test_admin_clear_per_account_counter` — trip the cap, then admin POSTs `auth/manage/clear_rate_limit` with `username=...`; next attempt allowed.
  - `test_admin_clear_requires_manage_users` — non-admin → 403.
  - `test_redis_outage_login_still_works` — patch `get_connection` to raise; login still succeeds (smoke test of fail-open).
- `tests/test_mfa/totp_throttle.py`:
  - `test_totp_verify_rate_limit` — 10 wrong codes from one IP → 11th gets 429.
  - `test_totp_recover_rate_limit` — same shape, recover endpoint.
  - `test_totp_login_per_account_cap` — passwordless TOTP login also throttled per-account.
- `tests/test_mfa/passkey_throttle.py` (or extend existing) — `test_passkey_login_complete_rate_limit` — 10 invalid completions → 429.
- `tests/test_incident/auth_brute_force_rule.py` — verify the new `invalid_password` ruleset is created by `ensure_auth_rules()` and matches a level-5 event by category.

Each `assert` carries a descriptive failure message per project rules.

### Docs

- `docs/django_developer/account/auth.md` — new "Failed Login Protection" subsection covering all five tiers, settings, admin clear flow.
- `docs/web_developer/account/user_self_management.md` — note 429 on per-account threshold; tell consumers to honor `Retry-After`.
- `docs/django_developer/core/rate_limiting.md` — document `muid_limit` / `muid_window` params and `check_account_attempt` helper.
- `CHANGELOG.md` — single entry summarizing the throttling, MFA caps, and the `invalid_password` rule.

## Resolution

**Status**: resolved
**Date**: 2026-05-01
**Commits**: 6ee73b2 (feature), cd77568 (docs + security follow-up)

### What Was Built
A 5-tier login defense that prevents single-attacker password grinding against a known username regardless of IP/duid/cookie rotation:

| Tier | Key | Limit | Where |
|---|---|---|---|
| 1 | IP | 100 / 60s (existing) | login decorator |
| 2 | `request.muid` (server cookie) | 10 / 300s (new — replaces duid as primary 2nd tier) | login decorator |
| 3 | resolved `user.id` | 10 / 900s (new) | view-level helper |
| 4 | `invalid_password` events at level ≥ 5 | 5 events / 15min → fleet-wide IP block 30min | RuleSet |
| 5 | TOTP / passkey verify IP cap | 10 / 60s (new) | decorator |

### Files Changed
- `mojo/decorators/limits.py` — added `muid_limit`/`muid_window` params on `rate_limit` and `strict_rate_limit`; added `check_account_attempt()` helper for view-level per-account sliding-window throttling (fail-open on Redis error); extended `clear_rate_limits()` with `muid=` and `account_id=` args.
- `mojo/apps/account/rest/user.py` — `on_user_login` now uses muid as primary 2nd tier (duid optional/additive), runs the per-account check before `check_password`, bumps `invalid_password` events to level=5, clears the per-account counter on success/MFA-required. `on_clear_rate_limit` accepts `username` / `user_id`.
- `mojo/apps/incident/models/rule.py` — `ensure_auth_rules()` registers an `invalid_password` ruleset (block IP fleet-wide for 30min after 5 events at level ≥ 5 in 15min).
- `mojo/apps/account/rest/totp.py` — `strict_rate_limit` decorators on `auth/totp/verify`, `auth/totp/recover`, `auth/totp/login`; per-account check on `totp/login`.
- `mojo/apps/account/rest/passkeys.py` — `strict_rate_limit` on `auth/passkeys/login/complete`.
- `testit/client.py` — `RestClient.login()` now also clears the per-account counter and the muid counter for the current cookie so test suites that issue many logins from one client are not throttled by limits intended for sustained attack patterns.

### Tests
- `tests/test_auth/login_throttling.py` — 10 tests covering: per-account cap trips at threshold; success clears the counter; cap is independent of duid; legitimate mistype is not blocked; unknown-username spray does not lock real accounts; pre-threshold 401 shape unchanged; `invalid_password` events emit at level ≥ 5; ruleset registered; admin clear by username works; admin clear requires `manage_users`.
- `tests/test_mfa/zz_throttle.py` — 3 tests verifying TOTP verify, TOTP recover, and passkey login-complete each return 429 after 10 IP-scoped attempts.

  Run: `bin/run_tests --agent -t test_auth.login_throttling -t test_mfa.zz_throttle` — all 13 pass.

  Wider regression check: `test_auth` (71) + `test_mfa` (45) all pass.

### Docs Updated
- `docs/django_developer/account/auth.md` — appended "Failed Login Protection" section covering all 5 tiers, the new settings, and the admin clear flow.
- `docs/django_developer/core/rate_limiting.md` — documented `muid_limit`/`muid_window` decorator params and the `check_account_attempt` helper, plus the new `clear_rate_limits` args.
- `docs/web_developer/account/authentication.md` — added "Login Rate Limiting" section noting the new 429 + `Retry-After` semantics.
- `CHANGELOG.md` — single entry summarising the throttling, MFA caps, and the `invalid_password` rule.

### Security Review
One MEDIUM finding was an accepted tradeoff (post-threshold 429 vs 401 reveals account existence — documented in the brief). Two LOW findings: admin clear endpoint reveals unknown-username via error message (gated behind `manage_users`), and a minor double-count in the `clear_rate_limits` muid branch — the latter was fixed in commit cd77568. No injection or auth-bypass concerns.

### Follow-up
- The pre-existing `auth/exchange is rate-limited` test in `tests/test_auth/handoff.py` is timing-sensitive in full-suite parallel mode and now flakes occasionally (passes solo, sometimes fails under heavy load). Not a feature regression — the test relies on issuing 21+ requests within a 60s sliding window, which has narrow margin. Loosening the test loop or window is out of scope for this request.
- Final defaults agreed: `LOGIN_USERNAME_LIMIT=10`, `LOGIN_USERNAME_WINDOW=900`, `MFA_VERIFY_IP_LIMIT=10`, `MFA_VERIFY_IP_WINDOW=60`. Customers can override per deployment.
