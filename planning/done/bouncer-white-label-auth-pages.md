# White-Label Auth Pages — Per-Group Branding in Bouncer Views

**Type**: request
**Status**: planned
**Priority**: high
**Date**: 2026-04-11

## Description

Bouncer auth views (login, register, password reset) currently load branding from global Django settings (`AUTH_LOGO_URL`, `AUTH_APP_TITLE`, `AUTH_CUSTOM_CSS`, etc.). These need to resolve **per-group** so that multi-tenant platforms can serve operator-branded auth pages through a single REDACTED deployment.

This is the foundational piece enabling REDACTED to act as a white-labeled Identity Provider (like Auth0/Clerk) for downstream platforms like WMX — where each operator (e.g., "Club AXO") has their own branded login/register experience hosted by REDACTED.

## Context

- WMX is a multi-tenant gaming SaaS platform. Operators run branded casino sites (e.g., "Club AXO"). Players never see "WMX" or "REDACTED".
- REDACTED already has the full auth stack: Google OAuth, Apple OAuth, SMS, email, passkeys, bouncer.
- Rather than every downstream platform rebuilding auth, REDACTED will serve white-labeled auth pages per operator via OAuth redirect.
- The `Setting` model already supports group-scoped resolution with parent chain fallback (`Setting.resolve(key, group=...)`), but bouncer views don't use it — they call `settings.get()` globally.
- Auth page templates already accept branding variables (`AUTH_LOGO_URL`, `AUTH_HERO_IMAGE_URL`, `AUTH_CUSTOM_CSS_URL`, etc.) — they just need to be loaded per-group instead of globally.

## Investigation

### What exists

- `mojo/apps/account/rest/bouncer/views.py` — `_auth_context()` (lines ~178-205) builds template context from global settings
- `mojo/apps/account/templates/account/auth_base.html` — base template consuming branding variables
- `mojo/apps/account/models/setting.py` — `Setting.resolve(key, group=..., default=...)` already supports group-scoped lookup with parent fallback
- `mojo/apps/account/models/group.py` — Group model with `metadata` JSONField

### What needs to change

1. **`_auth_context()` in bouncer/views.py** — accept a `group` parameter and resolve all `AUTH_*` settings via `Setting.resolve(key, group=group)` instead of `settings.get()`
2. **Group detection** — determine the operator group from the incoming request. Three mechanisms:
   - **OAuth client_id** — if the request includes `client_id` query param (OAuth flow), look up the OAuthClient's group
   - **Custom domain/subdomain** — map request hostname to a group (e.g., `auth.clubaxo.com` → Club AXO group)
   - **Explicit group param** — `?group=clubaxo` as fallback
3. **Fallback chain** — if no group detected or group has no override for a setting, fall back to global defaults (this is already how `Setting.resolve` works)

### Branding settings that need per-group resolution

| Setting | Purpose |
|---|---|
| `AUTH_LOGO_URL` | Operator logo on auth pages |
| `AUTH_FAVICON_URL` | Browser tab icon |
| `AUTH_APP_TITLE` | Page title / brand name |
| `AUTH_HERO_IMAGE_URL` | Hero image on login page |
| `AUTH_HERO_HEADLINE` | Hero headline text |
| `AUTH_HERO_SUBHEADLINE` | Hero subheadline text |
| `AUTH_BACK_TO_WEBSITE_URL` | "Back to site" link |
| `AUTH_TERMS_URL` | Terms & conditions URL |
| `AUTH_CUSTOM_CSS_URL` | External CSS for full theming |
| `AUTH_CUSTOM_CSS` | Inline CSS overrides |
| `AUTH_LAYOUT` | Card vs fullscreen layout |
| `AUTH_ENABLE_GOOGLE` | Toggle Google OAuth |
| `AUTH_ENABLE_APPLE` | Toggle Apple OAuth |
| `AUTH_ENABLE_PASSKEYS` | Toggle passkeys |
| `AUTH_SUCCESS_REDIRECT` | Post-auth redirect URL |
| `BOUNCER_LOGO_URL` | Logo on bouncer challenge |
| `BOUNCER_ACCENT_COLOR` | Accent color on challenge |

## Acceptance Criteria

- [ ] Bouncer login/register pages resolve branding settings per-group when group is determinable from the request
- [ ] Group is detected from OAuth `client_id` query parameter (primary mechanism)
- [ ] Group is detected from request hostname mapping (secondary mechanism)
- [ ] Falls back to global settings when no group is detected or group has no override
- [ ] All `AUTH_*` and `BOUNCER_*` branding settings listed above are per-group resolvable
- [ ] No breaking changes — existing single-tenant deployments work unchanged
- [ ] Settings are cached via existing Redis cache in `Setting.resolve`

## Constraints

- Must not break existing auth flows for single-tenant deployments
- Must use existing `Setting.resolve()` infrastructure — no new settings backend
- Group detection must be fast (single DB/cache lookup, not per-request scan)
- Custom domain mapping should be optional — not all deployments need it

## Out of Scope

