# Packaged Admin Security client

The packaged Portal is **portal-mojo 0.2.22**, source revision
`a758fdf3d45877df629405377778179fa6cbb0bc` (tag `v0.2.22`). It is a clean canonical
macOS arm64 build with Node **24.21.0** and npm **11.19.0**.

- Manifest SHA-256: `1a26ef9695ed4094de0af267aeec35b50f1f33e8d2c90a0640ca8b17910e7c4a`
- Lockfile SHA-256: `426f33cc5f5527b7b33a2a268c33892ebe53116ab8346a61f97277e716b70d60`
- Inventory: 160 files

See [the packaging guide](../../django_developer/account/admin.md) for the
platform-bound reproducibility proof and offline replacement procedure.

This build simplifies navigation and Domains flows, separates **AI Assistant**
setup from **MCP connections**, and uses focused dialogs for Conversations,
Skills and Memory. Memory distinguishes personal, global and selected group
audiences; group read access does not imply permission to edit. The Admin
Dashboard is unchanged. Existing REST permissions and source-session protection
remain authoritative.

Included from earlier builds:

- **Passkeys** (user detail and My account) no longer crashes for users who
  hold a passkey. `transports` on `/api/account/passkeys` rows is a JSON list
  of strings (`[]` when none).
- **Set temporary password** is back in the user header menu and Security
  section (`sys.users` or `sys.manage_users`). It confirms first, calls
  `POST /api/account/admin/user/password/temporary` with fresh auth, and shows
  the generated password once in a copyable dialog.

Included from the earlier Assistant Setup build:

- **Assistant → Setup** is an owner-only page for the encrypted, write-only API
  key, enablement, automatic latest compatible Sonnet selection and manual model
  pins. It shows the resolved runtime model and catalogue freshness, using the
  [Assistant setup API](admin_portal/assistant.md); the browser never discovers provider models
  directly.

Included from the earlier 0.2.18 build:

- **Users → Sys Perms** shows every system permission a user holds, including
  custom keys outside the catalog, and can grant or remove them.
- Clicking the user avatar replaces or clears it.
- Compact overview cards in the user, group, member, device and GeoIP modals.
- Members: group-local write gates and invite role presets are available to
  products that compose the member panels themselves. The packaged Admin still
  requires system-level permissions to manage members and shows no role presets.
- **Storage → Media rendering** for rendition settings.
- Storage → Media rendering keeps Save and Reset disabled until the current override is read.
- The file-manager detail shows CDN status, and storage managers get Check CDN,
  Enable CDN… and Disable CDN… actions. Check CDN runs directly; Enable and
  Disable require confirmation. Results show as a findings list.
- Member admission and member-detail saves are fenced to their owner: switching
  account or active group ends the operation, and its late result is dropped.

Added in the earlier 0.2.8 build:

- **My account** in the avatar menu: password, email, passkeys and
  authenticator. An own password change keeps the user signed in and offers
  "Sign out everywhere else".
- **Edge** pages for vhosts, routes, upstreams and the blocklist.
- **Personal API Keys** shows only the signed-in user's own keys.
- A flat reply with HTTP 200 and a top-level `success: false` is now shown as a
  refusal on every screen, unless the call passes `refusal: 'return'`.
  `status: false` was already a failure.
- Model record operations (read, update, delete, actions) no longer inherit the
  active group. Scoped lists and creates, and custom calls on a record path,
  keep the scope they declare.

Identity & Access → **Sign-in** configures the platform login page and its Google, Apple and GitHub sign-in — see [Sign-in Setup API](signin_setup.md). Toast notifications remain visible
and interactive above modal backdrops, including nested dialogs. Phone Hub
connection tests read the provider's `success` verdict, retain the result in
Overview and Connection, and distinguish test mode from verified connectivity.
The detail subtitle and Connection section show whether the configuration is
the system default or belongs to a group. Editing is disabled during a pending
test, and saving credentials clears the previous result.

Confirmation dialogs explain the effect of disabling API keys or webhooks,
granting group/member management permissions, and removing metadata keys.

