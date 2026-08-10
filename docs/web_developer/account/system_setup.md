# System Setup API

The built-in Admin's **System Setup** page is the supported browser workflow
for checking and repairing an installation. These endpoints are intentionally
narrower than normal administrator APIs: the caller must be an active literal
superuser using an interactive Bearer JWT. API keys, group tokens, inactive
users, and permission-only non-superusers receive `403`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/account/admin/setup/options` | Sections and an active fix operation |
| `GET` | `/api/account/admin/setup/readiness?section=<code>` | Run all or one read-only report |
| `POST` | `/api/account/admin/setup/create` | Create/replay a `check` or `fix` operation |
| `GET` | `/api/account/admin/setup/detail?operation=<uuid>` | Reload/resume an operation |
| `POST` | `/api/account/admin/setup/advance` | Execute or reconcile one step |
| `POST` | `/api/account/admin/setup/choose` | Supply the current typed choice |
| `POST` | `/api/account/admin/setup/cancel` | Cancel between steps |

Operation creation and all advance/choose/cancel calls require authentication
within 600 seconds. HTTP `440 reauth_required` means return through Bouncer with
`force_reauth=1`, then retry. Mutable calls also require the same browser
`Origin` that created the operation. Do not synthesize or forward a different
Origin.

## Run checks

```http
POST /api/account/admin/setup/create
Origin: https://admin.example.com
Authorization: Bearer <interactive-superuser-jwt>
Content-Type: application/json

{"mode":"check","replay_key":"client-generated-uuid"}
```

Call `advance` once with the returned operation id. The terminal operation has
`status: "succeeded"` even when the readiness report contains a failed check;
the operation successfully measured the system. Use `report.overall` for
readiness.

Reports use `schema_version: 1` and the statuses `pass`, `warn`, `fail`, and
`pending`. Every check includes `code`, `explanation`, `remediation`,
`fixable`, and optional `required_choice` metadata.

## Fix and resume

Create with `mode: "fix"`, then call `advance` one step at a time. Persist only
the operation id in browser state. The server owns the cursor, step versions,
lease, log, and report.

When `status` is `waiting_for_choice`, render the exact
`current_step.choice_schema`. Submit:

```json
{
  "operation": "8d266835-1c5b-4434-9eb3-bb559b51ac64",
  "step_id": "base_url",
  "step_version": 1,
  "choice": {"base_url": "https://mojo.example.com"}
}
```

The choice is stored once under a row lock. A stale version, wrong step,
duplicate choice, unsupported field, or changed operation returns `409`/`400`
without moving the cursor. Reload with `detail`, render the returned current
step, and continue. Never replay an old choice optimistically.

Fix steps may remain `reconciling` after a successful mutation. This is
expected: the next `advance` proves provider state before marking the step
`proven`. An interrupted `mutation_attempted` step reconciles instead of
blindly repeating the provider write.

The operation log is bounded and safe to render as text. It never contains
credentials or reveal-once secrets. Treat any future secret/provider-specific
entry as a server defect; do not create UI that depends on such values.

## Protected settings

`BASE_URL`, installation identity, monitoring topic ownership, and expected
edge topology cannot be created, updated, renamed, or deleted through generic
`/api/settings`, regardless of permission. Use System Setup. Arbitrary Django
settings editing is not part of this API.

## Status handling

- `401`: JWT missing or expired.
- `403`: not an active literal superuser, machine credential, or Origin mismatch.
- `409`: active fix conflict, stale choice, active lease, or terminal operation.
- `440`: recent interactive authentication required.