- Per-group terms versioning and acceptance tracking (separate request in REDACTED)
- OAuth consent screen (separate request in REDACTED)
- Custom domain SSL/CNAME provisioning infrastructure
- Per-group OAuth provider credentials (Google/Apple client IDs per operator)

---

## Plan

**Status**: planned
**Planned**: 2026-04-11

### Objective

Make bouncer auth pages (login, register, challenge) resolve branding settings per-group so multi-tenant deployments serve operator-branded experiences through a single deployment.

### Steps

1. **`mojo/apps/account/models/group.py`** — Add `auth_domain` field
   - `auth_domain = models.CharField(max_length=255, null=True, default=None, unique=True, db_index=True)`
   - Add to relevant REST graphs so admins can set it via API

2. **`mojo/apps/account/rest/bouncer/views.py`** — Add `_resolve_group(request)` helper
   - Try `request.get_host()` → `Group.objects.filter(auth_domain=hostname, is_active=True).first()`
   - Fall back to `request.DATA.get('group')` or `request.GET.get('group')` → `Group.objects.filter(uuid=value, is_active=True).first()`
   - Cache hostname→group_id in Redis (`auth_domain:{hostname} → group_id`) to avoid DB hit per page view
   - Return `None` if no group found (single-tenant fallback)

3. **`mojo/apps/account/rest/bouncer/views.py`** — Update `_auth_context(request, group=None)`
   - Accept `group` parameter
   - Change every `settings.get('AUTH_*', default)` to `settings.get('AUTH_*', default, group=group)`
   - This gives us automatic fallback: group setting → parent chain → global → Django settings → hardcoded default

4. **`mojo/apps/account/rest/bouncer/views.py`** — Update `_serve_login()`, `on_login_page()`, `on_register_page()`
   - Call `group = _resolve_group(request)` at the top of each page view
   - Pass `group` through to `_auth_context(request, group=group)`
   - Pass `group.uuid` into template context as `group_uuid` (for OAuth state and JS)

5. **`mojo/apps/account/rest/bouncer/views.py`** — Update `_serve_challenge()`
   - Accept `group` parameter
   - Default logo/brand: REDACTED (current hardcoded values)
   - If group provided: resolve `BOUNCER_CHALLENGE_LOGO_URL` and `BOUNCER_CHALLENGE_BRAND` via `settings.get(..., group=group)`, falling back to the REDACTED defaults
   - This keeps challenge REDACTED-branded unless operator explicitly overrides

6. **`mojo/apps/account/rest/bouncer/views.py`** — Update `_serve_decoy()` (nice-to-have)
   - Low priority. Accept `group` and resolve `BOUNCER_LOGO_URL` / `BOUNCER_ACCENT_COLOR` per-group
   - Only matters if operator is on a custom domain (bot hits `auth.clubaxo.com/login`)

7. **`mojo/apps/account/services/oauth/google.py` and `apple.py`** — Encode group in OAuth `state`
   - When building the OAuth redirect URL, if `group_uuid` is present in the request, embed it in the `state` parameter alongside the CSRF token
   - On OAuth callback, extract `group_uuid` from `state` and resolve the group so branding survives the round-trip

8. **`mojo/apps/account/templates/account/auth_base.html`** — Pass `group_uuid` to JS config
   - Add `group_uuid` to `window._matConfig` so `mojo-auth.js` can include it in API calls and OAuth redirects
   - No template structure changes needed — branding variables already flow in via context

9. **Run `bin/create_testproject`** — Regenerate test project with the new `auth_domain` migration

### Design Decisions

- **Hostname first, query param fallback**: Hostname (`auth_domain` field) is the trusted signal — DNS is harder to spoof than a query param. Query param (`?group=<uuid>`) is the easy path for platforms that don't have custom domains yet.
- **Indexed `auth_domain` field vs JSONField**: Proper column gives us DB-enforced uniqueness (no two groups can claim the same hostname), fast indexed lookup, and discoverability in admin/REST. JSONField query is fragile across backends and can't enforce uniqueness.
- **Redis cache for hostname→group**: `auth_domain:{hostname} → group_id` avoids DB hit on every page view. Cache invalidated on Group save. 2-3 lookups per login flow, all cache hits after first.
- **`settings.get(group=group)` for all branding**: Leverages existing `Setting.resolve()` infrastructure — group → parent chain → global fallback. Zero new settings backend code.
- **Challenge page: REDACTED default, opt-in override**: Challenge is a security gate. Operators see REDACTED branding unless they explicitly set `BOUNCER_CHALLENGE_LOGO_URL` / `BOUNCER_CHALLENGE_BRAND` at their group level.
- **Decoy page: nice-to-have**: Bots aren't humans — operator branding on decoy is low priority. Implement if trivial, skip if not.
- **Group query param uses `uuid`**: Already indexed on Group, no new field needed. URL looks like `?group=f7a2b3c4-...`.
- **Verification emails use global REDACTED domain**: Out of scope — emails and SMS come from REDACTED's own addresses/numbers regardless of operator branding.

