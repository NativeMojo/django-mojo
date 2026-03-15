# Request: Notification Preferences

## Status
Ready to build

## Priority
Medium

## Decisions (from planning session)
- Enforcement is **included in this sprint** — wire the helper into all three
  delivery callsites (`send_template_email`, push delivery, `Notification` creation)
  at the same time as the endpoints. Do not defer enforcement to a follow-up.
- Storage in `user.metadata["notification_preferences"]` — no new model, no migration.
- Default is **allow** — only suppress when the user has explicitly opted out.
- Partial update semantics on POST — only keys present in the request body are
  changed; others are untouched.

---

## Summary

Add `GET` and `POST` endpoints so a logged-in user can control which notification
types they receive and on which channels. Add a helper function that all delivery
paths check before sending. Wire enforcement into all three delivery callsites in
the same sprint.

In plain terms: a user goes to their settings page and turns off "marketing emails".
The framework stores that preference and then respects it everywhere a notification
is sent — in-app inbox, email, and push. Default is always on; the user only
suppresses what they explicitly opt out of.

---

## Endpoints

### GET `/api/account/notification/preferences`

Returns the user's current preferences. An absent kind means all channels are on
(default). Only explicitly set preferences are stored.

**Auth:** Required

**Response:**
```json
{
  "status": true,
  "data": {
    "preferences": {
      "message":   { "in_app": true, "email": true,  "push": true  },
      "marketing": { "in_app": true, "email": false, "push": false }
    }
  }
}
```

---

### POST `/api/account/notification/preferences`

Partial update — only the keys present in the request body are changed. Missing
keys are left untouched.

**Auth:** Required

**Request:**
```json
{
  "preferences": {
    "marketing": { "email": false, "push": false }
  }
}
```

**Response:** Same shape as GET — returns full current preferences after applying
the update.

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

Stored in `user.metadata` under the key `"notification_preferences"`:

```python
user.metadata["notification_preferences"] = {
    "marketing": {"in_app": True, "email": False, "push": False}
}
user.save(update_fields=["metadata", "modified"])
```

`metadata` is already a `JSONField` on `User`. No new model or migration required.

---

## Helper Function

Add `is_notification_allowed(user, kind, channel)` in a new file:

```
mojo/apps/account/services/notification_prefs.py
```

Signature:

```python
def is_notification_allowed(user, kind, channel):
    """
    Returns True if the user has not opted out of this kind/channel combination.
    Default (no stored preference) is True — only suppress on explicit opt-out.

    Args:
        user: User instance
        kind: notification kind string (e.g. "marketing", "message")
        channel: one of "in_app", "email", "push"
    """
```

- If `user` is `None` or `user.metadata` is missing → return `True`
- If `kind` not in stored preferences → return `True`
- If `channel` not in that kind's preferences → return `True`
- Otherwise return the stored boolean value

---

## Enforcement — Wire Into All Three Delivery Paths

All three callsites must check `is_notification_allowed` before sending.
This is part of the current sprint, not a follow-up.

### 1. In-app notifications — `mojo/apps/account/models/notification.py`

Before creating a `Notification` record, check `in_app` channel:

```python
from mojo.apps.account.services.notification_prefs import is_notification_allowed

if not is_notification_allowed(user, kind, "in_app"):
    return None  # silently suppressed
```

The `kind` value on `Notification` is the natural key to check.

### 2. Email — `mojo/apps/account/models/user.py` → `send_template_email`

`send_template_email` is called with a `template_name`. Most template names
correspond directly to a notification kind. Add an optional `kind` param:

```python
def send_template_email(self, template_name, context=None, kind=None, ...):
    if kind:
        from mojo.apps.account.services.notification_prefs import is_notification_allowed
        if not is_notification_allowed(self, kind, "email"):
            return None
```

Callers that know the kind should pass it. Callers that don't (legacy, system
emails like password reset) do NOT pass `kind` and are therefore never suppressed
— system/transactional emails are always sent.

### 3. Push — `mojo/apps/account/rest/push.py` / push delivery path

Before dispatching a push notification to a user's devices, check `push` channel:

```python
from mojo.apps.account.services.notification_prefs import is_notification_allowed

if not is_notification_allowed(user, kind, "push"):
    return  # silently suppressed
```

---

## Validation

- `preferences` must be a dict — return 400 if not
- Each key is a notification kind string (max 64 chars)
- Each value must be a dict — return 400 if not
- Channel keys in the value dict: `in_app`, `email`, `push` — values must be boolean
- Unknown channel keys are ignored (forward-compatible)
- Unknown kind keys are accepted as-is (projects define their own kinds)

---

## Files to Touch

| File | Change |
|---|---|
| `mojo/apps/account/rest/notification_prefs.py` | New file — GET + POST endpoints |
| `mojo/apps/account/rest/__init__.py` | Import the new REST file |
| `mojo/apps/account/services/notification_prefs.py` | New file — `is_notification_allowed()` helper |
| `mojo/apps/account/models/notification.py` | Wire `is_notification_allowed` before `Notification` creation |
| `mojo/apps/account/models/user.py` | Add optional `kind` param to `send_template_email`; wire helper |
| `mojo/apps/account/rest/push.py` | Wire helper before push dispatch |
| `docs/web_developer/account/notifications.md` | Add preferences section |
| `docs/web_developer/account/user_self_management.md` | Update section 10, quick reference table |
| `tests/test_accounts/notification_prefs.py` | New test file |
| `CHANGELOG.md` | New entry |

---

## Out of Scope

- Per-group or per-org preference inheritance
- UI for managing preferences (downstream project concern)
- System / transactional emails (password reset, email verification, magic login,
  deactivation confirmation) — these are never suppressed by preferences

---

## Tests Required

Add `tests/test_accounts/notification_prefs.py`:

- `GET` with no preferences set returns empty `preferences` dict (all defaults on)
- `POST` sets a preference; subsequent `GET` returns it
- `POST` partial update does not affect previously set unrelated kinds
- `POST` with non-dict `preferences` returns 400
- `POST` with non-dict value for a kind returns 400
- `is_notification_allowed` returns `True` when no preference stored (default on)
- `is_notification_allowed` returns `False` when explicitly opted out
- `is_notification_allowed` returns `True` for unknown kind
- `is_notification_allowed` returns `True` for unknown channel
- Notification creation is suppressed when `in_app` preference is `False`
- `send_template_email` with `kind` is suppressed when `email` preference is `False`
- `send_template_email` without `kind` is never suppressed (transactional)
- Unauthenticated GET/POST returns 403

Run in downstream project:
```
python manage.py testit test_accounts.notification_prefs
```
