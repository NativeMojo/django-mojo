# Admin Portal API Guide — REST API Reference

This guide covers django-mojo's built-in Admin portal and the same APIs for web
developers building a custom internal console.

## Built-in portal

The built-in portal defaults to `/admin/` and uses the hosted Bouncer auth
pages. It provides System Setup/readiness, a system overview, User and Group
management, permanent Domains/Credentials/DNS/Certificates/Upstreams/Vhosts/
Routes pages, an Activity center for Incidents, Events, Logs, and Tickets, and
WebApp `MOJO_DEPLOY_KEY` management with light, dark, and system themes.

System Setup is a stricter surface than ordinary Admin pages: only an active
literal superuser with an interactive JWT can use it. See the
[System Setup API](system_setup.md) for its report, late-choice, resume,
fresh-auth, and Origin contracts.

Anonymous document requests receive only a small auth handoff. Private HTML,
JavaScript, and CSS require a short-lived path-scoped source cookie; anonymous
asset requests return `404`. That prevents unauthenticated HTTP browsing of the
implementation, but does not replace API authorization or hide source from an
authorized operator or artifact holder.

The browser obtains its source cookie with:

```http
POST /api/account/admin/session
Authorization: Bearer <interactive-user-jwt>
```

The caller needs a global `view_admin`, `manage_users`, or `admin` grant. API
keys and group-scoped tokens are refused. Normal portal data calls continue to
send the JWT and are authorized by each endpoint's own permissions.

The permanent network pages consume the public APIs documented by dnsman and
edge; there are no Admin-only write endpoints. DNS changes operate on complete
record sets, never retry a provider write after transport ambiguity, then read
the authoritative provider inventory. Until that proof matches, the affected
set is visibly refresh-required and further writes are blocked. Domain purchase
keeps the quote token in the confirmation modal only and requires the operator
to type the exact domain and price. Provider credentials are write-only and
cleared from the form after verification.

Certificates poll list/detail metadata only. Upstreams are declared or retired,
never repointed. The Vhost wizard exposes only the four structured edge shapes;
Routes are created sequentially and a partial failure offers repair of only the
missing rows. WebApps is the sole UI that receives the reveal-once deployment
token for create/rotate and offers revoke; System Setup only links there.

### Modular browser contract

The packaged portal is divided into six fixed, capability-gated feature lanes,
but primary navigation is exactly Dashboard, People, Web Apps, Platform, and
Activity. Advanced is a collapsed disclosure under Platform, alongside
deployments and literal-superuser System Setup. Activity owns the
bounded Incidents, Events, Logs, and Tickets operator journey. Platform owns
public/local health, UUID deployment recovery, fleet evidence, System Setup,
and readiness. Advanced owns bounded hosting/AWS inventory, typed settings,
and the raw Domains, Credentials,
DNS, Certificates, Upstreams, Vhosts, Routes, and network resources. Bootstrap
returns both the stable flat `capabilities` object and a namespaced `features`
object:

```json
{
  "capabilities": {"people": true, "network": true},
  "features": {
    "people": {"id": "people", "enabled": true,
               "capabilities": {"users": true, "groups": true}},
    "platform": {"id": "platform", "enabled": true,
                 "capabilities": {"setup": true}},
    "advanced": {"id": "advanced", "enabled": true,
                 "capabilities": {"view": true, "manage": true}},
    "activity": {"id": "activity", "enabled": true,
                 "capabilities": {"view_logs": true,
                                  "view_security": true,
                                  "manage_security": false}}
  }
}
```

Unknown namespaces are not loaded. Malformed server provider output disables
that namespace. The browser registry likewise imports a fixed set of local
descriptors; no URL, package name, or module path comes from user or deployment
settings.

Dashboard consumes `GET /api/account/admin/dashboard`, never Setup readiness.
Its independently permissioned evidence and status vocabulary are documented
in the [Dashboard API](admin_portal/dashboard.md).

Feature renderers receive `{ctx, route, navigate, signal}` and return one DOM
node. Honor the abort signal for fetches and attach a `dispose()` function to
the returned node when the feature owns timers, observers, or document-level
listeners. Route changes abort stale work, discard late nodes, close all nested
overlays, and focus the new page heading.

For searchable foreign-key inputs, the shared relationship control consumes
ordinary REST list and detail endpoints. Configure its endpoint, value/label
paths, optional `graph`, fixed filters, and capability boolean. It sends
encoded `search`, `start`, and `size` query parameters, preserves the REST
envelope's count and paging metadata, and submits the selected id through its
named hidden input. Treat endpoint permissions as authoritative: hiding or
disabling a control is only presentation, never authorization.

