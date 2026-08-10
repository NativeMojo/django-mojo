# System Setup and Readiness

System Setup is django-mojo's durable installation control plane. It gives the
built-in Admin a versioned readiness report and a resumable repair protocol;
HTTP views do not invoke management commands. The node command and portal both
reuse `mojo.apps.edge.services.sanity`.

## Readiness registry

Register an application section during app startup:

```python
from mojo.apps.account.services import system_readiness

system_readiness.register_section(
    "storage", "System storage", check_storage,
    fix=fix_storage, reconcile=reconcile_storage,
    choice_schema={
        "type": "object",
        "properties": {"bucket": {"type": "string"}},
        "required": ["bucket"],
        "additionalProperties": False,
    },
    order=40,
    definition_version=1,
)
```

Checks return one result dictionary or a list created with
`system_readiness.result()`. Stable statuses are `pass`, `warn`, `fail`, and
`pending`. Each row carries a stable code, explanation, remediation,
`fixable`, and optional typed choice metadata. The report schema is versioned
with `schema_version: 1`; it also includes `generated_at`, an aggregate
`overall`, counts in `summary`, and ordered `sections`. Each section has
`code`, `label`, aggregate `status`, `fixable`, and `checks`. Aggregate status
uses the precedence `fail`, `pending`, `warn`, then `pass`.

Fixers receive a bounded context and a validated non-secret choice. A fixer is
called once after its `mutation_attempted` intent is committed. The next
advance calls `reconcile` against authoritative state. A resumed operation
never blindly calls the fixer again. Do not put credentials, reveal-once
tokens, provider responses, or presigned URLs in results or choices.

The extension callback contract is:

- `check(context)` returns one `result()`-shaped dictionary or a list of them.
- `fix(context, choice)` applies one repeatable mutation; its return value is
  ignored. It must raise on failure.
- `reconcile(context, choice)` reads authoritative state and returns a boolean
  or `{"status": "proven"|"pending"}`. Only `proven` advances the cursor.
- `choice_schema` is an object schema or a callable returning one. Setup
  validates required and additional fields, `string`, `integer`, `boolean`,
  `enum`, and the `https-origin` format. Secret-shaped field names are refused.

The context contains the durable `operation`, the freshly validated advancing
`actor`, a trusted loopback `local_url` with `/api/version`, and bounded
`timeout` and `retries`. It never attributes a resumed write to the historical
creator. A section code and `definition_version` are persisted API identifiers:
keep the code stable and increment the version whenever old persisted steps
cannot safely run the new implementation. Register sections from
`AppConfig.ready()` before the first readiness request.

An untyped fixer or reconciler exception is ambiguous after mutation intent is
durable: Setup logs only its class and leaves the step `reconciling`. A fixer
may raise `system_readiness.DefinitiveSetupFailure` only when it has proved no
mutation occurred; that typed exception terminally fails the step. Never use it
for timeouts or provider 5xx responses.

When incrementing `definition_version`, keep a read-only reconciler for every
old version that may still be uncertain. Supply
`reconciliation_adapters={old_version: callable}` at registration or call
`register_reconciliation_adapter(code, old_version, callable)`. The adapter
receives the normal context and choice and returns only `proven` or `pending`;
it must never repeat the mutation. Planned/waiting stale steps remain safely
cancellable, while an uncertain stale step runs only its exact old adapter.

The public `BASE_URL` probe resolves the hostname, rejects the entire answer
set if any address is non-global, pins one approved address for the TCP
connection while preserving TLS SNI/hostname verification and the HTTP Host,
and refuses redirects. The local probe uses only a validated static loopback
listener or the WSGI `SERVER_PORT`; it never derives a destination from Host.
Set `SYSTEM_SETUP_LOCAL_API_URL` statically to an HTTP(S) loopback origin when
the application listener is not the request's `SERVER_PORT`. The local client
also ignores environment proxy configuration and never follows redirects.

## Protected system settings

The protected keys are initially absent; System Setup creates them only when
needed:

| Key | Stored value and initialization |
|---|---|
| `BASE_URL` | Canonical public HTTPS origin selected by the administrator |
| `MOJO_INSTALLATION_UUID` | UUID frozen on the first ownership operation |
| `MOJO_INSTALLATION_SLUG` | `AWS_MONITORING_NAME` when statically configured, otherwise `mojo-` plus the frozen UUID prefix |
| `AWS_CLOUDWATCH_ALARM_TOPIC_ARNS` | JSON list of non-empty topic ARN strings |
| `EDGE_EXPECTED_TOPOLOGY` | `{"nodes": [...], "pools": [...]}` with duplicate-free sorted string lists |

Generic `Setting` REST, `Setting.set()`, `save()`, renames, and deletes refuse
these keys even for a superuser. Application setup code uses the allowlisted
`system_settings.set_value(actor, key, value)` service, which re-reads the
actor and requires an active literal `account.User` superuser.

`BASE_URL` is a canonical public HTTPS origin: no credentials, query,
fragment, non-root path, localhost, or private/link-local address. It is
validated independently from the operation's bootstrap Origin.

`system_settings.installation_identity(actor)` freezes an immutable UUID and
resource slug under one database lock before owned-resource mutation. The
slug uses static `AWS_MONITORING_NAME` when configured, otherwise the UUID. It
never derives from `BASE_URL`, so correcting the public hostname cannot orphan
owned resources.
The generic protected setter cannot write either identity key. Every identity
read validates that both values exist, the UUID and slug are well formed, and
does not reinterpret them through mutable static configuration. Changing
`AWS_MONITORING_NAME` after the freeze cannot change or invalidate ownership.
All protected writes lock a
stable database row even when their `Setting` row is absent, and publish Redis
only after the database transaction commits.