Its updated detail dialogs keep
navigation usable at narrow widths and put lifecycle actions in context menus.
Phone Hub also provides **SMS → Send SMS** with a recipient and custom body.
The composer requires SMS visibility plus `sys.send_sms` or `sys.comms` and
posts only `{to_number, body}` to `/api/phonehub/sms/send`, using the system
configuration and default sender. It shows the returned SMS record's
`data.status`, retains drafts after transport or malformed-response failures,
and refreshes SMS history after every attempted send. A successful envelope can
contain a `failed` or `undelivered` record; only `delivered` confirms delivery.
After an uncertain result, check the audit before retrying. The
[SMS endpoint reference](../phonehub/README.md#send-an-sms) documents the request
and response formats.

See the [packaged artifact guide](../../django_developer/account/admin.md)
for the exact release identity and offline installation proof.

Admin v2 exposes Security at `#/security-operations` with Overview, Cases,
Incidents & events, Rules, Firewall & IPSets, and Recommendations. This is the
seventh packaged feature destination. Admin v1 is compatible and still keeps its
original Activity surface; v2 Activity now contains only tickets and logs.

Use `features.security` from `GET /api/account/admin/bootstrap` to disclose the
destination. `enabled` plus `capabilities.view` is required to route there;
`capabilities.manage` controls mutation affordances. A missing/malformed block,
an absent Incident app, or an unauthorized legacy Activity hash must not render
Security. Portal admission (`view_admin`, `manage_users`, `manage_settings`, or
literal `admin`) is a separate prerequisite and never substitutes for
`view_security`, `manage_security`, or `security`.

All content comes from the version-3 Admin Security envelope. Every panel must
honor its own `status`, `cutoff`, `window`, and `truncated` values. Display exact
and sampled metrics separately. An unavailable, partial, missing, or stale
receipt is not success and is not an empty result.
For a truncated discovery list, the UI follows the opaque `next_cursor` through
`sections=<the same section>&page_cursor=<next_cursor>`; cursors are
server-bound to the authenticated scope, section, page size, and original
window snapshot. Large detail fields use a separate `chunk_cursor` and are
reassembled according to their `encoding` and digest.

Treat schema version 3 as necessary but not sufficient. The packaged client
strictly validates every requested envelope, row collection, action schema,
and firewall host summary. A malformed, contradictory, or oversized section
produces a contract-error view with no table or action controls; it is never
converted into synthetic empty or successful state.

The read is `GET /api/incident/admin/security`; mutations use
`POST /api/incident/admin/security/action`. Validated per-user API keys retain
their user's global-or-default-group read permissions. Group API keys/tokens
may read the exact authenticated group's evidence and cannot choose another
group in query data.
Writes remain global and use the deployment-configured freshness policy;
machine credentials do not need an interactive reauthentication they cannot
perform. The complete parameters, response envelopes, typed action
schemas, and error contract are in the
[Admin Security client contract](../security/README.md#admin-security-client-contract).

RuleSet forms and governed actions are generated from `sections=schemas`.
Bind actions to `modified`, require the advertised typed confirmation, and send
them once. On 409, reread authoritative state, discard/rebase the draft, and
ask for confirmation again. Never replay automatically.

For authentication recovery, retry a 401 once only for GET/HEAD. Do not replay
a POST after a 401. A 440 indicates the server refused the action before it ran;
perform recent authentication and retry once. If the session cannot be renewed,
remove authenticated chrome and send the user to sign-in with the exact current
path/query/hash as `redirect`. On installations using
`MOJO_APP_STATUS_200_ON_ERROR`, a framework failure arrives over HTTP 200 with
`status: false` and its real 400–599 status in `error_status`; the packaged
client applies the same 401/409/440 behavior to that effective status.

## QA without security mutations

Deterministic states are available through `bin/admin_preview
--security-state STATE`; they cover full/empty, unavailable/partial/failed/stale,
view-only/no-access, 401/440/409, recovery, and malformed-contract paths. The
opt-in Chrome rider is documented in the
[framework guide](../../django_developer/account/admin.md#preview-csp-and-release-proof).

For acceptance against a real installation, run:

```bash
bin/admin_preview --port 8766 --upstream https://api.example.com
# open http://localhost:8766/admin/v2/#/security-operations
```

Use a global `view_admin` + `view_security` operator without
`manage_security` when possible. Exercise navigation, status rendering,
complete evidence with secret-only scrubbing, legacy links, and session recovery only. Do not submit RuleSet,
recommendation, or IPSet confirmations; live-preview API calls reach the real
installation.

## Protected health snapshot read

`GET /api/account/admin/health/snapshot` reads the five-minute schema2 publication
without collecting. Optional `evidence_cursor` traverses bounded durable failed/
expired job evidence. Entry requires current global platform permission; AWS and
jobs facts use independent source grants. See [the full health snapshot contract](health_snapshot.md)
for immutable identity/generation/digest, per-slot expiry, limits and denial behavior.
