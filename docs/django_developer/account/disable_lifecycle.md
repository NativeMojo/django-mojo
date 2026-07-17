# Disable Lifecycle (User and Group)

A single service owns all writes to `User.is_active` / `Group.is_active` plus the
audit metadata that explains *why*, *when*, *by whom*, and *with what note*. The
metadata lives under `metadata.protected.disable.*`.

Three callers go through this service:

1. **REST POST_SAVE_ACTIONS** — `disable` and `reactivate` on User and Group
2. **Inactive sweep** — auto-disables stale users / groups using `reason="inactive"`
3. **`pii_anonymize()`** — terminal anonymization, records `reason="anonymized"`

Direct writes to `is_active` via REST still work (`{"is_active": false}`) but do
not write the namespace and are no longer the recommended path.

---

## Schema

`metadata.protected.disable` (visible in graphs, write-protected):

```jsonc
{
  "reason": "admin|abuse|archived|inactive|anonymized|self|null",
  "at": "2026-05-09T14:22:01Z",
  "by_user_id": 42,                         // null for system-driven (sweep, anonymize)
  "by_username": "alice",                   // captured at action time
  "note": "Violated TOS §4. Refunded.",
  "exempt_from_auto_disable": false,
  "warning": {                              // present during inactivity warning window
    "sent_at": "2026-05-02T...",
    "days_until_disable_at_send": 7
  },
  "history": [                              // FIFO cap, see HISTORY_CAP
    {
      "at": "...", "reason": "...", "by_user_id": ..., "by_username": "...", "note": "...",
      "reactivated_at": "...|null",
      "reactivated_by_user_id": ..., "reactivated_by_username": "...",
      "reactivated_note": "..."
    }
  ]
}
```

`is_active` is the single source of truth. The namespace adds context, never
overrides the field.

### Reason enums

| Reason | Source | Description |
|---|---|---|
| `admin` | REST | Admin manually disabled the user/group |
| `abuse` | REST | Abuse / TOS violation |
| `archived` | REST (Group only) | Group archived as part of operations cleanup |
| `inactive` | Sweep (server-only) | Auto-disabled after N days of inactivity |
| `anonymized` | `pii_anonymize` (server-only) | GDPR right-to-erasure |
| `self` | Reserved for future self-deactivation flow | — |

REST POST_SAVE_ACTIONS reject server-only reasons with a 400.

### History cap

`history` is FIFO-capped at `HISTORY_CAP = 20`. Older entries are dropped on
overflow. Long-term audit lives in `incident.Event` and `logit.Log`, not on the
user record — this cap prevents unbounded JSON growth from repeat-toggle.

### Anonymize and history

`pii_anonymize()` is terminal. If the user has a non-empty live disable block at
the time of anonymization, the service appends a history entry with
`reactivated_at=null` and `reactivated_note="Anonymized; not reactivated"` so
the prior disable's reason is preserved. The live block is then overwritten
with `{reason: "anonymized", at, by_user_id, by_username, note: null, history}`.
All other (PII-bearing) metadata keys are wiped.

---

## Service API

```python
from mojo.apps.account.services import disable as disable_service

disable_service.disable_entity(entity, *, reason, by_user=None, note=None, request=None)
disable_service.reactivate_entity(entity, *, by_user=None, note=None, request=None)
disable_service.record_anonymize(entity, *, by_user=None, request=None)  # called by pii_anonymize
disable_service.mark_warning(entity, *, days_until_disable)
disable_service.clear_warning(entity)
disable_service.is_exempt(entity)        # honours new + legacy flags
disable_service.has_warning(entity)      # honours new + legacy
disable_service.get_warning_sent_at(entity)
disable_service.migrate_legacy(entity)   # idempotent, leaves legacy keys in place
```

`disable_entity` and `reactivate_entity` are atomic: they use a conditional
update on `is_active` to detect concurrent flippers and raise `ValueException`
on a race or already-in-target-state collision.

`disable_service.HISTORY_CAP` and the reason frozensets
(`USER_REST_REASONS`, `GROUP_REST_REASONS`) are exported for callers / tests.

---

## Kill Switch: Disable Is Instant (DM-042)

For `User` entities, `disable_entity` rotates `auth_key` in the **same atomic
UPDATE** as the `is_active` flip, then best-effort force-disconnects the
user's live websockets (`disconnect_realtime`, cross-process via the realtime
pub/sub disconnect channel — a Redis/realtime failure never blocks the
disable itself). Consequences:

- Every outstanding JWT (including `user_api_key` tokens) fails its signature
  check on the very next request — `User.validate_jwt` checks both the
  rotated `auth_key` and `is_active` directly, and rejects with the same
  generic `"Invalid token user"` error either way (no account-state oracle
  for whoever holds a stale token).
