# Auto-Disable Inactive Users and Groups

A nightly cron job (`inactive_sweep`) automatically warns and then disables users and groups that have not been active for a configurable number of days. The feature is opt-in via settings flags and affects both `User` and `Group` independently.

---

## How It Works

The sweep runs every night at 03:00 UTC. For each enabled resource type it runs three phases:

1. **Clear stale warnings** — If an entity has a pending warning flag but its `last_activity` is more recent than the warning date, it reactivated. The warning flag is cleared.
2. **Warn** — Entities whose `last_activity` has passed `inactive_days - warning_days` ago and have not yet been warned get a warning email and a `disable_warned` flag in `metadata["protected"]`.
3. **Disable** — Entities whose `last_activity` has passed `inactive_days` ago are set `is_active = False` atomically. An incident event is emitted at level 4.

---

## Settings

| Setting | Default | Description |
|---|---|---|
| `ACCOUNT_AUTO_DISABLE_ENABLED` | `False` | Enable/disable the user sweep. Off by default. |
| `ACCOUNT_INACTIVE_DAYS` | `90` | Days of inactivity before a user is disabled |
| `ACCOUNT_INACTIVE_WARNING_DAYS` | `7` | Days before disable threshold to send the warning email |
| `GROUP_AUTO_DISABLE_ENABLED` | `False` | Enable/disable the group sweep. Off by default. |
| `GROUP_INACTIVE_DAYS` | `90` | Days of inactivity before a group is disabled |

```python
# settings.py
ACCOUNT_AUTO_DISABLE_ENABLED = True
ACCOUNT_INACTIVE_DAYS = 90
ACCOUNT_INACTIVE_WARNING_DAYS = 7

GROUP_AUTO_DISABLE_ENABLED = True
GROUP_INACTIVE_DAYS = 90
```

---

## Exemptions

The following users are always skipped — both for warnings and for disable:

- `is_superuser = True`
- `is_staff = True`
- `metadata["protected"]["no_disable"] = True`
- `last_activity` is null **and** `last_login` is null (never logged in)

For groups, only the `no_disable` flag applies (no staff/superuser concept).

To permanently exempt an entity from the sweep:

```python
user.set_protected_metadata("no_disable", True)
group.set_protected_metadata("no_disable", True)
```

---

## Warning Emails

The sweep sends template emails when warning. Both templates ship as seed files and are auto-loaded from `mojo/apps/aws/seeds/email_templates/` on first use.

| Template | Sent To | Context Variables |
|---|---|---|
| `account_inactive_warning` | The user directly | `days_until_disable`, `inactive_days` |
| `group_inactive_warning` | All active users with `manage_groups` or `groups` permission | `group_name`, `group_id`, `days_until_disable`, `inactive_days` |

Customize or override either template by creating or editing the `EmailTemplate` database record with the same name.

---

## Incident Events

Each warning and disable action emits an incident event via `incident.report_event()`:

| Action | Category | Level |
|---|---|---|
| User warned | `account:inactive_warning` | 2 |
| User disabled | `account:auto_disabled` | 4 |
| Group warned | `group:inactive_warning` | 2 |
| Group disabled | `group:auto_disabled` | 4 |

These appear in the security incident feed and in the assistant's `query_events` tool.

---

## Protected Metadata Keys

The sweep uses `metadata["protected"]` on both `User` and `Group` to track state:

| Key | Type | Purpose |
|---|---|---|
| `no_disable` | `True` | Permanent exemption from the sweep |
| `disable_warned` | `True` | Set when a warning has been sent; cleared after disable or reactivation |
| `disable_warn_date` | ISO datetime string | UTC timestamp of when the warning was sent; used to detect reactivation |

These keys are written by the sweep service and are write-protected from REST clients. See [Group protected metadata](group.md#protected-metadata) and the `user.md` protected metadata section.

---

## Service API

The service functions are in `mojo.apps.account.services.inactive` and can be called directly in code or tests:

```python
from mojo.apps.account.services.inactive import (
    warn_inactive_users,
    disable_inactive_users,
    warn_inactive_groups,
    disable_inactive_groups,
    _clear_stale_warnings,
)
from mojo.apps.account.models import User, Group

# Run individual phases manually
cleared = _clear_stale_warnings(User, inactive_days=90)
warned  = warn_inactive_users()
disabled = disable_inactive_users()
```

Each function returns the count of entities affected.

---

## Cron Schedule

The sweep is registered as a cron job in `mojo/apps/account/cronjobs.py`:

```
0 3 * * *   → account.inactive_sweep   (daily at 03:00 UTC)
```

It runs as an async job on the `cleanup` channel. No action is taken when both `ACCOUNT_AUTO_DISABLE_ENABLED` and `GROUP_AUTO_DISABLE_ENABLED` are `False`.

---

## Related

- [User model](user.md)
- [Group model](group.md)
- [Email sending](../email/sending.md)
