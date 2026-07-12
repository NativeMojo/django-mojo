# Permissions — Django Developer Reference

How RestMeta permissions work across the platform. This document covers the permission system, all permission names, and the current permission map for every model.

## How Permissions Work

Every model with a `RestMeta` class defines who can read, write, and delete:

```python
class RestMeta:
    VIEW_PERMS = ["view_books", "owner"]     # who can GET (list + detail)
    SAVE_PERMS = ["manage_books", "owner"]   # who can POST/PUT (create + update)
    CREATE_PERMS = ["manage_books"]           # optional: who can POST (create only, falls back to SAVE_PERMS)
    DELETE_PERMS = ["manage_books"]           # optional: who can DELETE (falls back to SAVE_PERMS)
```

### Special Permission Values

| Value | Meaning |
|-------|---------|
| `"owner"` | Grants access if `instance.{OWNER_FIELD} == request.user` |
| `"all"` | Public — no authentication required |
| `"authenticated"` | Any logged-in user |
| Any other string | Must exist as a key in `user.permissions` or group permissions |

### Common Mistake

**If a permission is in SAVE_PERMS, it should also be in VIEW_PERMS.** Otherwise users can create/update records they can't read back. Always ensure write permissions are a subset of read permissions.

```python
# WRONG — can write but can't read
class RestMeta:
    VIEW_PERMS = ["view_books"]
    SAVE_PERMS = ["manage_books"]  # manage_books user can't see the list!

# RIGHT — manage implies view
class RestMeta:
    VIEW_PERMS = ["view_books", "manage_books"]
    SAVE_PERMS = ["manage_books"]
```

## Global vs Group-Scoped Permission Checks

Non-RestMeta endpoints gate with a decorator. There are two, and choosing the
wrong one is a **cross-tenant privilege-escalation risk**.

- **`@md.requires_perms(...)`** — checks the caller's **global**
  `User.permissions` first, then (when `REQUIRES_PERMS_IS_GROUP` is `True`, the
  default) falls back to their **group/member** permission for the group named in
  the request (`request.group.user_has_permission(...)`). Correct when the
  endpoint's effect is confined to `request.group` (e.g. rotating *that* group's
  webhook secret).
- **`@md.requires_global_perms(...)`** — checks **global `User.permissions` (or
  superuser) only**, never the group/member fallback. Use it for any endpoint
  whose effect is **platform-wide**: global settings, fleet/job control, AWS
  infra, cross-tenant data, metrics ACLs, geofence config.

Why it matters: `GroupMember.permissions` accepts **arbitrary key names**, and
any group admin (holding `manage_group`/`manage_members`/`manage_users`/
`manage_groups` — or the combined `users`/`groups` term, which satisfies the
`manage_users`/`manage_groups` checks by definition) can assign them — bounded only by `MEMBER_PERMS_PROTECTION`
(empty by default). So under `requires_perms`, a tenant admin can grant a
teammate any permission *scoped to their own group*, then satisfy the check on
**any** endpoint by passing their own `group` id. If that endpoint's effect is
global, that's an escalation. `requires_global_perms` closes it: a group-scoped
grant never authorizes a global action.

```python
# Group-scoped effect — group/member fallback is correct:
@md.requires_perms("manage_group", "manage_groups", "groups")
def on_group_webhook_secret(request): ...   # touches request.group only

# Platform-wide effect — global grant required:
@md.requires_global_perms("manage_jobs", "jobs")
def on_clear_queue(request): ...            # clears the global job queue
```

## Instance-Level Permission Hooks

A model may override two per-instance hooks that the generic permission layer
(`MojoModel._evaluate_permission`) consults for **detail** operations (GET /
POST / PUT / DELETE on a pk):

| Hook | Signature | Governs |
|---|---|---|
| `check_view_permission(perms, request)` | returns `bool` | **reads** (GET, and the FK attach-by-pk VIEW check) |
| `check_edit_permission(perms, request)` | returns `bool` | **writes** (POST/PUT/DELETE and `POST_SAVE_ACTIONS`) |

