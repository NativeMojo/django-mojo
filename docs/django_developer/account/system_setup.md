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
)
```

Checks return one result dictionary or a list created with
`system_readiness.result()`. Stable statuses are `pass`, `warn`, `fail`, and
`pending`. Each row carries a stable code, explanation, remediation,
`fixable`, and optional typed choice metadata. The report schema is versioned
with `schema_version: 1`.

Fixers receive a bounded context and a validated non-secret choice. A fixer is
called once after its `mutation_attempted` intent is committed. The next
advance calls `reconcile` against authoritative state. A resumed operation
never blindly calls the fixer again. Do not put credentials, reveal-once
tokens, provider responses, or presigned URLs in results or choices.

## Protected system settings

The following global keys are protected:

- `BASE_URL`
- `MOJO_INSTALLATION_UUID`
- `MOJO_INSTALLATION_SLUG`
- `AWS_CLOUDWATCH_ALARM_TOPIC_ARNS`
- `EDGE_EXPECTED_TOPOLOGY`

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

Downstream apps may extend the boundary with
`register_protected_setting(key, validator)` during `AppConfig.ready()`. Keep
the writer allowlist narrow; this is not an arbitrary Django settings editor.

## Durable operation model

`account.SystemSetupOperation` stores:

- mode, optional section, replay fingerprint, creator, and bound Origin;
- versioned steps and a cursor;
- typed choices, bounded report, and a 200-entry safe operation log;
- short lease owner/expiry and terminal time.

Fix steps use `planned → mutation_attempted → reconciling → proven`, with
`waiting_for_choice` and terminal operation states. Only one fix operation may
be active. Check operations can coexist. Create calls with the same
`replay_key`, actor, mode, section, and Origin return the original operation.

Choices are accepted only for the current step id and version under a row
lock. Acceptance increments the step version, making repeated or stale forms
fail with `409`. Cancellation is allowed only between steps. A final read-only
readiness rerun is stored before a fix terminates.

Every endpoint revalidates an active literal superuser and refuses API-key or
group-token sessions. Create binds the browser's same-origin `Origin` header;
advance, choose, and cancel must present that exact origin. This operation-local
binding lets an administrator connected on an internal address save the future
public `BASE_URL` without broadening any other Admin request.

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