### Activity center contract

Activity uses the ordinary public REST lists; it never calls the legacy
`/api/incident/stats` aggregate. Each request sends `graph=activity`, an
explicit bounded `size`, `start`, and an allowlisted `sort`. The four sources
remain permission-separated:

| Tab | Endpoint | Capability |
|---|---|---|
| Incidents | `GET /api/incident/incident` | `view_security` |
| Events | `GET /api/incident/event` | `view_security` |
| Logs | `GET /api/logs` | `view_logs` |
| Tickets | `GET /api/incident/ticket` | `view_security` |

Incident and Ticket status controls appear only with `manage_security` and
write through their existing model REST save paths. Those paths remain the
authority for IncidentHistory and TicketNote audit records; Activity defines no
parallel mutation endpoint.

The table presents one persistent search field. Secondary filters and sorting
are collapsed behind a Filters button, which shows an active-filter count and
can be closed without clearing the query. Clear resets the visible query while
preserving subject context from a deep link.

The frozen hash vocabulary is:

```text
#/activity?tab=events&search=login&start=0&size=25&sort=-created
            &status=open&category=auth&level=9&kind=request
            &date_from=2026-08-01&date_to=2026-08-10
            &subject_type=group&subject_id=9
```

Allowed subject types are `incident`, `user`, `group`, and `model`; model links
also require `subject_model`. A subject is translated only when that source has
a stored scalar relationship (`incident`, `uid`/`user`, `group`/`gid`, or
`model_name` plus `model_id`). An unsupported combination displays an explicit
unsupported state and issues no list request, so a narrow deep link can never
silently become a global query.

The `activity` graphs avoid nested User/Group graphs. Ticket provides scalar
relation ids and minimal labels owned by the Ticket view; Event excludes
`geo_ip`, preventing a lookup per list row. Free-text search is restricted to
each model's explicit `SEARCH_FIELDS`; JSON metadata, request payloads, device
ids, credentials, and user-agent strings are not search columns. Structured
evidence is recursively masked for sensitive key names and bounded by depth,
item count, and string length before display or copy. This display protection
does not weaken the endpoint permission requirement.

Counts come from each permission-scoped list envelope. A successful empty list
shows `0`; a denied or failed source shows `Unavailable`. Search is debounced,
in-flight requests are aborted on replacement/navigation, and a stale page
offset is clamped to the last real page after the authoritative count arrives.

## What "Admin Portal" Means in Mojo

An admin portal is a frontend that calls privileged REST endpoints. Access is controlled by permissions stored on each user. Always design UI and API calls around explicit permission checks. A logged-in user is not automatically an admin.

## Base Pattern

1. Authenticate user (`POST /api/login`).
2. Store JWT securely and send `Authorization: Bearer <token>`.
3. Pass `group=<id>` when operating on group-scoped resources.
4. Handle `403` as "authenticated but missing permission".

## Permissions

### Category Permissions (Use These in Your UI)

Category permissions are the **recommended way** to assign access. Each category grants full read+write access to an entire domain. Display these as toggles in your admin portal's user/member permission editor.

| Permission | Label | What it unlocks |
|---|---|---|
| `users` | User Management | Users, passkeys, MFA, API keys, OAuth, devices, locations |
| `groups` | Group Management | Groups, members, group API keys, settings |
| `security` | Security & Logs | Incidents, events, rules, tickets, IP blocks, bouncer, GeoIP, system logs, geofence config |
| `comms` | Communications | Email, phone, SMS, push notifications, chat rooms, messages |
| `jobs` | Job System | Jobs, job events, job logs, runners, queue control |
| `metrics` | Metrics | All metrics — recording, fetching, categories, permissions |
| `files` | File Management | File managers, files, renditions, vault files, vault data |

**Superusers** (`is_superuser=true`) bypass all permission checks automatically. No category permissions needed.

### How to Display Permissions

Build your permission editor as a simple list of category toggles:

```
User Management     [on/off]
Group Management    [on/off]
Security & Logs     [on/off]
Communications      [on/off]
Job System          [on/off]
Metrics             [on/off]
File Management     [on/off]
```

This replaces the need to show 30+ individual permission toggles. Seven toggles cover the entire platform.

### Assigning Permissions via API

**Update a user's permissions:**

```
POST /api/user/<id>
```

```json
{
  "permissions": {
    "users": true,
    "groups": true,
    "security": true,
    "comms": true
  }
}
```

Requires `manage_users` or `users` permission.

**Update a group member's permissions:**