The layer classifies each call **by its RestMeta keys**: a request carrying a
write key (`CREATE`/`SAVE`/`DELETE_PERMS`) is a **write** and uses
`check_edit_permission`, **skipping** `check_view_permission` — a read affordance
inside the view hook (e.g. a "members may read, downgraded" fallthrough) must
never authorize a save. A read carries only `VIEW_PERMS` and prefers
`check_view_permission`. A model that defines **only** `check_edit_permission`
(e.g. `User`) uses it for reads too, since there is no view hook to prefer. If a
hook isn't defined for the classified operation, evaluation falls through to the
owner match, the group-membership check, and finally `user.has_permission`.

`Group` is the canonical example: `check_view_permission` lets any active member
read their group with a downgraded (`basic`) graph, while `check_edit_permission`
requires an actual `SAVE_PERMS` grant (global `manage_groups`/`groups`, or
member-level `manage_group`; an ApiKey must be confined to its own group tree
**and** hold the perm). See [Account → Group](../account/group.md).

**API keys.** An `ApiKey` (`Authorization: apikey <token>`) is a *group-scoped*
credential, so `requires_global_perms` **rejects it by default** — letting a key
satisfy a platform-global gate would recreate the escalation through a machine
door. Machine access to a global endpoint should use a real service-account User
with a global grant. The one exception is a federation/ingest receiver whose
intended caller *is* a fleet-peer key: decorate it
`@md.requires_global_perms("perm", allow_api_keys=True)` (e.g. the geoip sync
receiver). The group/member fallback is still never consulted.

The same principle applies at the **model-security layer**, not just the
decorator: a `uses_model_security` model with **no `group` foreign key** is
platform-global, so `_evaluate_permission` **denies an ApiKey by default** on it
(there is no group to confine the key to). A model may opt back in with
`RestMeta.ALLOW_API_KEY_GLOBAL = True` (default `False`; none do initially).
Without this, a key self-claiming `manage_users` could read every tenant's
`User` rows. A key's own `permissions` are additionally gated on assignment by
`APIKEY_PERMS_PROTECTION` (see [API Keys](../account/api_keys.md)). Net: an
ApiKey can only ever reach **group-owned** data, confined to its own group.

The security registry records `global_only: True` for these endpoints, so audit
tooling can tell them apart.

## Permission Names

### Category Permissions (Broad Access)

Category permissions grant full read+write access to an entire domain. Use these for admin roles that need everything in a domain without toggling individual permissions.

**The bare category term is `view_X` + `manage_X` combined into one simple
term** — and the permission checkers enforce that automatically. A check for
`view_users` or `manage_users` is satisfied by a holder of bare `users` (same
for every category in the table below), at all three checker levels
(`User.has_permission`, `GroupMember.has_permission`, `ApiKey.has_permission`
— see `mojo/helpers/perms.py`). You do not need to remember to add the bare
term to every perm list; a list naming `manage_users` admits `users` holders
by definition.

Two boundaries to the expansion:

- **One-directional.** `manage_users` alone does NOT satisfy a check for bare
  `users` — the combined term is the superset, not the other way around.
- **Categories only.** Fine-grained perms that aren't `view_`/`manage_` +
  a category name (`manage_group` (singular), `manage_members`,
  `manage_settings`, `manage_chat`, ...) are never expanded.

| Permission | Domain | Grants access to |
|-----------|--------|-----------------|
| `security` | Security & Logs | Incidents, events, rules, tickets, IPSets, bouncer devices/signals/signatures, GeoLocatedIP, system logs, geofence config |
| `users` | Users | All user records, passkeys, TOTP, API keys, OAuth, devices, locations, bouncer/GeoLocatedIP |
| `groups` | Groups | Groups, members, group API keys, settings |
| `comms` | Communications | Email (mailboxes, domains, templates, messages), phone (numbers, config, SMS), push (config, templates, delivery, devices), chat (rooms, messages, reactions, receipts, membership) |
| `jobs` | Job System | Jobs, job events, job logs, runners, queue control, system stats |
| `metrics` | Metrics | All metrics operations — recording, fetching, categories, values, permissions management |
| `files` | Files | File managers, files, renditions, vault files, vault data, S3 buckets |

**Note:** `is_superuser` bypasses all permission checks. No need for a "system_admin" category.

### Fine-Grained Permissions

Use these when you need read-only access or scoped access within a domain:

