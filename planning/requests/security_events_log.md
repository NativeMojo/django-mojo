# Request: Security Events Log

## Status
Pending

## Summary

Add `GET /api/account/security-events` — a lightweight, ungated endpoint that
returns auth-relevant audit events for the currently authenticated user. No
special permission required.

## Background

The incident/audit log already exists and auth events are already being written
to it (`login:unknown`, `invalid_password`, `totp:login_failed`,
`email_change:requested`, `password_reset`, etc.). The full log is gated behind
the `view_logs` permission, which ordinary users do not hold. This means users
have no way to see their own security history — a basic expectation on any
security settings page.

This is a permission-wrapper and filter around existing data, not new
infrastructure.

## Relevant Files

- `mojo/apps/account/rest/user.py` — add the new endpoint here
- `mojo/apps/account/models/user.py` — `log()` / `report_incident()` methods
  that write to the incident table
- `docs/web_developer/account/user_self_management.md` — add to section 12
  (Activity Log) and quick reference table
- `docs/web_developer/account/authentication.md` — cross-reference
- `tests/test_accounts/` — add test file or extend existing

## Endpoint

```
GET /api/account/security-events
Authorization: Bearer <access_token>
```

Optional query params:

| Param | Default | Purpose |
|---|---|---|
| `size` | `25` | Max results to return |
| `sort` | `-created` | Sort order |
| `dr_start` | — | ISO date range start |
| `dr_end` | — | ISO date range end |

**Response:**

```json
{
  "status": true,
  "count": 4,
  "results": [
    {
      "created": "2026-04-01T10:00:00Z",
      "kind": "login",
      "summary": "Successful login",
      "ip": "203.0.113.5"
    }
  ]
}
```

## Event Kinds to Include

Filter the incident/log table to the following `kind` prefixes for the
requesting user only:

- `login` / `login:unknown`
- `invalid_password`
- `password_reset`
- `totp:*`
- `email_change:*`
- `email_verify:*`
- `phone_change:*`
- `phone_verify:*`
- `username:changed`
- `oauth`
- `passkey:*`
- `api_key:*`

## Security / Permission Rules

- Requires valid JWT (`@md.requires_auth()`)
- Always scoped to `request.user` — never accepts a `uid` param
- No `view_logs` permission required
- Returns the requesting user's own events only — no cross-user access possible

## Implementation Notes

- Query the existing incident/log model (same table as `logit`) filtered to
  `uid=request.user.pk` and the kind prefixes above
- Do not expose raw `details` strings from the incident table — these may
  contain internal state. Return only `created`, `kind`, a sanitised `summary`,
  and `ip`
- The `summary` field should be a human-readable label derived from `kind`, not
  the raw `log` column value
- Result set should be capped (default 25, max 100) — this is not a full audit
  export

## Docs to Update

- `docs/web_developer/account/user_self_management.md` — update section 12 to
  note this endpoint as the user-facing security feed; keep the `logit` reference
  for admin/privileged use
- Quick reference table — add row
- `CHANGELOG.md` — add entry under current version

## Tests

Add tests covering:

1. Returns events for authenticated user
2. Returns empty list when no matching events exist
3. Does not return events belonging to another user
4. Unauthenticated request returns 403
5. `size` and date range params filter correctly
6. Raw `details` / sensitive fields are not present in the response

Run in downstream project:
```
python manage.py testit test_accounts.security_events
```
