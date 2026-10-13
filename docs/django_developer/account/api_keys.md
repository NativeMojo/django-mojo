# API Keys — Django Developer Reference

API keys give programmatic clients authenticated access scoped to a group, without requiring a user login. They authenticate via the standard `Authorization` header and plug into the existing permission system with no special cases.

> **API Keys vs User Auth Tokens:** MOJO has two authentication mechanisms for programmatic access. **API Keys** (`ApiKey` model, `Authorization: apikey <token>`) are group-scoped with explicit permissions — use these for external integrations. **User Auth Tokens** (`User.generate_api_token()`, `Authorization: bearer <token>`) are JWT tokens that carry a user's full system-level permissions — use these only when you need to act as a specific user. See [REST API docs](../../web_developer/account/api_keys.md) for the REST-facing comparison.

## How It Works

```
Authorization: apikey <raw_token>
```

`AuthenticationMiddleware` routes this to `ApiKey.validate_token()`, which:

1. SHA-256 hashes the incoming token and looks it up by `token_hash`
2. Checks `is_active` and `expires_at`
3. Sets `request.group = api_key.group` and `request.api_key = api_key`
4. Stamps `last_used` via `api_key.touch_last_used()` — at most once per
   `API_KEY_TOUCH_SECONDS` (default 300): the loaded value is compared first,
   and only a stale or unset stamp costs a single-column UPDATE. `UserAPIKey`
   (a user's long-lived JWT, validated in `User.validate_jwt`) uses the same
   method and setting.
5. Returns a synthetic user object whose `has_permission` delegates to `api_key.has_permission`

From that point forward the request behaves like a **group-scoped** request against **group-owned** data — `RestMeta` permission checks, `requires_perms`, and `request.group` filtering all confine the key to its own group. This synthetic user is not a request `User`, so any code that touches group membership must be ApiKey-safe: see [`Group.get_member_for_user` / `user_has_permission`](group.md#membership) for the identity guard that makes group permission gates degrade to deny/`None` instead of raising for a non-`User` identity.

**A key is confined to its group — it cannot reach platform-global data.** Two gates enforce this beyond the group filter:

- **`@md.requires_global_perms`** — endpoints with platform-wide effect (job control, AWS infra, geofence config, etc.) reject an ApiKey identity by default, regardless of its permissions dict — [`is_request_user(request)`](../helpers/request.md#is_request_user) is `False` for an `ApiKey`, so it never reaches the permission check. Pass `allow_api_keys=True` only for a federation/machine-ingest surface.
- **Model security on groupless models** — a `uses_model_security` model that has **no `group` foreign key** (e.g. `User`, `GeoLocatedIP`, `Job`, `UserLoginEvent`) is platform-global; there is no group to confine a key to, so the model-security layer **denies ApiKey identities by default**. A model may opt in with `RestMeta.ALLOW_API_KEY_GLOBAL = True` (default `False`) — no model does initially. Without this, a key self-claiming `manage_users` could otherwise read every tenant's rows. `Group` (also groupless) confines a key to its own group + descendants on both list and detail. **`ALLOW_API_KEY_GLOBAL` is honored only on genuinely groupless models — setting it `True` on a model that has a `group` FK is a misconfiguration and is ignored (fail-closed, logged via `logit.error`), since such a key could otherwise reach unscoped rows by arriving with no active group context.**

Machine access to platform-global data should use a dedicated `allow_api_keys` endpoint (like the geoip federation sync) or a **service-account `User`** with a real global grant — not a group ApiKey. See [permissions.md](../core/permissions.md#global-vs-group-scoped-permission-checks).

## Permissions

Permissions are stored as a plain JSON dict on the key:

```python
{"view_data": True, "edit_data": True}
```

**Rules:**
- `sys.*` permissions are **always denied** — API keys have no backing user to escalate to
- `"all"` always returns `True`
- List/set input uses OR logic — any match grants access
- Everything else is a direct dict lookup

**The `sys.` prefix convention:**

In `GroupMember.has_permission`, a permission like `sys.manage_users` strips the prefix and checks `user.has_permission("manage_users")` — escalating to the user's system-level permissions. This is how endpoints enforce "only a real system-level user can do this, even within a group context." API keys have no backing user, so `sys.*` is unconditionally denied.

**Framework protection floor.** `APIKEY_PERMS_PROTECTION` is **not** empty by
default. `ApiKey.APIKEY_PERMS_PROTECTION_DEFAULTS` currently protects
`geoip_sync`, `dnsman_acme_federation`, `edge_node`, and `mojosec_ingest` with
their matching global `sys.*` grants. The effective map has three layers: the
platform-wide `Setting` row, with the settings-file value merged over it, with
`DEFAULTS` merged over both. Deployments may add protected permissions from
either source, but cannot replace or relax a framework-floor entry, and a
database row can neither loosen nor tighten a permission the settings file
names — the file wins. If either source is malformed the map is not read as
empty: every API-key permission change below a global
`manage_groups`/`manage_users` holder is refused, a key-backed session can
grant nothing, and an error is logged, until the value is fixed (see
[Settings reference](../helpers/settings_reference.md#apikey)). The merged map
is not exposed as a Setting value; a Setting row shows only what the
deployment added there.

**A key-backed session can never grant a protected permission.** Whatever its own permissions dict says, and regardless of the short-circuit described below, a request authenticated with an `ApiKey` is refused for any permission named in the effective protection map. Same reasoning as the acting-as block in `_can_manage_acting_user`: a confined credential must not be able to mint a successor carrying authority it does not legitimately hold. Unprotected permissions are unaffected, so key-provisions-key flows keep working.

**A no-op is never gated.** `set_permissions` checks each key in the incoming dict, but skips any whose value would not change the stored state. It reads the current state through `_get_permissions_dict()` rather than normalizing the column first — a `permissions` column holding a JSON *string* is a supported shape that `has_permission` authorizes off, and materializing it to `{}` before the gate would let an all-no-op payload wipe it with no authorization check at all. Without this, the admin UI would break: it submits the entire permission switch catalog on every save (including disabled switches), so an untouched protected permission rides along as `false` on every write — renaming a key, toggling `is_active`, creating any key at all — and gating that would return a bare 403. **Revoking** a protected permission still requires the authority to grant it, so this is not a loophole for stripping a federation key's access.

**Who can assign a key's permissions (`APIKEY_PERMS_PROTECTION`):** a key's `permissions` are gated on write by `ApiKey.can_change_permission` (mirroring `GroupMember`). A `manage_users`/`manage_groups` holder may assign anything — as of the bare-category expansion (see [Core → Permissions](../core/permissions.md#category-permissions-broad-access)), a holder of the combined `users`/`groups` term qualifies too, since it includes `manage_users`/`manage_groups` by definition. **That short-circuit reads whatever identity is on the request**, so for a `User` session it is a genuine global grant, but for a key-backed session it reads the *key's own* group-bounded dict — which is why a key-backed session is refused outright for any **protected** permission (below), before the short-circuit is reached. Otherwise a group admin could mint key A with the unprotected `groups` term and have A mint key B carrying a protected one. Where the short-circuit does not apply, the requester must be **a member of the key's group** (a global holder of a protected perm who is *not* a member is denied — the membership check comes first) and hold the perm required by the `APIKEY_PERMS_PROTECTION` setting (a `{perm: required_perm}` dict — the row, the settings file and the framework floor merged as described above; `sys.`-prefixed requirements escalate to a global grant). This stops a group admin from self-minting a key with permissions they aren't entitled to grant. `ApiKey.create_for_group(...)` sets permissions directly (trusted internal call) and is not gated. The REST setter (`set_permissions`) accepts only a real dict — any other payload shape, including a JSON-encoded string, raises `ValueException` (400) rather than being silently ignored.

**Why regular permissions are still group-scoped:**

`validate_token` sets `request.group` to the key's group when that group is active (else `None` — see Group Scoping below). In `rest_check_permission`, when `request.group` is set the check routes to the api_key branch and returns immediately — the system-level user permission branch is never reached. This means `manage_users` on an API key applies within the key's group only, not system-wide.

## Group Scoping

Every API key belongs to one group. The key can access that group and any of its **effectively active** descendants. If a request passes `group=<id>` in the request data and that group is not the key's group or a descendant, the dispatcher returns 403 before the endpoint runs. The answer is the same status and body (`Group not accessible with this API key`) whether the group belongs to someone else, is inactive or does not exist, so a key cannot learn which ids exist, and a refused request does not update the other group's `last_activity`. The same holds for a `GroupScopedToken`. A non-integer `group` is still a 400.

**Deactivating a group suspends its keys instantly (DM-037), and deactivating an ancestor suspends the whole subtree's keys (DM-048).** "Active" on every surface below means *effectively* active — the group **and every ancestor** (`Group.is_effectively_active`). The check is enforced at request time, so keys are never mutated — reactivating the group (or the ancestor) restores them immediately. It holds on every surface a key derives group context from:

- `validate_token` sets `request.group` only when the key's group is effectively active; a dark chain leaves it `None`, so a no-`group=` request fails closed at model security.
- A detail/save/delete op re-binds `request.group` from the target row's group; the model-security api_key branch re-checks effective activeness there too, so it fails closed rather than being revived by the re-bind.
- The RestMeta list fallback derives the key's groups from `ApiKey.get_groups`, which excludes effectively-inactive groups — so a deactivated tenant's rows never enumerate.
- `ApiKey.is_group_allowed` requires the target group to be effectively active. This also gates `Group.check_view_permission`/`check_edit_permission` (which run before the model-security branch), so a suspended tenant's key cannot read or write its own `Group` row — in particular it cannot flip `is_active` back on and un-suspend itself.
- The `@md.requires_perms` / `@md.requires_group_perms` decorators trust a non-User identity's permission dict only within an ACTIVE group context — a key with `request.group = None` is denied before its self-claimed perms are consulted. This closes custom (non-RestMeta) endpoints like `sms/send` too.
- **(DM-045)** For a group-scoped model, the model-security layer denies an inactive-group instance to an ApiKey identity *before* `check_view_permission`/`check_edit_permission` run — a future hook that naively grants via bare `request.api_key.has_permission(perms)` cannot reopen a suspended tenant's rows. See [Core → Instance-Level Permission Hooks](../core/permissions.md#instance-level-permission-hooks).

Not a hard token reject: an inactive-group key still **authenticates** (it returns `request.group = None`, not a 401). This preserves the group-independent federation path (`requires_global_perms(..., allow_api_keys=True)`, e.g. the geoip `/sync` receiver), which authorizes on the key's `has_permission` and ignores `request.group`. An **active child under an inactive parent is NOT reachable** (DM-048 overturned the old per-group carve-out): the child is effectively inactive, so `group=<child id>` resolves like a nonexistent id and the child's own keys go dark too. No flag is written to the child — reactivating the parent restores the entire subtree instantly.

## Acting as a Member (`user` + `override_user`)

A key can name the member it acts as. `ApiKey.user` is an optional FK to
`account.User`; `ApiKey.override_user` (default `False`) decides what that link
actually does.

| | `override_user=False` (default) | `override_user=True` |
|---|---|---|
| `request.user` | the `ApiKey` — unchanged | the linked `User` |
| `request.acting_user` | the linked `User` | the linked `User` |
| Authorization | the key's `permissions` dict | the member's, via `GroupMember` |
| Attribution (`FK(User)` columns) | the linked member | the linked member |
| Tenant boundary | the key's group | the key's group |

**Reference mode** (the default) exists because most consumers only need
attribution: before this, an `ApiKey` could not be stored in any
`ForeignKey("account.User")`, so apps hand-rolled resolvers that guessed a user
from the key's *name*. With a link set, `request.acting_user` is what the REST
layer stamps into `CREATED_BY_OWNER_FIELD`, so `created_by` / `note.user` and
friends record a real member with no per-app code. It grants **no authority** —
a reference-mode key with an empty `permissions` dict can still do nothing.

**Override mode** is the opt-in: the key *becomes* the member, and permissions
resolve through their `GroupMember` exactly as they would for a human. That is
the point — one place to manage access instead of maintaining the member's
permissions and a parallel key permissions dict.

### Who can be linked

Enforced by `validate_acting_user`, on both the REST setter and
`create_for_group`:

- **Never a superuser.** A hard block, not a warning — a key is a bearer token
  in a config file and must not be a route to platform-wide authority.
- **Only an active member of the key's own group**, with `check_parents=False`.
  Delegation must not climb: without this, an admin of a child group could link
  a key to a more privileged *parent*-group member.
- **The requester needs key-management perms** — global `manage_groups`/
  `manage_users`, or `manage_group`/`manage_members`/`manage_users`/
  `manage_groups` within the key's group. That bar is enough for a
  **reference-mode** link only. Making the key *assume* the member has a
  stricter one, below.
- **A linked key cannot be moved to a group its member is not in.** `group` is
  an ordinary writable field; a REST save that changes it on a linked key
  re-runs the two rules above against the new group and answers 403 when the
  member is not an active direct member there. An unlinked key moves as before.

Two incidents are raised on the **link** (never on use — a linked key serving
10k requests must not write 10k incidents): linking to a member holding
`manage_users`, `manage_groups`, or any `sys.*`; and enabling `override_user`
at all.

### Who may make a key assume a member

Managing a group is **not** enough to switch `override_user` on. A key that
assumes a member carries that member's authority, so a manager who could do it
for anyone would reach, through the key, whatever only the member may reach.
`ApiKey._can_assume(request, target)` is the one rule, and it is true only
when all of these hold:

- the requester passes the key-management bar above;
- the credential is a **person's own session** — `credential_kind(request)` is
  `"user"` or `"oauth"`. A per-user API key (`UserAPIKey`) is refused, self-issue
  included: it is a stored secret, and a second credential minted from it would
  outlive its revocation. Group API keys and group tokens were already refused;
- the requester **is the member**, or holds platform-wide `manage_users` or
  `manage_groups` (the way to provision a key for a service member).

Where it is enforced, all on the REST boundary:

| Request | Result when `_can_assume` is false |
|---|---|
| `override_user: true` on a key that has it off (create or edit) | 403 |
| `user: <other member>` on a key that already has `override_user` on | 403. The link and override are left as they were; override is never silently cleared |
| `GET ...?graph=token` on a key that assumes a member | 200 with `"token": null`, and an `api_key:token_read_refused` Log row in place of `api_key:token_read` |
| `POST /api/account/admin/apikey/action` with `rotate` on such a key | 403 |

Both setters call the rule, so the order of `user` and `override_user` in one
body cannot matter. Still open to a manager, because a manager must always be
able to reduce or stop a key: switching `override_user` off, clearing `user`,
`deactivate`, `reactivate`, `revoke` and delete. Unchanged: the create response
(the creator passed the rule in that same request) and `group/apikey/rotate`,
where the key rotates itself with its own token.

**Server code is trusted.** `ApiKey.create_for_group(...)`, a management
command or a shell is not a request and is not checked against this rule; the
data rules (never a superuser, only a direct member) still apply there. Whoever
can run code on the server can therefore create a key that assumes any member.

**Keys that assumed a member before this rule existed keep working.** They
sign in as before and carry no issuer record. The token and rotate rows above
apply to them at once, so a group manager can no longer read or admin-rotate
their tokens; the member or a platform administrator can. Authority a manager
created earlier, and any token already copied, stays live until the key is
rotated or revoked. To count them, in a Django shell (counts only, reads no
token):

```python
from mojo.apps.account.models import ApiKey
q = ApiKey.objects.filter(override_user=True, user__isnull=False)
print(q.count(), q.filter(is_active=True).count(), q.values("group").distinct().count())
```

### The issuer record

| Field | Meaning |
|---|---|
| `user_linked_by`, `user_linked_at` | Who linked the current member, and when |
| `override_enabled_by`, `override_enabled_at` | Who switched `override_user` on, and when |

Written only by `set_user` / `set_override_user`, from the request's user, and
by `create_for_group(issued_by=<User>)`. They are in `NO_SAVE_FIELDS` (the
`_id` aliases too), so a posted value is ignored. Each pair is cleared with
the value it describes: clearing `user` clears all four, switching override off
clears the second pair. Re-posting the same member does not re-stamp; re-pointing a key that already assumes a member stamps both pairs with the requester. They are
null on a key written before the fields existed, and on any key server code
created without `issued_by`. All four are in the `default`, `token` and `me`
graphs (the two users through the User `basic` graph).

The FKs are `SET_NULL`, so the record alone does not survive deletion of the
issuer's account. Switching override on therefore also writes an
`api_key:override_enabled` `logit.Log` row that names the member and the issuer
by id, beside the existing level 5 incident.

### What an override key still cannot do

- **`sys.*` is always denied**, regardless of who the key acts as.
- **The tenant boundary is the key's group.** If the member also belongs to
  other groups, the key does **not** reach them — model security refuses to
  rebind `request.group` outside the key's own tree for a key-backed session.
- **It cannot change the member's credentials.** This is the guarantee that
  makes the feature safe: *revoking the key revokes the access*. Registering a
  passkey, minting a user auth token, enrolling or disabling MFA, or moving the
  email/phone used for recovery would all outlive the key, so
  `@md.denies_key_backed_session()` refuses them. See
  [Core → Permissions](../core/permissions.md).
- **It cannot edit `User` rows.** `User.check_edit_permission` denies whenever
  `request.api_key` is set, which covers the password, username, email, phone
  and MFA setters and actions.
- **It cannot satisfy `requires_global_perms(allow_api_keys=False)`**, and it
  does not inherit the member's *global* permission dict — `requires_perms`
  resolves a key-backed session through the group instead, because the global
  dict has no tenant bound.

**Residual risk, stated plainly:** an override key linked to a privileged
member *is* privileged, within its group. That is inherent to what the feature
does. The link rules control who can create it, the incident makes it
discoverable, the credential block stops it becoming permanent, and deleting
the key ends the access — but choosing whom to link is a real decision.

Three limits of the assume rule, so nobody reads more into it:

- **A key keeps up with its member.** A member's own override key gains what
  the member is granted later. That grant is checked against the grantor when
  it is made (`GroupMember` permission rules), and no ceiling tied to the
  issuer is applied at sign-in.
- **A manager can still build a member and act as it.** A manager who can
  create a member, grant it permissions and sign in as it can self-issue a key
  for it. The key carries only what the manager could already grant; protected
  permissions stay refused at grant time.
- **Membership is not re-read at sign-in.** A member removed from the group
  after issuing a key still authenticates with it; group checks then find no
  member row for them.

### Deactivation

If the linked member is deactivated, the key stops authenticating and returns
the **existing** `"API key is inactive"` message — deliberately not a distinct
string, which would turn the endpoint into an account-state oracle for anyone
holding the token.

Clearing `user` also clears `override_user`; a key set to assume nobody is not
a valid state.

## Creating Keys

### Programmatically

```python
from mojo.apps.account.models import ApiKey

api_key, raw_token = ApiKey.create_for_group(
    group=my_group,
    name="Mobile App v2",
    permissions={"view_orders": True, "create_orders": True},
)
# Optional: user=<member>, override_user=True, issued_by=<User>. issued_by is
# recorded as the issuer (see "The issuer record"); it is not checked.
# raw_token is a 48-char alphanumeric string. It is also stored encrypted on the
# row, so api_key.get_token() returns it later — you do not have to persist it to
# recover it. Treat it as a live credential regardless.
```

### Via REST

```
POST /api/group/apikey
```

```json
{
  "group": 42,
  "name": "Mobile App v2",
  "permissions": {"view_orders": true, "create_orders": true}
}
```

The raw token is included in the creation response under `data.token`. It is **also stored encrypted** via `MojoSecrets`, so it can be retrieved later — `ApiKey.get_token()` server-side, and over REST through the **opt-in** `token` graph (`GET /api/group/apikey/<id>?graph=token`), which is audited. This is not a "shown once" credential, but the secret is **not** on the `default` graph: ordinary reads and list responses never carry it. See [Security Notes](#security-notes).

```json
{
  "status": true,
  "data": {
    "id": 7,
    "name": "Mobile App v2",
    "token": "aB3kR9...48chars",
    "is_active": true,
    "permissions": {"view_orders": true, "create_orders": true},
    ...
  }
}
```

## Rate Limit Overrides

The `limits` field stores per-endpoint rate limit overrides used by `@md.rate_limit` and `@md.strict_rate_limit`:

```python
api_key, token = ApiKey.create_for_group(
    group=my_group,
    name="High-volume integration",
    permissions={"view_orders": True},
    limits={"orders": {"limit": 500, "window": 60}},  # window in minutes
)
```

See [Rate Limiting](../core/rate_limiting.md) for full details.

## REST Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/group/apikey` | List keys for a group (no tokens) |
| `GET` | `/api/group/apikey?graph=token` | List keys **with their live raw tokens** (bulk read, audited) |
| `POST` | `/api/group/apikey` | Create a key (response includes the raw token) |
| `GET` | `/api/group/apikey/<id>` | Get key details (no token) |
| `GET` | `/api/group/apikey/<id>?graph=token` | Get key details **plus the live raw token** (audited) |
| `POST` | `/api/group/apikey/<id>` | Update name, permissions, limits, is_active |
| `DELETE` | `/api/group/apikey/<id>` | Delete key |
| `GET` | `/api/group/apikey/me` | Whoami — the **calling** key's own identity + permissions |
| `POST` | `/api/group/apikey/rotate` | Rotate the **calling** key's secret in place; returns the new token once |

The CRUD endpoints require `manage_group` or `manage_groups` permission. The
`me` and `rotate` endpoints require only that the request is authenticated
**with an API key** (`@md.requires_auth()`) — no management permission (the caller
already holds the secret).

`manage_group` is intentionally a **full-trust credential-management grant**
inside the group. That trust boundary also applies to key-backed callers: an
API key carrying `manage_group` may request the `token` graph for sibling keys
and recover their live tokens. The framework does not block that path. Give
integrations narrower permissions when they must not administer credentials.

**One exception: a key that assumes a member.** Its token is that member's
authority, not the group's, so the `token` graph returns `null` for it, and the
admin `rotate` action is refused, unless the caller is the member or a platform
administrator on their own session. No API key can read it. See
[Who may make a key assume a member](#who-may-make-a-key-assume-a-member).

### `GET /api/group/apikey/me` — whoami

A self-introspection endpoint for service principals, analogous to
`GET /api/user/me` for human users. It lets a key holder confirm the token
is valid and inspect what the key is allowed to do, without holding any
management permission.

- Authenticate with `Authorization: apikey <token>`.
- A user/JWT-authenticated request has no API key and gets **401** — those
  callers should use `GET /api/user/me` instead.
- Serialized with the `me` graph: `id`, `created`, `name`, `is_active`,
  `permissions`, `limits`, `last_used`, `expires_at`, plus the nested
  `group` (basic). The graph is forced server-side — a `?graph=` override
  is ignored — so the raw `token` is **never** returned.

```json
{
  "status": true,
  "data": {
    "id": 7,
    "name": "sms-bridge",
    "is_active": true,
    "permissions": {"send_sms": true},
    "group": {"id": 12, "name": "Acme Co"},
    "last_used": "2026-05-20T17:04:00Z",
    "expires_at": null
  }
}
```

This is what `PhoneConfig.test_connection()` calls to validate a `mojo`
SMS-provider configuration without sending a real message.

### `POST /api/group/apikey/rotate` — rotate self

Rotates the **calling** key's secret **in place**: same key id, name,
permissions, and limits — a brand-new token. The **previous** token is
invalidated immediately (its hash *and* its encrypted copy are overwritten)
and cannot be recovered. The **new** token is returned in this response; it is
not write-once — like any ApiKey token it stays readable afterwards via
`ApiKey.get_token()` and the opt-in `token` graph until the next rotation.

- Authenticate with `Authorization: apikey <token>` (the key being rotated).
- Self-service: no management permission — the caller already holds the secret
  (same trust model as `me`). A user/JWT request has no API key and gets **401**.
- Returns the `me` graph **plus** the new `token`:

```json
{
  "status": true,
  "data": {
    "id": 7,
    "name": "sms-bridge",
    "is_active": true,
    "permissions": {"send_sms": true},
    "group": {"id": 12, "name": "Acme Co"},
    "token": "<new-48-char-token>"
  }
}
```

Use it for scheduled credential rotation: a service rotates its own key, stores
the returned token, and continues — no second key, no `manage_group` grant, no
gap where the old secret lingers. (`ApiKey.rotate_token()` is the model-level
equivalent.)

## Lifecycle

```python
# Deactivate without deleting
api_key.is_active = False
api_key.save()

# Set expiry
from mojo.helpers import dates
api_key.expires_at = dates.utcnow() + dates.timedelta(days=90)
api_key.save()

# Rotate in place — the prior token stops authenticating immediately
new_token = api_key.rotate_token()
```

## Security Notes

- **The raw token is stored, not just hashed.** `token_hash` (SHA-256) is for indexed lookup; the raw token itself lives in `mojo_secrets`, encrypted via `MojoSecrets` (AES-256-GCM, key via PBKDF2). `ApiKey.get_token()` recovers it.
- **Key-derivation caveat.** `MojoSecrets._get_secrets_password()` derives the key from `{created}{pk}{ClassName}` — all plaintext columns on the same row, with no server-side secret mixed in. This protects against exfiltration of the `mojo_secrets` column on its own; it does **not** protect against a row-level or full-table dump. Treat `account_apikey` as a table of live credentials and protect it like one. (`KSMSecrets` — the KMS-backed base in the same module — is a stronger option; `ApiKey` does not use it today.)
- **Token read-back is opt-in and audited.** The secret is on the `token` graph only — `GRAPHS["token"]` carries `("rest_get_token", "token")`. `default` (which list responses fall back to, since `ApiKey` defines no `list` graph) and `me` both omit it, and `/rotate` uses the forced `me` graph plus an explicit token field. Every export through `rest_get_token()` writes an `api_key:token_read` `logit.Log` row — one per serialized key, so a bulk read via `?graph=token` on a list is visible once per credential. **The permission bar is unchanged**: `graph=token` is open to the same `manage_group` / `manage_groups` / `groups` holders as any other read, including an API key that carries one of those grants. This is deliberate: those permissions confer full credential custody inside the group. What changed is that the credential no longer rides along on requests that never asked for it — not who may ask. If such a management key is compromised, rotate or revoke every sibling credential it could have read; revoking only the original key is not sufficient containment.
- **An unrecognized graph name is refused, not silently served.** `token` is a *special* graph name, not one of the common names (`default`, `basic`, `list`, `simple`, `detail`, `detailed`, `full`) that fall back to `default` — so a typo like `?graph=tokens` is refused with `400` rather than quietly returning the key without the token. See [Core → Graphs](../core/graphs.md#graph-resolution-fallback-refusal-and-the-whole-model-guard).
- **The opt-in read works on lists too.** `GET /api/group/apikey?graph=token` returns a live token for every key in the group, so it is a bulk credential read. It is audited once per key, but the list endpoint has no maximum page size — size the blast radius of a `groups` grant accordingly.
- **Credential routes suppress bodies in every logging mode.** Request and response logging recognizes `/api/group/apikey*` before inspecting content and writes only a fixed marker, including with `LOGIT_FILE_ALL` or `LOGIT_DEBUG_ALL`. The recursive sanitizer remains defense in depth for token-shaped keys elsewhere.
- **Admin People lifecycle actions are step-up protected.** `POST /api/account/admin/apikey/action` supports `rotate`, `deactivate`, `reactivate`, and `revoke`, denies API-key/group-token sessions, and requires an interactive JWT authenticated in the last 600 seconds. Creation and rotation return plaintext once to the caller. The older audited `graph=token` recovery contract remains compatible, but the built-in Admin portal never calls it.
- **`DENY_AI = True`.** The assistant's model tools cannot read this table at all. This is the hard opt-out and it stays, independently of how the assistant serializes: its model tools no longer accept a graph name (they read a model through its `ai` graph, else `default`, so the `token` graph is unreachable through them), but a table of live credentials has no business in an LLM's context even through `default`.
- `sys.*` permissions are unconditionally denied
- Expired or inactive keys return 401
- Group scope is enforced at the dispatcher level — keys cannot access groups outside their hierarchy
- The raw `token_hash` and `mojo_secrets` **columns** are never serialized — but note that the decrypted token is still reachable via the `token` graph above, so "the secret columns are hidden" is not the same as "the secret is unreachable"