| Permission | Domain | Grants |
|-----------|--------|--------|
| `owner` | Per-record | Access to records owned by the current user |
| `manage_users` | Users | User administration (CRUD users, devices, OAuth) |
| `view_users` | Users | Read-only access to user records |
| `manage_groups` | Groups | Group administration |
| `view_groups` | Groups | Read-only access to groups |
| `manage_group` | Groups | Manage the current user's group (scoped) |
| `view_members` | Groups | Read-only access to group members |
| `manage_settings` | Groups | Secure settings CRUD |
| `view_security` | Security | Read incidents, events, rules, firewall, bouncer data |
| `manage_security` | Security | Write access to incidents, rules, tickets, IP blocks |
| `view_geofence` | Security | Read-only access to geofence config (system rules, allowlist, simulate, bypass holders) |
| `manage_geofence` | Security | Write access to geofence system rules and IP allowlist |
| `manage_aws` | Email | SES email templates, domains, mailboxes |
| `manage_chat` | Communications | Create/manage chat rooms and membership |
| `view_fileman` | Files | Read file manager records |
| `manage_files` | Files | Upload and manage files |
| `view_vault` | Files | Read vault files and data |
| `manage_vault` | Files | Write vault files and data |
| `manage_docit` | Docs | Manage documentation books, pages, assets |
| `view_logs` | Security | Read logit entries |
| `manage_logs` | Security | Write/delete logit entries |
| `admin` | Security | Full admin access to logit |
| `manage_notifications` | Communications | Manage notification templates and delivery |
| `view_notifications` | Communications | Read notification delivery records |
| `manage_devices` | Communications | Manage registered push devices |
| `view_devices` | Communications | Read registered push devices |
| `manage_push_config` | Communications | Push notification configuration |
| `view_phone_numbers` | Communications | Read phone number records |
| `manage_phone_numbers` | Communications | Manage phone numbers |
| `manage_phone_config` | Communications | Phone hub configuration |
| `view_sms` | Communications | Read SMS records |
| `manage_sms` | Communications | Send and manage SMS |
| `send_sms` | Communications | Send SMS messages |
| `send_notifications` | Communications | Send push notifications |
| `view_jobs` | Jobs | Read-only job monitoring, queue sizes, runner info |
| `manage_jobs` | Jobs | Full job system control — create, cancel, retry, purge, runner management |
| `view_metrics` | Metrics | Read metrics data and categories |
| `manage_metrics` | Metrics | Manage metrics permissions and accounts |
| `write_metrics` | Metrics | Record metrics data |
| `manage_shortlinks` | Shortlink | Manage short links |

## Permission Map — All Models

### Account App

| Model | VIEW_PERMS | SAVE_PERMS | Notes |
|-------|-----------|-----------|-------|
| User | view_users, manage_users, **users**, owner | manage_users, **users**, owner | |
| Group | view_groups, manage_groups, manage_group, **groups** | manage_groups, manage_group, **groups** | |
| GroupMember | view_members, view_groups, manage_groups, manage_group, **groups** | manage_groups, manage_group, **groups** | |
| Setting | manage_settings, **groups** | manage_settings, **groups** | |
| ApiKey | manage_group, manage_groups, **groups** | manage_group, manage_groups, **groups** | Group-scoped API keys |
| Passkey | owner, manage_users, **users** | owner, manage_users, **users** | WebAuthn credentials |
| UserTOTP | owner, manage_users, **users** | owner, manage_users, **users** | MFA setup |
| UserAPIKey | owner, manage_users, **users** | owner, manage_users, **users** | Per-user API keys |
| OAuthConnection | owner, manage_users, **users** | manage_users, **users** | |
| UserDevice | manage_users, **users**, owner | (read-only) | Intentional — devices tracked automatically |
| UserDeviceLocation | manage_users, **users** | (read-only) | Intentional — locations tracked automatically |
| Notification | owner | owner | User's own notifications only |
| GeoLocatedIP | manage_users, view_security, manage_security, **security**, **users** | (actions only) | Has POST_SAVE_ACTIONS |
| BouncerDevice | manage_users, view_security, manage_security, **security**, **users** | manage_users, manage_security, **security**, **users** | |
| BouncerSignal | manage_users, view_security, manage_security, **security**, **users** | (read-only) | Audit trail |
| BotSignature | manage_users, view_security, manage_security, **security**, **users** | manage_users, manage_security, **security**, **users** | |

