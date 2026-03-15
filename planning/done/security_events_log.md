# Request: Security Events Log

## Status
Ready to build

## Priority
High

## Summary

Add `GET /api/account/security-events` — a lightweight, ungated endpoint that
returns auth-relevant audit events for the currently authenticated user. No
special permission required.

---

## Background

Auth events are already written to `incident.Event` via `report_incident()` /
`class_report_incident()` throughout the account and auth code. The full incident
system is gated behind `view_incidents`, which ordinary users do not hold. This
means users have no visibility into their own security history — a basic
expectation on any settings page ("Recent activity", "Where you're logged in",
"Recent password changes", etc.).

This endpoint is a permission-scoped, field-sanitised filter over `incident.Event`
for the requesting user's own events. No new infrastructure required.

---

## Table to Query

**`incident.Event`** — not `logit.Log`.

`incident.Event` is where `report_incident()` writes to. It has:
- `uid` — integer user PK (our filter key)
- `category` — the event kind string (e.g. `"invalid_password"`, `"totp:login_failed"`)
- `created` — timestamp
- `source_ip` — the IP the event originated from
- `details` — raw internal detail string (**do not expose** — may contain internal state)
- `title` — optional human title
- `level` — severity integer

`incident.Incident` is a higher-level aggregation used for system operator
action (e.g. a rule fired, a threshold was crossed). Do not query that table here.

---

## Endpoint

```
GET /api/account/security-events
Authorization: Bearer <access_token>
```

### Query params

| Param | Default | Notes |
|---|---|---|
| `size` | `25` | Max results; hard cap at 100 |
| `sort` | `-created` | Sort order |
| `dr_start` | — | ISO date range start (inclusive) |
| `dr_end` | — | ISO date range end (inclusive) |

### Response

```json
{
  "status": true,
  "count": 4,
  "results": [
    {
      "created": "2026-04-01T10:00:00Z",
      "kind": "invalid_password",
      "summary": "Failed login attempt",
      "ip": "203.0.113.5"
    },
    {
      "created": "2026-04-01T09:55:00Z",
      "kind": "login",
      "summary": "Successful login",
      "ip": "203.0.113.5"
    }
  ]
}
```

### Field mapping from `incident.Event`

| Response field | Source field | Notes |
|---|---|---|
| `created` | `Event.created` | Direct |
| `kind` | `Event.category` | Renamed for clarity |
| `summary` | Derived from `Event.category` | Human-readable label — see table below |
| `ip` | `Event.source_ip` | May be null |

**Never expose:** `details`, `title`, `metadata`, `level`, `model_name`,
`model_id`, `hostname`, `country_code`, `incident` FK, or any raw log content.

---

## Kind → Summary mapping

Build a plain dict in the endpoint implementation. Unknown kinds fall back to
the `kind` string itself (forward-compatible).

| `kind` (category) | `summary` |
|---|---|
| `login` | `"Successful login"` |
| `login:unknown` | `"Login attempt with unknown account"` |
| `invalid_password` | `"Failed login — incorrect password"` |
| `password_reset` | `"Password reset requested"` |
| `totp:confirm_failed` | `"TOTP setup — invalid confirmation code"` |
| `totp:login_failed` | `"Failed login — incorrect TOTP code"` |
| `totp:login_unknown` | `"TOTP login attempt with unknown account"` |
| `totp:recovery_used` | `"TOTP recovery code used"` |
| `email_change:requested` | `"Email change requested"` |
| `email_change:requested_code` | `"Email change requested (code flow)"` |
| `email_change:cancelled` | `"Email change cancelled"` |
| `email_change:invalid` | `"Email change — invalid token"` |
| `email_change:expired` | `"Email change — expired token"` |
| `email_verify:confirmed` | `"Email address verified"` |
| `email_verify:confirmed_code` | `"Email address verified via code"` |
| `phone_change:requested` | `"Phone number change requested"` |
| `phone_change:confirmed` | `"Phone number changed"` |
| `phone_change:cancelled` | `"Phone number change cancelled"` |
| `phone_verify:confirmed` | `"Phone number verified"` |
| `username:changed` | `"Username changed"` |
| `oauth` | `"Signed in with social account"` |
| `passkey:login_failed` | `"Failed passkey login"` |
| `account:deactivated` | `"Account deactivated"` |
| `sessions:revoked` | `"All sessions revoked"` |
| `sessions:revoke_failed` | `"Session revoke — incorrect password"` |

---

## Category prefixes to include

Query `Event` where `uid = request.user.pk` AND `category` matches any of these
prefixes (use `Q` objects with `__startswith` or an `__in` with exact matches):

- `login`
- `invalid_password`
- `password_reset`
- `totp:`
- `email_change:`
- `email_verify:`
- `phone_change:`
- `phone_verify:`
- `username:`
- `oauth`
- `passkey:`
- `account:deactivat`
- `sessions:`
- `api_key:`
- `magic_login`

---

## Security / Permission Rules

- Requires valid JWT — `@md.requires_auth()`
- Scoped unconditionally to `request.user.pk` — the `uid` param is ignored if
  provided; a user can only ever see their own events
- No `view_incidents` permission required
- No cross-user access possible by design

---

## Implementation Notes

- Query: `Event.objects.filter(uid=request.user.pk, <category filter>).order_by('-created')`
- Apply `dr_start` / `dr_end` as `created__gte` / `created__lte`
- Cap `size` at 100 regardless of what is passed
- Return a plain `JsonResponse` — do not use `Event.on_rest_request()` (that
  requires `view_incidents` and would expose raw fields)
- Build the response list manually from the field mapping above

---

## Files in Scope

| File | Change |
|---|---|
| `mojo/apps/account/rest/user.py` | Add `on_account_security_events` endpoint |
| `docs/web_developer/account/user_self_management.md` | Update section 12 — note this as the user-facing feed; keep `logit` reference for admin use |
| `docs/web_developer/account/authentication.md` | Add cross-reference under Security Notes |
| `tests/test_accounts/security_events.py` | New test file |
| `CHANGELOG.md` | Entry under current version |

---

## Tests Required

1. Returns events for the authenticated user filtered to security kinds
2. Returns empty list when no matching events exist
3. Does not return events belonging to another user — even if `uid` param is
   passed in the query string
4. Unauthenticated request returns 403
5. `size` param limits results; values > 100 are capped at 100
6. `dr_start` and `dr_end` filter correctly
7. `details`, `title`, `metadata`, `level`, `model_name` are absent from all results
8. Unknown `category` values fall back to the category string as summary

Run in downstream project:
```
python manage.py testit test_accounts.security_events
```

---

## Out of Scope

- Exposing `incident.Incident` data (system operator concern)
- Querying `logit.Log` (separate table, different purpose)
- Pagination beyond `size` (not needed for a security feed of this nature)
- Filtering by kind client-side (return all matching kinds; UI can filter locally)