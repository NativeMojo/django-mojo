# Request: OAuth Registration Gate + New-User Signal

## Status
Resolved — 2026-03-16

## Resolution

- `mojo/apps/account/rest/user.py` — `jwt_login` replaced `is_new_user=False` param with `extra=None`; extra dict merged into response `data` (not `token_package`)
- `mojo/apps/account/rest/oauth.py` — `_find_or_create_user` returns `(user, conn, created)`; path 3 checks `OAUTH_ALLOW_REGISTRATION`; `on_oauth_complete` passes `extra={"is_new_user": True} if created else None`
- `tests/test_accounts/oauth.py` — updated all `_find_or_create_user` call sites to unpack 3 values; added `test_oauth_registration_gate`
- Docs: pending

## Priority
Medium

## Summary

Two related OAuth `complete` behaviours:

1. **Registration gate** — a Django setting (`OAUTH_ALLOW_REGISTRATION`, default `True`)
   that prevents new account creation via OAuth when set to `False`. Existing users
   can still log in via OAuth; only path 3 (no matching user or connection) is blocked.

2. **New-user signal** — when a new account IS created (path 3), the `complete` response
   should include a flag so the frontend knows to show an onboarding/welcome flow.

---

## Background

`_find_or_create_user` has three paths:

| Path | Condition | Desired new behaviour |
|------|-----------|----------------------|
| 1 | Existing `OAuthConnection` | unchanged |
| 2 | Existing `User` by email | unchanged |
| 3 | No match → create user | gate on `OAUTH_ALLOW_REGISTRATION`; signal `is_new_user` on success |

---

## Design Questions to Resolve

### Q1 — Where does `is_new_user` live in the response?

**Option A — Inside `token_package` (current partial impl)**
`token_package['is_new_user'] = True` alongside `access_token`, `refresh_token`, `user`.

Problem: `token_package` is the JWT response dict. Adding transient flags here is
semantically wrong — it's not part of the token or the user record, and `jwt_login`
shouldn't know about OAuth-specific signals.

**Option B — Top-level `data` field alongside `token_package`**
```json
{
  "status": true,
  "data": {
    "access_token": "...",
    "refresh_token": "...",
    "user": { ... },
    "is_new_user": true
  }
}
```
`jwt_login` returns its normal response. `on_oauth_complete` builds its own
`JsonResponse` when `created=True` by embedding the flag directly in the data dict.

Problem: `jwt_login` currently returns a `JsonResponse` object, so the caller can't
inject into it without unwrapping. Two options:
  - B1: have `jwt_login` accept an `extra` kwarg that gets merged into `data`
  - B2: have `on_oauth_complete` not call `jwt_login` directly when `created=True`,
    and instead inline the minimal token-creation logic (duplication risk)

**Option C — Separate top-level key**
```json
{
  "status": true,
  "is_new_user": true,
  "data": { "access_token": "...", ... }
}
```
Flag lives outside `data` entirely. Cleaner separation, but slightly non-standard
for our response shape.

**Recommendation: Option B1** — add `extra=None` to `jwt_login`, merge into the
response dict before returning. Keeps `jwt_login` generic (callers can attach any
signal), doesn't couple it to OAuth, and the flag lands naturally in `data`
alongside the tokens. `jwt_login` never inspects `extra`; it just passes it through.

---

## Proposed Implementation

### 1. `mojo/apps/account/rest/user.py` — `jwt_login`

Add `extra=None` parameter. After building `token_package`, merge `extra` into the
response dict (not into `token_package` — the JWT payload itself stays clean):

```python
def jwt_login(request, user, legacy=False, source=None, extra=None):
    ...
    # build token_package as today
    response_data = dict(token_package)
    if extra:
        response_data.update(extra)
    return JsonResponse(dict(status=True, data=response_data))
```

`token_package` is not mutated. `extra` is a plain dict of response-level signals.

### 2. `mojo/apps/account/rest/oauth.py` — `_find_or_create_user`

- Return `(user, conn, created)` — `True` only for path 3 new-user creation.
- Path 3: check `settings.get("OAUTH_ALLOW_REGISTRATION", True)` at call time.
  If `False`, raise `PermissionDeniedException("Account registration via OAuth is not permitted")`.

### 3. `mojo/apps/account/rest/oauth.py` — `on_oauth_complete`

```python
user, conn, created = _find_or_create_user(provider, profile)
...
extra = {"is_new_user": True} if created else None
return jwt_login(request, user, extra=extra)
```

### 4. Revert partial work

The current partial implementation put `is_new_user` directly into `token_package`
via `jwt_login(... is_new_user=created)` and `token_package['is_new_user'] = True`.
That must be reverted/replaced with the `extra` approach above.

---

## Settings

| Setting | Default | Purpose |
|---------|---------|---------|
| `OAUTH_ALLOW_REGISTRATION` | `True` | Allow path 3 new-user creation via OAuth |

---

## Response Shape (complete endpoint, new user)

```json
{
  "status": true,
  "data": {
    "access_token": "eyJ...",
    "refresh_token": "eyJ...",
    "expires_in": 21600,
    "user": { "id": 99, "username": "alice_g", "display_name": "Alice" },
    "is_new_user": true
  }
}
```

`is_new_user` is only present (and `true`) when path 3 ran. Existing-user logins
(paths 1 and 2) return the normal token response with no `is_new_user` key.

---

## Error Case (registration disabled)

`OAUTH_ALLOW_REGISTRATION = False` + unknown email:

```json
{
  "status": false,
  "error": "Account registration via OAuth is not permitted",
  "status_code": 403
}
```

---

## Files to Change

| File | Change |
|------|--------|
| `mojo/apps/account/rest/user.py` | Add `extra=None` to `jwt_login`; merge into response dict |
| `mojo/apps/account/rest/oauth.py` | `_find_or_create_user` returns `(user, conn, created)`; registration gate; pass `extra` to `jwt_login` |
| `docs/django_developer/account/oauth.md` | Document `OAUTH_ALLOW_REGISTRATION` setting |
| `docs/web_developer/account/oauth.md` | Document `is_new_user` flag in complete response |

---

## Out of Scope

- Onboarding flow logic (frontend concern)
- Profile completion prompts
- Any change to paths 1 or 2