**REST endpoints (non-RestMeta):** The geofence config-plane endpoints in `account/rest/geofence.py` (`geo/rules`, `geo/simulate`, `geo/allowlist`, `geo/bypass_holders`) accept `view_geofence`/`manage_geofence`/`security` as **global-only** grants, not RestMeta perms. The member-plane read `GET geo/policy` instead accepts `view_security`/`security` — global **or** granted on a GroupMember for the requested group (the response is confined to that group; see the geofence doc's Member Plane section).

### Incident App

| Model | VIEW_PERMS | SAVE_PERMS | Notes |
|-------|-----------|-----------|-------|
| Incident | view_security, **security** | manage_security, **security** | CREATE disabled |
| Event | view_security, **security** | manage_security, **security** | CREATE_PERMS = all |
| IncidentHistory | view_security, **security** | manage_security, **security** | |
| RuleSet | view_security, **security** | manage_security, **security** | |
| Rule | view_security, **security** | manage_security, **security** | |
| IPSet | view_security, **security** | manage_security, **security** | |
| Ticket | view_security, **security** | manage_security, **security** | |
| TicketNote | view_security, **security** | manage_security, **security** | |

### Communications (Push, Chat, Email, Phone)

| Model | VIEW_PERMS | SAVE_PERMS | Notes |
|-------|-----------|-----------|-------|
| PushConfig | manage_push_config, manage_groups, **comms** | manage_push_config, manage_groups, **comms** | |
| NotificationTemplate | manage_notifications, manage_groups, **comms**, owner, manage_users | manage_notifications, manage_groups, **comms** | |
| NotificationDelivery | view_notifications, manage_notifications, **comms**, owner, manage_users | manage_notifications, **comms** | |
| RegisteredDevice | view_devices, manage_devices, **comms**, owner, manage_users | manage_devices, **comms**, owner | |
| ChatRoom | **comms**, manage_chat, owner | manage_chat, **comms**, owner | CREATE_PERMS = authenticated |
| ChatMessage | **comms**, manage_chat | (read-only) | Created via WebSocket |
| ChatReaction | **comms**, manage_chat | (read-only) | Created via WebSocket |
| ChatReadReceipt | **comms**, manage_chat | (read-only) | Created via WebSocket |
| ChatMembership | **comms**, manage_chat | manage_chat, **comms** | |
| Mailbox | manage_aws, **comms** | manage_aws, **comms** | |
| EmailDomain | manage_aws, **comms** | manage_aws, **comms** | |
| EmailTemplate | manage_aws, **comms** | manage_aws, **comms** | |
| SentMessage | manage_aws, **comms** | manage_aws, **comms** | |
| IncomingEmail | manage_aws, **comms** | manage_aws, **comms** | |
| EmailAttachment | manage_aws, **comms** | manage_aws, **comms** | |
| PhoneNumber | view_phone_numbers, manage_phone_numbers, **comms**, manage_users | manage_phone_numbers, **comms**, manage_users | |
| PhoneConfig | manage_phone_config, manage_groups, **comms** | manage_phone_config, manage_groups, **comms** | |
| SMS | view_sms, manage_sms, **comms**, owner, manage_notifications | manage_sms, **comms**, manage_notifications | |

### Logit App

| Model | VIEW_PERMS | SAVE_PERMS | Notes |
|-------|-----------|-----------|-------|
| Log | manage_logs, view_logs, **security**, admin | admin, **security** | Logs are part of the security category |

### File Apps

| Model | VIEW_PERMS | SAVE_PERMS | Notes |
|-------|-----------|-----------|-------|
| FileManager | view_fileman, manage_files, **files** | manage_files, **files** | |
| File | view_fileman, manage_files, **files**, **owner** | manage_files, **files**, **owner** | `owner` = the initiating uploader (`OWNER_FIELD=user`); `DELETE_PERMS = manage_files, files, owner`. Owner may view/complete/attach/delete own file; list auto-scoped to own rows |
| FileRendition | view_fileman, manage_files, **files** | manage_files, **files** | |
| VaultFile | view_vault, manage_vault, **files**, owner | manage_vault, **files**, owner | |
| VaultData | view_vault, manage_vault, **files**, owner | manage_vault, **files**, owner | |

### Jobs App

| Model | VIEW_PERMS | SAVE_PERMS | Notes |
|-------|-----------|-----------|-------|
| Job | view_jobs, manage_jobs, **jobs** | manage_jobs, **jobs** | |
| JobEvent | manage_jobs, view_jobs, **jobs** | (system-created) | SAVE_PERMS = [] |
| JobLog | manage_jobs, view_jobs, **jobs** | (system-created) | SAVE_PERMS = [] |

**REST endpoints (non-RestMeta):** all jobs/rest/ control + status endpoints accept `manage_jobs`/`view_jobs`/`jobs`, but are gated with `@md.requires_global_perms(...)` (job control is platform-wide) — a group/member-scoped `jobs` grant does **not** authorize them (see [Global vs Group-Scoped Permission Checks](#global-vs-group-scoped-permission-checks)).

### Metrics App

Metrics does not use RestMeta models. Permission checking is handled via `check_view_permissions()` and `check_write_permissions()` in `mojo/apps/metrics/rest/helpers.py`. See [Metrics Permissions](../metrics/permissions.md) for details.

The `metrics` category grants full read+write access to all metrics operations. Fine-grained: `view_metrics` (read), `write_metrics` (record), `manage_metrics` (admin).

### Other Apps

| Model | VIEW_PERMS | SAVE_PERMS | Notes |
|-------|-----------|-----------|-------|
| ShortLink | manage_shortlinks, owner | manage_shortlinks, owner | |
| ShortLinkClick | manage_shortlinks | (read-only) | Analytics |
| Book | all | manage_docit, docs, owner | Public read |
| Asset | all | manage_docit, docs, owner | |
| Page | all | manage_docit, docs, owner | |
| PageRevision | all | manage_docit, docs, owner | |

## Resolved Issues

### 1. ~~`admin_security` vs `view_security`/`manage_security`~~ (Fixed)

Bouncer models now use `view_security`/`manage_security` instead of `admin_security`, aligned with the incident app. `manage_users` kept for backward compatibility.

### 2. ~~GeoLocatedIP uses `manage_users`~~ (Fixed)

GeoLocatedIP now accepts `view_security`/`manage_security` in addition to `manage_users`. Security admins can see firewall data without needing user management access.

### 3. ~~File models missing explicit SAVE_PERMS~~ (Fixed)

FileManager, File, and FileRendition now have `SAVE_PERMS = ["manage_files"]`.
File additionally carries the `"owner"` token in `VIEW_PERMS`/`SAVE_PERMS`/
`DELETE_PERMS` (ITEM-033) so the member who initiated an upload can complete and
manage their own file without `manage_files`/`files`.

## Known Issues

### 1. Event CREATE_PERMS = `all` (intentional)

The Event model allows anyone to create security events via `POST /api/incident/event`. This is the public reporting API used by frontend JavaScript to report security signals (XSS attempts, suspicious behavior, etc.). Viewing events requires `view_security`. This asymmetry is intentional and correct.

## Design Guidelines

When adding permissions to a new model:

1. **Always add the category permission** — every model should include its domain's category perm (`security`, `users`, `groups`, `comms`, `jobs`, `metrics`, `files`) in both VIEW_PERMS and SAVE_PERMS. (Since the checkers expand `view_X`/`manage_X` to also accept bare `X`, omitting it is no longer a lockout — but list it anyway for clarity.)
2. **Use view/manage pairs for fine-grained access** — `view_X` for read-only, `manage_X` for write
3. **Always include manage in view** — if `manage_X` is in SAVE_PERMS, add it to VIEW_PERMS too
4. **Use `owner` sparingly** — only for user-facing models where users manage their own records
5. **Group by domain** — all security models use `security` + `view_security`/`manage_security`, etc.
6. **Explicit over implicit** — always define SAVE_PERMS even if it's `[]` for read-only models
7. **Don't mix domains** — a security model shouldn't require `manage_users` permission
8. **Superuser covers everything** — `is_superuser` bypasses all checks, no need for a "system_admin" category
