# Request: Notification Preferences

## Status
Pending

## Priority
Medium

## Summary
Add `GET` and `POST` endpoints so a logged-in user can control which notification
types they receive and on which channels (in-app, email, push). Preferences stored
in `user.metadata` — no new model or migration required.

---

## Background

The framework already delivers notifications by `kind` via `account.Notification`,
email templates, and push. There is currently no way for a user to say "don't send
me email for `kind=marketing`" or "only send push for `kind=message`". This creates
noise and undermines user trust.

---

## Endpoints

### GET `/api/account/notification/preferences`

Returns the user's current preferences.

**Auth:** Required

**Response:**
```json
{
  "status": true,
  "data": {
    "preferences": {
      "message":  { "in_app": true, "email": true,  "push": true  },
      "marketing": { "in_app": true, "email": false, "push": false }
    }
  }
}
```

An absent kind means "all channels on" (default). Only explicitly set preferences
are stored.

---

### POST `/api/account/notification/preferences`

Update one or more kind preferences in a single call. Partial update — only keys
present in the request body are changed; others are left untouched.

**Auth:** Required

**Request:**
```json
{
  "preferences": {
    "marketing": { "email": false, "push": false }
  }
}
```

**Response:**
```json
{
  "status": true,
  "data": {
    "preferences": {
      "marketing": { "in_app": true, "email": false, "push": false }
    }
  }
}
```

---

## Storage

Store in `user.metadata` under the key `"notification_preferences"`:

```python
user.metadata["notification_preferences"] = {
    "marketing": {"in_app": True, "email": False, "push": False}
}
user.save(update_fields=["metadata", "modified"])
```

No new model or migration needed. `metadata` is already a `JSONField` on `User`.

---

## Enforcement

Add a helper `get_notification_preference(user, kind, channel)` in
`mojo/apps/account/services/` (or `mojo/helpers/`) that returns `True`/`False`.
Call it before sending notifications in:

- `user.send_template_email(...)` — check `email` channel
- push delivery path — check `push` channel
- `Notification.objects.create(...)` — check `in_app` channel

Default (no stored preference) must be `True` — fail-open for delivery,
fail-closed for suppression (i.e. only suppress when the user has explicitly
opted out).

---

## Validation

- `preferences` must be a dict
- Each key is a notification kind string (max 64 chars)
- Each value must be a dict with boolean values for known channels:
  `in_app`, `email`, `push`
- Unknown channel keys in the value dict are ignored (forward-compatible)
- Unknown kinds are accepted as-is (projects define their own kinds)

---

## In Scope

- `GET` and `POST` endpoints
- Storage in `user.metadata`
- `get_notification_preference(user, kind, channel)` helper
- Tests covering: get with no prefs set (all defaults true), set a pref, partial
  update preserves other prefs, invalid input rejected
- Docs: `docs/web_developer/account/notifications.md` (add preferences section)
- `user_self_management.md` section 10 updated
- `CHANGELOG.md` updated

## Out of Scope

- Per-group or per-org preference inheritance
- UI for managing preferences (downstream project concern)
- Backfilling enforcement into existing notification callsites in one pass —
  the helper should be added and enforcement wired incrementally

---

## Files to Touch

| File | Change |
|---|---|
| `mojo/apps/account/rest/` | New file `notification_prefs.py` or add to `notifications.py` |
| `mojo/apps/account/services/notification_prefs.py` | New helper `get_notification_preference()` |
| `docs/web_developer/account/notifications.md` | Add preferences section |
| `docs/web_developer/account/user_self_management.md` | Update section 10 |
| `tests/test_accounts/notification_prefs.py` | New test file |
| `CHANGELOG.md` | New entry |