- **Reactivating does NOT resurrect pre-disable tokens** — the key was
  rotated, so the user must re-authenticate after reactivation. This is
  deliberate for the abuse case (see
  [Authenticated-Abuse Hardening](../security/abuse_hardening.md#3-the-account-kill-switch)).
- `User.revoke_sessions` (rotate `auth_key` without disabling) also drops
  live websockets via the same `disconnect_realtime` call.

`Group` entities are unaffected by the key rotation (`hasattr(entity,
"auth_key")` gates it to `User`); `Group.is_active` is already checked
per-request — and since DM-048, so is every ancestor's, via
`Group.is_effectively_active()` (deactivating a parent instantly darkens the
whole subtree; see [Group → Membership](group.md#membership)).

---

## REST Surface

| Method | Path | Body | Permission |
|---|---|---|---|
| POST | `/api/user/<id>` | `{"disable": {"reason": "admin", "note": "..."}}` | `manage_users` |
| POST | `/api/user/<id>` | `{"reactivate": {"note": "..."}}` | `manage_users` |
| POST | `/api/group/<id>` | `{"disable": {"reason": "admin\|abuse\|archived", "note": "..."}}` | `manage_groups` |
| POST | `/api/group/<id>` | `{"reactivate": {"note": "..."}}` | `manage_groups` |
| GET | `/api/auth/manage/throttle?user_id=N&key=login` | — | `manage_users` |

The body key (`disable` / `reactivate`) IS the action name — the model's
`POST_SAVE_ACTIONS` dispatch invokes `on_action_disable` / `on_action_reactivate`.

### Throttle read endpoint

Returns `{count, limit, window, retry_after_seconds}` from the per-account login
sliding-window in Redis. Pure read — does not modify Redis state. Pairs with
`POST /api/auth/manage/clear_rate_limit` for the reset operation. Unlike the
disable/reactivate actions above (RestMeta `SAVE_PERMS`, which allow the usual
group/member fallback), both throttle endpoints are gated with
`@md.requires_global_perms` — `manage_users` must be a global grant on the User.

Only `key="login"` is supported in v1. Unsupported keys return 400. Lookup by
`user_id` or `username`.

---

## Inactive Sweep Integration

`mojo.apps.account.services.inactive` calls into the disable service:

- Warn phase → `disable_service.mark_warning(entity, days_until_disable=N)`
- Disable phase → `disable_service.disable_entity(entity, reason="inactive", by_user=None)`
- Reactivation detection (`_clear_stale_warnings`) → `disable_service.has_warning` +
  `disable_service.get_warning_sent_at` + `disable_service.clear_warning`
- Exemption check → `disable_service.is_exempt`

See [inactive_sweep.md](inactive_sweep.md) for the cron, settings, and email
templates that trigger the sweep.

---

## `pii_anonymize` Integration

`User.pii_anonymize()` calls `disable_service.record_anonymize(self)` before
flipping `is_active=False`. The service replaces `metadata` with a fresh dict
containing only `protected.disable.*`, preserving any prior cycle in history
while wiping every other metadata key (which may carry user PII).

---

## Back-Compat for Legacy Keys

Legacy protected metadata keys are still honoured on read for one release:

| Legacy key | New equivalent |
|---|---|
| `protected.no_disable=True` | `protected.disable.exempt_from_auto_disable=True` |
| `protected.disable_warned=True` + `protected.disable_warn_date="<iso>"` | `protected.disable.warning.sent_at="<iso>"` + `days_until_disable_at_send` |

`disable_service.is_exempt`, `has_warning`, and `get_warning_sent_at` read both
shapes. New writes go to the new namespace only.

The data migration `0041_disable_lifecycle_migrate.py` walks existing entities
and populates the new namespace from legacy keys, leaving legacy keys in place.
A follow-up migration in the next release will remove the legacy keys.

---

## Permissions and Audit

- `is_active` is in `MANAGE_USERS_ONLY_FIELDS` ([user.py](../../../mojo/apps/account/models/user.py)),
  so direct writes via REST already require `manage_users`.
- The new POST_SAVE_ACTIONS additionally re-check `manage_users` (User) /
  `manage_groups` (Group) inside `on_action_*` and validate the reason enum —
  the combined `users`/`groups` term satisfies these checks too, since it
  includes `manage_users`/`manage_groups` by definition.
- The disable service emits `model_logit(kind="disabled"|"reactivated"|"auto_disabled")`
  and an `incident.report_event` per state change. These are the long-term
  audit records — `disable.history` is bounded.

---

## Why JSONField, not new columns?

Schema-shaped audit data (reason / note / by_user / history) belongs in JSON
because the column count would grow indefinitely as we add fields. JSONField
queries (`metadata__protected__disable__reason="inactive"`) are fast enough at expected
scale, and `metadata.protected` already has write-protection plumbing.

If a hot-path filter later proves slow at very large user counts, individual
fields can be promoted to columns without breaking the namespace contract.

---

## Related

- [User model](user.md)
- [Group model](group.md)
- [Inactive sweep](inactive_sweep.md)
- [Login throttling](../../../mojo/decorators/limits.py) — `read_account_attempt`
- [Authenticated-Abuse Hardening](../security/abuse_hardening.md) — kill switch rationale, API throttle, websocket limits
