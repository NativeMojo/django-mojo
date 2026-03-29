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

## Permission Names

### Category Permissions (Broad Access)

Category permissions grant full read+write access to an entire domain. Use these for admin roles that need everything in a domain without toggling individual permissions.

| Permission | Domain | Grants access to |
|-----------|--------|-----------------|
| `security` | Security | All incidents, events, rules, tickets, IPSets, bouncer devices/signals/signatures, GeoLocatedIP |
| `users` | Users | All user records, passkeys, TOTP, API keys, OAuth, devices, locations, bouncer/GeoLocatedIP |
| `groups` | Groups | Groups, members, group API keys, settings |
| `phone` | Phone | Phone numbers, phone config, SMS |
| `push` | Push | Push config, notification templates, delivery records, registered devices |
| `files` | Files | File managers, files, renditions, vault files, vault data |
| `chat` | Chat | Chat rooms, messages, reactions, receipts, membership |
| `email` | Email | Mailboxes, domains, templates, sent/incoming messages, attachments |
| `logs` | Logs | All log entries (read + write) |
| `docs` | Docs | Books, pages, assets, revisions (write — read is public) |

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
| `manage_aws` | Email | SES email templates, domains, mailboxes |
| `manage_chat` | Chat | Create/manage chat rooms and membership |
| `view_fileman` | Files | Read file manager records |
| `manage_files` | Files | Upload and manage files |
| `view_vault` | Files | Read vault files and data |
| `manage_vault` | Files | Write vault files and data |
| `manage_docit` | Docs | Manage documentation books, pages, assets |
| `view_logs` | Logs | Read logit entries |
| `manage_logs` | Logs | Write/delete logit entries |
| `admin` | Logs | Full admin access to logit |
| `manage_notifications` | Push | Manage notification templates and delivery |
| `view_notifications` | Push | Read notification delivery records |
| `manage_devices` | Push | Manage registered push devices |
| `view_devices` | Push | Read registered push devices |
| `manage_push_config` | Push | Push notification configuration |
| `view_phone_numbers` | Phone | Read phone number records |
| `manage_phone_numbers` | Phone | Manage phone numbers |
| `manage_phone_config` | Phone | Phone hub configuration |
| `view_sms` | Phone | Read SMS records |
| `manage_sms` | Phone | Send and manage SMS |
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

### Push Notifications

| Model | VIEW_PERMS | SAVE_PERMS | Notes |
|-------|-----------|-----------|-------|
| PushConfig | manage_push_config, manage_groups, **push** | manage_push_config, manage_groups, **push** | |
| NotificationTemplate | manage_notifications, manage_groups, **push**, owner, manage_users | manage_notifications, manage_groups, **push** | |
| NotificationDelivery | view_notifications, manage_notifications, **push**, owner, manage_users | manage_notifications, **push** | |
| RegisteredDevice | view_devices, manage_devices, **push**, owner, manage_users | manage_devices, **push**, owner | |

### Chat App

| Model | VIEW_PERMS | SAVE_PERMS | Notes |
|-------|-----------|-----------|-------|
| ChatRoom | chat, manage_chat, owner | manage_chat, **chat**, owner | CREATE_PERMS = authenticated |
| ChatMessage | chat, manage_chat | (read-only) | Created via WebSocket |
| ChatReaction | chat, manage_chat | (read-only) | Created via WebSocket |
| ChatReadReceipt | chat, manage_chat | (read-only) | Created via WebSocket |
| ChatMembership | chat, manage_chat | manage_chat, **chat** | |

### Email App (AWS)

| Model | VIEW_PERMS | SAVE_PERMS | Notes |
|-------|-----------|-----------|-------|
| Mailbox | manage_aws, **email** | manage_aws, **email** | |
| EmailDomain | manage_aws, **email** | manage_aws, **email** | |
| EmailTemplate | manage_aws, **email** | manage_aws, **email** | |
| SentMessage | manage_aws, **email** | manage_aws, **email** | |
| IncomingEmail | manage_aws, **email** | manage_aws, **email** | |
| EmailAttachment | manage_aws, **email** | manage_aws, **email** | |