### Edge Cases

- **No group detected**: `group=None` → `settings.get(..., group=None)` skips group chain, goes straight to global. Identical to current behavior. Single-tenant deployments unaffected.
- **Inactive group**: `_resolve_group()` filters `is_active=True`. Deactivated operator falls through to global branding.
- **Query param spoofing**: Attacker uses `?group=<other_uuid>`. Worst case: they see that operator's public branding (logo, colors). No security impact — branding is cosmetic, auth logic unchanged.
- **OAuth round-trip loses group context**: Group UUID encoded in OAuth `state` parameter alongside CSRF token. When Google/Apple redirects back with `?code=...&state=...`, we extract the group UUID and restore branding.
- **Hostname matches but group has no setting overrides**: `Setting.resolve` walks parent chain then global. Operator gets parent/global branding — correct behavior.
- **Two groups try to claim same hostname**: `unique=True` on `auth_domain` — DB rejects the second save with IntegrityError. REST returns 400.
- **Redis cache stale after group save**: Invalidate `auth_domain:{old_hostname}` and set `auth_domain:{new_hostname}` on Group post-save. Use existing `post_save` signal or `on_rest_saved`.
- **Pass cookie across groups on same domain**: Pass cookie (`mbp`) is domain-scoped by browser. If operators share a parent domain (e.g., `axo.REDACTED.com` and `other.REDACTED.com`), cookie scope depends on `SameSite` and domain attribute. Not a branding issue — cookie just skips challenge, doesn't affect which group's branding is shown.

### Testing

- **Group detection from hostname** → `tests/test_account/test_bouncer_whitelabel.py`
  - Request with matching `auth_domain` → correct group resolved
  - Request with unknown hostname → `None` returned
  - Request with inactive group's hostname → `None` returned
- **Group detection from query param** → same test file
  - `?group=<valid_uuid>` → correct group resolved
  - `?group=<invalid>` → `None` returned
  - Hostname takes precedence over query param when both present
- **Per-group branding resolution** → same test file
  - Group has `AUTH_LOGO_URL` override → login page uses it
  - Group has no override, parent does → parent's value used
  - No group or override → global default used
- **Challenge page branding** → same test file
  - No override → REDACTED branding
  - Group sets `BOUNCER_CHALLENGE_LOGO_URL` → operator logo shown
- **OAuth state round-trip** → same test file or `tests/test_account/test_oauth.py`
  - OAuth redirect includes group UUID in state
  - Callback extracts group UUID and resolves branding
- **`auth_domain` uniqueness** → same test file
  - Two groups with same `auth_domain` → IntegrityError
- **Redis cache invalidation** → same test file
  - Change group's `auth_domain` → old hostname cache cleared, new hostname cached

### Docs

- `docs/django_developer/account/bouncer.md` — Add "Per-Group Branding" section: `auth_domain` field, Setting overrides per group, challenge page override settings
- `docs/django_developer/account/group.md` — Document `auth_domain` field
- `docs/web_developer/account/bouncer.md` — Add note about per-group branding for custom domain deployments
- `docs/web_developer/account/auth_pages.md` — Document `?group=<uuid>` query param for branding, note that branding settings can be per-group

## Resolution

**Status**: resolved
**Date**: 2026-04-11

### What Was Built
Per-group branding for bouncer auth pages. Group detection via hostname (`auth_domain` field) or `?group=<uuid>` query param. All AUTH_* settings resolve per-group with parent chain fallback. Challenge page defaults to REDACTED branding with opt-in override. OAuth state preserves group context through round-trip.

### Files Changed
- `mojo/apps/account/models/group.py` — `auth_domain` field, `resolve_by_auth_domain()`, Redis cache + invalidation
- `mojo/apps/account/migrations/0039_group_auth_domain.py` — migration
- `mojo/apps/account/rest/bouncer/views.py` — `_resolve_group()`, per-group `_auth_context()`, challenge branding
- `mojo/apps/account/rest/oauth.py` — `group_uuid` in OAuth state + callback redirect
- `mojo/apps/account/templates/account/auth_base.html` — `groupUuid` in `window._matConfig`

### Tests
- `tests/test_whitelabel/whitelabel.py` — 17 tests covering group detection, branding resolution, parent fallback, challenge branding, OAuth state, REST API
- Run: `bin/run_tests -t test_whitelabel`
- Full suite: 1,561 tests, 0 failures

### Docs Updated
- `docs/django_developer/account/group.md` — auth_domain field docs
- `docs/django_developer/account/bouncer.md` — Per-Group Branding section
- `docs/web_developer/account/bouncer.md` — Custom domain deployments
- `docs/web_developer/account/auth_pages.md` — ?group= param, per-group branding
- `CHANGELOG.md` — v1.1.19 entry

### Security Review
- Fixed negative cache bug (b'0' is truthy in Python)
- Added |escapejs filter on group_uuid template var
- scan_iter in cache invalidation is O(N) but only on admin saves — acceptable
- ?group= spoofing is cosmetic-only by design

### Follow-up
- None