Downstream apps may extend the boundary with
`register_protected_setting(key, validator)` during `AppConfig.ready()`. Keep
the writer allowlist narrow; this is not an arbitrary Django settings editor.
The optional validator receives `(key, value)`, returns the normalized value,
and raises `ValueError` with operator-safe text on invalid input. Omitting it
protects the key without transforming its value.

## Durable operation model

`account.SystemSetupOperation` stores:

- mode, optional section, replay fingerprint, creator, and bound Origin;
- versioned steps and a cursor;
- typed choices, bounded report, and a 200-entry safe operation log;
- short lease owner/expiry and terminal time.

The durable fields are `id`, `created`, `modified`, `finished_at`,
`created_by`, `mode`, `section`, `status`, `replay_fingerprint`,
`bound_origin`, `cursor`, `steps`, `choices`, `report`, `operation_log`,
`lease_owner`, and `lease_expires_at`. `created_by` is protected from deletion,
and the model orders newest first. It is internal orchestration state, not a
generic model REST surface.

Operation status and step state are deliberately separate:

- Operation statuses: `planned`, `running`, `waiting_for_choice`,
  `reconciling`, `succeeded`, `failed`, and `cancelled`.
- Step states: `planned`, `waiting_for_choice`, `mutation_attempted`,
  `reconciling`, `proven`, and `failed`.

Only one fix operation may be active; the database partial unique constraint
covers its four active statuses. Check operations can coexist. Create calls
with the same `replay_key`, actor, mode, section, and Origin return the original
operation. The replay key is optional, but callers should generate and retain
one for every user intent.

Choices are accepted only for the current step id, immutable
`definition_version`, and mutable `choice_revision` under a row lock.
Acceptance increments only the choice revision. Choose and advance both reject
a persisted step whose definition no longer matches the registered
implementation. Cancellation is allowed only while genuinely between steps or
waiting for a choice; `mutation_attempted` and `reconciling` cannot be
cancelled even after a lease expires. A final read-only readiness rerun is
stored before a fix terminates, and a fix succeeds only when that report is
fully `pass`.

`setup_safety.sanitize()` is the single boundary for direct reports, persisted
reports, choices/schemas, operation serialization, and log messages. It caps
depth, item count, string length, and total JSON bytes; pre-bounds huge strings
before parsing; redacts secret-shaped names, known credential formats, and
unlabeled high-entropy opaque values; and removes URL userinfo and query values.
Readiness results and logs use fixed schemas rather than accepting arbitrary
provider payload fields.

Every endpoint revalidates an active literal superuser and refuses API-key or
group-token sessions. Create binds the browser's same-origin `Origin` header;
advance, choose, and cancel must present that exact origin. This operation-local
binding lets an administrator connected on an internal address save the future
public `BASE_URL` without broadening any other Admin request.

Create, advance, choose, and cancel also require an interactive authentication
time no older than 600 seconds. Options, readiness, and detail are read-only and
do not use that freshness gate. The REST handlers and their complete schemas
are documented in the web-developer
[System Setup API](../../web_developer/account/system_setup.md).

## Built-in Admin source gate

The System Setup page is advertised only when bootstrap returns
`capabilities.setup: true`, which is derived from literal superuser status.
Its `assets/setup.js` module is in the private Admin asset allowlist: the
browser must first obtain the path-scoped Admin source session described in
[Built-in Admin Portal](built_in_admin.md). The source cookie controls delivery
of the UI bytes; the interactive JWT and endpoint checks remain authoritative
for every setup read and mutation.

## REST boundary

`mojo.apps.account.rest.system_setup` exposes the service without duplicating
its orchestration logic:

| Handler | Route | Additional gate |
|---|---|---|
| `on_setup_options` | `GET /api/account/admin/setup/options` | Read-only |
| `on_setup_readiness` | `GET /api/account/admin/setup/readiness` | Read-only; optional section |
| `on_setup_create` | `POST /api/account/admin/setup/create` | Fresh auth and same-origin create binding |
| `on_setup_detail` | `GET /api/account/admin/setup/detail` | Read-only operation resume |
| `on_setup_advance` | `POST /api/account/admin/setup/advance` | Fresh auth and bound Origin |
| `on_setup_choose` | `POST /api/account/admin/setup/choose` | Fresh auth, bound Origin, current definition version, and choice revision |
| `on_setup_cancel` | `POST /api/account/admin/setup/cancel` | Fresh auth and bound Origin |

All seven handlers deny key-backed sessions, require the global `admin` gate,
and then require an active literal superuser in the service. The mutation
handlers use the fixed 600-second freshness window. Keep HTTP handlers thin;
new provider setup belongs in a registered readiness section, not in a view.

## Management command compatibility

`manage.py sanity_check` preserves its ordered fail-fast output and exit
behavior. It is a thin adapter over `edge.services.sanity`, which checks Django
apps, database, migrations, Redis, and a real local HTTP request. The readiness
service additionally reports configured static directories and `BASE_URL`.

## Migration and tests

Migration `account.0050_systemsetupoperation` adds the durable model and the
partial unique constraint for one active fix. Coverage lives in
`tests/test_account/test_system_setup.py`, Admin source tests in
`tests/test_account/test_admin_portal.py`, and command compatibility in
`tests/test_edge/13_migrate_locked.py`.