### Logit App

| Model | VIEW_PERMS | SAVE_PERMS | Notes |
|-------|-----------|-----------|-------|
| Log | manage_logs, view_logs, **logs**, admin | admin, **logs** | |

### File Apps

| Model | VIEW_PERMS | SAVE_PERMS | Notes |
|-------|-----------|-----------|-------|
| FileManager | view_fileman, manage_files, **files** | manage_files, **files** | |
| File | view_fileman, manage_files, **files** | manage_files, **files** | |
| FileRendition | view_fileman, manage_files, **files** | manage_files, **files** | |
| VaultFile | view_vault, manage_vault, **files**, owner | manage_vault, **files**, owner | |
| VaultData | view_vault, manage_vault, **files**, owner | manage_vault, **files**, owner | |

### Docit App

| Model | VIEW_PERMS | SAVE_PERMS | Notes |
|-------|-----------|-----------|-------|
| Book | all | manage_docit, **docs**, owner | Public read |
| Asset | all | manage_docit, **docs**, owner | |
| PageRevision | all | manage_docit, **docs**, owner | |
| Page | all | manage_docit, **docs**, owner | |

### Other Apps

| Model | VIEW_PERMS | SAVE_PERMS | Notes |
|-------|-----------|-----------|-------|
| PhoneNumber | view_phone_numbers, manage_phone_numbers, **phone**, manage_users | manage_phone_numbers, **phone**, manage_users | |
| PhoneConfig | manage_phone_config, manage_groups, **phone** | manage_phone_config, manage_groups, **phone** | |
| SMS | view_sms, manage_sms, **phone**, owner, manage_notifications | manage_sms, **phone**, manage_notifications | |
| ShortLink | manage_shortlinks, owner | manage_shortlinks, owner | |
| ShortLinkClick | manage_shortlinks | (read-only) | Analytics |

## Resolved Issues

### 1. ~~`admin_security` vs `view_security`/`manage_security`~~ (Fixed)

Bouncer models now use `view_security`/`manage_security` instead of `admin_security`, aligned with the incident app. `manage_users` kept for backward compatibility.

### 2. ~~GeoLocatedIP uses `manage_users`~~ (Fixed)

GeoLocatedIP now accepts `view_security`/`manage_security` in addition to `manage_users`. Security admins can see firewall data without needing user management access.

### 3. ~~File models missing explicit SAVE_PERMS~~ (Fixed)

FileManager, File, and FileRendition now have `SAVE_PERMS = ["manage_files"]`.

## Known Issues

### 1. Event CREATE_PERMS = `all` (intentional)

The Event model allows anyone to create security events via `POST /api/incident/event`. This is the public reporting API used by frontend JavaScript to report security signals (XSS attempts, suspicious behavior, etc.). Viewing events requires `view_security`. This asymmetry is intentional and correct.

## Design Guidelines

When adding permissions to a new model:

1. **Always add the category permission** — every model should include its domain's category perm (`security`, `users`, `groups`, `phone`, `push`, `files`, `chat`, `email`, `logs`, `docs`) in both VIEW_PERMS and SAVE_PERMS
2. **Use view/manage pairs for fine-grained access** — `view_X` for read-only, `manage_X` for write
3. **Always include manage in view** — if `manage_X` is in SAVE_PERMS, add it to VIEW_PERMS too
4. **Use `owner` sparingly** — only for user-facing models where users manage their own records
5. **Group by domain** — all security models use `security` + `view_security`/`manage_security`, etc.
6. **Explicit over implicit** — always define SAVE_PERMS even if it's `[]` for read-only models
7. **Don't mix domains** — a security model shouldn't require `manage_users` permission
8. **Superuser covers everything** — `is_superuser` bypasses all checks, no need for a "system_admin" category