```
POST /api/group/member/<id>
```

```json
{
  "permissions": {
    "files": true,
    "comms": true,
    "jobs": true
  }
}
```

Requires `manage_groups`, `manage_group`, or `groups` permission.

**Invite a user with permissions:**

```
POST /api/group/member/invite
```

```json
{
  "email": "alice@example.com",
  "group": 7,
  "permissions": {
    "security": true,
    "files": true,
    "metrics": true
  }
}
```

Requires authentication — an unauthenticated call returns a clean `403`, never
a `500`. See [Invite a User](group.md#invite-a-user) for the full permission
list and error-response detail.

### Reading Current Permissions

Permissions are returned in the user and group member responses as a JSON object:

```json
{
  "id": 42,
  "username": "alice",
  "permissions": {
    "security": true,
    "files": true,
    "users": true,
    "comms": true,
    "jobs": true
  }
}
```

Use this to set the initial state of your toggle UI. Any key not present or set to `false` means the user does not have that permission.

### Fine-Grained Permissions (Advanced)

Category permissions cover most use cases. Fine-grained permissions exist for when you need read-only access or scoped access within a domain. You do **not** need to show these in a standard admin UI — they are for special cases.

Protected machine grants are not ordinary fine-grained toggles. `geoip_sync`,
`dnsman_acme_federation`, `edge_node`, and `mojosec_ingest` are protected from
group administrators; provision them only in a platform key-management
workflow. Their normal member assignment paths require matching global `sys.*`
grants, and a global `manage_users` / `manage_groups` administrator can also
assign them. They authorize fleet federation, private edge material, or
host-security ingestion. In particular, the
[ACME hub](../dnsman/README.md#acme-delegation-hub-optional) is machine-only:
even a JWT user holding similarly named permissions cannot call its
`/api/dnsman/acme/*` routes.

| Fine-Grained | Category | Use case |
|---|---|---|
| `view_security` | `security` | Read-only security dashboard (no edit access) |
| `view_geofence` | `security` | Read-only geofence admin section — rules, allowlist, simulate, bypass holders (no edit access) |
| `manage_geofence` | `security` | Manage geofence system rules and IP allowlist without full `security` |
| `view_users` | `users` | Read-only user directory |
| `view_groups` | `groups` | Read-only group listing |
| `view_fileman` | `files` | Read-only file browser |
| `view_logs` | `security` | Read-only log viewer (logs are part of `security`) |
| `view_jobs` | `jobs` | Read-only job monitoring |
| `view_metrics` | `metrics` | Read-only metrics dashboard |
| `manage_group` | `groups` | Manage only the user's own group (not all groups) |

If your portal needs a "read-only security viewer" role, assign `view_security` instead of `security`. But for most admin roles, the category permission is all you need.

### Protected Permissions

Some permission changes require the assigning user to have `manage_users` or `users`:

- `manage_users`, `manage_groups`, `view_logs`, `view_admin`, `manage_notifications`, `manage_files`, `manage_aws`
- All category permissions (`users`, `groups`, `security`, `comms`, `jobs`, `metrics`, `files`)

This prevents non-admin users from escalating their own access.

## Common Admin Endpoints

| Area | Endpoint(s) | Category Permission |
|---|---|---|
| User administration | `GET/POST /api/user`, `GET/POST /api/user/<id>` | `users` |
| Group administration | `GET/POST /api/group`, `GET/POST /api/group/<id>` | `groups` |
| Group membership | `POST /api/group/member`, `POST /api/group/member/<id>` | `groups` |
| Admin reset link | `POST /api/account/admin/user/password/reset` | Global `users`/`manage_users`, interactive JWT authenticated in the last 600 seconds |
| Admin temporary password | `POST /api/account/admin/user/password/temporary` | Same; plaintext appears once and forces replacement |
| Permission bundles | `GET/POST /api/account/admin/people/permission-bundles` | Global User view/manage; writes require interactive auth in the last 600 seconds |
| API-key lifecycle | `POST /api/account/admin/apikey/action` | Object edit authority, non-key session, interactive auth in the last 600 seconds |
| Secure settings | `GET/POST /api/settings`, `DELETE /api/settings/<id>` | `groups` |
| System Setup | `/api/account/admin/setup/*` | Literal active superuser only |
| Platform evidence/deploy recovery | `/api/account/admin/platform`, `/api/account/admin/platform/deploy/*` | Dedicated global Platform grants; writes require fresh non-key auth |
| Advanced evidence/settings | `/api/account/admin/advanced`, `/api/account/admin/advanced/settings` | Dedicated global Advanced grants; settings additionally require literal superuser |
| Security dashboard | `GET /api/incident/incident`, `GET /api/incident/event` | `security` |
| Firewall / IP blocks | `GET/POST /api/incident/ipset` — see [IPSet Bulk Blocking](../security/README.md#ipset-bulk-blocking) | `security` |
| Geofence admin | `GET/POST /api/geo/rules`, `GET/POST /api/geo/allowlist` — see [Geofence Admin](geofence.md) | `security` (or `view_geofence`/`manage_geofence`) |
| Bouncer devices | `GET /api/account/bouncer/device` | `security` or `users` |
| Bot signatures | `GET/POST /api/account/bouncer/bot_signature` | `security` or `users` |
| System logs | `GET /api/logs` | `security` |
| Email / SES | `GET/POST /api/aws/mailbox`, `GET/POST /api/aws/email_template` | `comms` |
| Phone numbers | `GET/POST /api/phonehub/phone` | `comms` |
| SMS | `GET/POST /api/phonehub/sms` | `comms` |
| Push notifications | `GET/POST /api/account/push/config` | `comms` |
| Chat rooms | `GET/POST /api/chat/room` | `comms` |
| Job system | `GET /api/jobs/status`, `GET /api/jobs/health` | `jobs` |
| Job control | `POST /api/jobs/control/*` | `jobs` |
| Metrics | `GET /api/metrics/fetch`, `POST /api/metrics/record` | `metrics` |
| Metrics permissions | `GET/POST /api/metrics/permissions` | `metrics` |
| File management | `GET/POST /api/fileman/manager`, `GET/POST /api/fileman/file` | `files` |
| WebApp key status | `GET /api/edge/webapp/key_status?webapp=<id>` | `view_dns`, `manage_dns`, or `security`, plus object access |
| WebApp key create/rotate | `POST /api/edge/webapp/link_key` | `manage_webapp`, recent interactive auth, plus object access |
| WebApp key revoke | `POST /api/edge/webapp/revoke_key` | `manage_webapp`, recent interactive auth, plus object access |
| WebApp onboarding | `/api/edge/webapp/onboarding/{options,create,detail,choose,cancel,workflow}` | `manage_webapp` or `security`, exact group/actor/origin; mutations require interactive auth |
| WebApp summary v1 | `GET /api/edge/webapp/summary?webapp=<id>` | `view_dns`, `manage_dns`, or `security`, plus object access |
| Domains and live DNS | `/api/dnsman/domain`, `/api/dnsman/dns*`, `/api/dnsman/registrar/*` | `view_dns` / `manage_dns`; adopt/discover are literal superuser only |
| DNS provider credentials | `/api/dnsman/credential`, `/api/dnsman/credential/link` | `view_dns` / `manage_dns`; secrets are write-only |
| Certificates | `/api/dnsman/certificate`, `/api/dnsman/certificate/request` | `view_dns` / `manage_dns`; portal never calls material |
| Upstreams | `/api/edge/upstream`, `/api/edge/upstream/declare`, `/api/edge/upstream/retire` | Read by DNS grants; declare/retire are literal platform-admin actions |
| Vhosts and Routes | `/api/edge/vhost`, `/api/edge/route` | `view_dns` / `manage_dns` / `security` |

> **Global grants required for platform-wide endpoints.** Job control/status,
> AWS ops and email admin (`cloudwatch/*`, `s3/bucket`, `email/send`,
> `email/domain[/<id>/onboard|audit|reconcile]`, `email/mailbox`,
> `email/template`, `email/incoming`, `email/sent` — these AWS/SES models are
> platform-global, not per-group), metrics permissions, geofence config
> (`/api/geo/*`), incident health, and cross-tenant user admin
> (`/api/auth/manage/*`, device lookup, device location, login-event reads,
> push send/config-test) are gated on the user's **global** permission grant. A
> permission held only at the group/member level — **or an API key** — does
> **not** authorize them (passing a `group` param does not change this, and a
> group API key cannot substitute a global grant). Grant these on the User, not
> the GroupMember. Genuinely group-scoped endpoints (group/member management,
> per-group webhook secret, ApiKey-federated SMS send) still accept a
> group-scoped grant as before.
>
> **This extends past the decorator-gated list above.** Any plain CRUD
> endpoint backed by a model with no per-group ownership — `/api/user`,
> `/api/system/geoip`, `/api/jobs/job`, `/api/jobs/event`, `/api/jobs/logs`,
> `/api/account/logins`, `/api/account/bouncer/*`, `/api/fileman/rendition` —
> also rejects a group API key by default, even if the key's `permissions`
> dict has the exact permission the model requires. There's no group to
> confine the key's access to, so the model-security layer denies it up
> front. See [API Keys](api_keys.md).

## Secure Settings API (Admin)

The secure settings API is intended for admin portals and configuration consoles.

`BASE_URL`, `MOJO_INSTALLATION_UUID`, `MOJO_INSTALLATION_SLUG`,
`AWS_CLOUDWATCH_ALARM_TOPIC_ARNS`, and `EDGE_EXPECTED_TOPOLOGY` are protected
system keys. Generic settings create/update/rename/delete requests refuse them
for every caller, including superusers. Configure them through System Setup.

### Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/settings` | List settings (requires `groups` or `manage_settings`) |
| `POST` | `/api/settings` | Create setting |
| `GET` | `/api/settings/<id>` | Get one setting |
| `POST` | `/api/settings/<id>` | Update setting |
| `DELETE` | `/api/settings/<id>` | Delete setting |

Enforcement-bearing keys registered with a write validator (most `GEOFENCE_*`
keys, plus app-registered keys) reject malformed values with a readable `400`
and are global-only — a group-scoped row for such a key is refused with
`"...is a global-only setting"`, and `is_secret: true` is refused too (a
validated setting cannot be masked). Other keys accept arbitrary values as
before.

`GEOFENCE_TEST_OVERRIDE` and `MOJO_TEST_MODE` are the exception: they are
**conf-file-only** (read from the Django settings file via `get_static`, never
from this API's backing store). `POST /api/settings` will still accept a row
for either key — there's no validator refusing the write — but it has **no
effect**: geofence enforcement and the `X-Mojo-Test-*` header plane never read
it. Don't expose these two as admin-console toggles; they are deploy-time
settings-file values only.

### Create a global setting

```json
POST /api/settings
{
  "key": "WEBHOOK_SECRET",
  "value": "super-secret-value",
  "is_secret": true
}
```

### Create a group-scoped setting

```json
POST /api/settings
{
  "group": 42,
  "key": "WEBAPP_BASE_URL",
  "value": "https://portal.example.com",
  "is_secret": false
}
```

### Response behavior for secrets

When `is_secret=true`, API responses include masked `display_value` (`"******"`). Build UIs to treat secret values as write-only unless the user explicitly replaces them.

### Listing with search

```http
GET /api/settings?search=WEBAPP&sort=key
```

## User MFA Status

The user default graph includes `requires_mfa` and `has_passkey`, so admin portals can display MFA status per user (and per group member, since member lists nest the user object):

```json
{
  "id": 42,
  "username": "alice@example.com",
  "requires_mfa": true,
  "has_passkey": true
}
```

- `requires_mfa` — whether this user must complete MFA at login (superuser-only writable)
- `has_passkey` — whether this user has at least one registered passkey

To enable MFA for a user (superuser only):

```
POST /api/user/<id>
{"requires_mfa": true}
```

## Admin Password Reset

Admins with `manage_users` can reset any user's password without knowing the current one:

```
POST /api/user/<target_id>
{"new_password": "NewPass##123"}
```

No forgot-password email is sent — the password is changed immediately. Password strength validation still applies.

---

## Example: Permission-Aware Frontend

```js
const headers = { Authorization: `Bearer ${token}` };

// Fetch current user to check their permissions
const me = await fetch('/api/user/me', { headers }).then(r => r.json());
const perms = me.data.permissions || {};

// Show/hide admin sections based on category permissions
if (perms.users)    showSection('user-admin');
if (perms.groups)   showSection('group-admin');
if (perms.security) showSection('security-dashboard');
if (perms.comms)    showSection('communications');
if (perms.jobs)     showSection('job-system');
if (perms.metrics)  showSection('metrics-dashboard');
if (perms.files)    showSection('file-manager');

// API calls — the server enforces permissions regardless of UI state
await fetch('/api/user?size=20&sort=-created', { headers });
await fetch('/api/group?size=50&sort=name', { headers });
await fetch('/api/settings?size=100&sort=key', { headers });
```

## Error Handling Contract

Use `status`, `code`, and `error` from the response envelope:

- `401`: not authenticated (login required / expired token)
- `403`: authenticated but missing required permission
- `404`: resource not found

Do not infer permission from UI state alone. Always trust API responses.

## Related References

- [Core Authentication](../core/authentication.md)
- [User API](user.md)
- [Group API](group.md)
- [Account Overview](README.md)
- [Security Dashboard](../security/README.md)
