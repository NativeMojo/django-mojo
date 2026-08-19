# AWS Maintenance API

Pending managed-service upgrades, and applying one of them. Five endpoints back
the Admin portal's **Maintenance** route.

Reads require the `manage_aws` permission, checked as a **global**
`User.permissions` grant only (`@md.requires_global_perms`) — a `manage_aws`
grant held at the group/member level does not authorize them.

The apply endpoint additionally requires a platform management tier. That is an
**AND**, not an either/or:

| Endpoint | Permissions |
|---|---|
| `GET /api/aws/maintenance/versions` | `manage_aws` |
| `GET /api/aws/maintenance/status` | `manage_aws` |
| `POST /api/aws/maintenance/apply` | `manage_aws` **and** (superuser or `manage_platform` or `admin`) |
| `GET /api/account/admin/platform/framework` | `view_platform`, `manage_platform`, or `admin` |
| `POST /api/account/admin/platform/framework/update` | `manage_platform` or `admin` |

Both POST endpoints refuse key-backed sessions (ApiKey, GroupScopedToken) and
require authentication within the last 600 seconds. A stale session gets
HTTP 440 `reauth_required`; re-authenticate and retry.

---

## GET /api/aws/maintenance/versions

Every managed database and cache running a major version with a newer one
available, with live status.

**Query parameters**

| Name | Type | Notes |
|---|---|---|
| `refresh` | `1`/`true`/`yes` | Bypass the 10-minute server cache and re-scan |

**Response** (`data`)

```json
{
  "schema_version": 1,
  "status": "ok",
  "region": "us-east-1",
  "generated_at": "2026-08-18T18:00:00+00:00",
  "level": 8,
  "scheduled": true,
  "findings": [
    {
      "kind": "rds-instance",
      "resource_id": "mojo-prod-postgres",
      "engine": "postgres",
      "current_version": "14.11",
      "available_major": "16.4",
      "deadline": "2026-09-10T00:00:00+00:00",
      "extended_deadline": null,
      "days_remaining": 23,
      "note": "postgres 14.11 has a major upgrade to 16.4 (current major 14)",
      "release_notes_url": "https://docs.aws.amazon.com/...",
      "status": "available",
      "pending_version": null,
      "settled": true
    }
  ],
  "warnings": []
}
```

| Field | Meaning |
|---|---|
| `status` | `"ok"`, or `"unavailable"` with a `reason` when AWS could not be reached at all (no credentials is the normal local state) |
| `kind` | `rds-instance`, `rds-cluster`, or `elasticache` — pass it back verbatim on apply and status |
| `available_major` | The **only** version this installation will apply for that resource |
| `days_remaining` | `null` when AWS publishes no end-of-life date. ElastiCache never publishes one — that is not a bug |
| `pending_version` | The version AWS accepted but has not applied yet |
| `settled` | No change is in flight. **Not** a claim that the upgrade landed |
| `scheduled` | Whether scheduled drift scanning is enabled. An empty `findings` with `scheduled: false` means nothing is watching between page loads |
| `warnings[].iam_action` | The exact IAM action to grant when a describe was refused |

An `elasticache` finding also carries `members` (count) and `engine_versions`
(every distinct member version).

---

## POST /api/aws/maintenance/apply

Request one engine-version upgrade.

**Body — every field required**

| Name | Type | Notes |
|---|---|---|
| `kind` | string | `rds-instance`, `rds-cluster`, or `elasticache` |
| `resource` | string | The `resource_id` from the report |
| `target_version` | string | Must equal the report's `available_major` for that resource |
| `confirm_resource` | string | Must equal `resource` **exactly** |
| `apply_immediately` | boolean | `true` = apply now (outage starts immediately); `false` = at the resource's next maintenance window |

`apply_immediately` has **no default**. Omitting it, or sending the string
`"false"`, is a 400 — the two values are different outage decisions and a
missing field is a question that was never asked.

`target_version` is re-derived server-side from the installation's own report
before anything reaches AWS. A version the server is not currently offering is
refused, so a stale page cannot move a database somewhere unexpected.

**Response** (`data`)

```json
{
  "schema_version": 1,
  "requested": true,
  "kind": "rds-instance",
  "resource": "mojo-prod-postgres",
  "engine": "postgres",
  "from_version": "14.11",
  "target_version": "16.4",
  "apply_immediately": false,
  "release_notes_url": "https://docs.aws.amazon.com/...",
  "operation": "rds.modify_db_instance"
}
```

`requested: true` means AWS accepted the request. It does **not** mean the
upgrade finished — poll the status endpoint.

**Errors**

| HTTP | `error_code` | Meaning |
|---|---|---|
| 400 | `invalid_request` | Unknown `kind`, missing `resource`, mismatched `confirm_resource`, or an unstated `apply_immediately` |
| 403 | — | The caller holds `manage_aws` but no platform management tier |
| 403 | `provider_denied` | IAM refused. `data.failure.iam_action` names the action to grant |
| 409 | `upgrade_not_offered` | That version is not what the server is offering for that resource |
| 409 | `upgrade_in_progress` | Another apply for the same resource is already running |
| 440 | `reauth_required` | Re-authenticate and retry |
| 502 | `provider_error` | AWS returned an error |
| 503 | `cache_unavailable` | Concurrency could not be confirmed. **Retry**; this is never a silent success |
| 503 | `provider_unavailable` | Retryable AWS failure |

Provider failures carry `data.failure` — `operation`, `provider_code`,
`retryable`, `mutation_state`, and `iam_action` on a denial. Raw AWS error text
is never returned.

`mutation_state` is worth reading on a failure: `attempted` means the request
reached AWS, `unknown` means it may have. Neither is proof nothing happened.

---

## GET /api/aws/maintenance/status

Live progress for one resource. Poll this after an apply.

**Query parameters**

| Name | Required | Notes |
|---|---|---|
| `kind` | yes | Same value used on apply |
| `resource` | yes | Same value used on apply |
| `target_version` | no | The version being moved to; required for `upgraded` to be meaningful |

**Response** (`data`)

```json
{
  "schema_version": 1,
  "kind": "rds-instance",
  "resource": "mojo-prod-postgres",
  "target_version": "16.4",
  "found": true,
  "status": "upgrading",
  "engine_version": "14.11",
  "engine_versions": ["14.11"],
  "pending_version": "16.4",
  "settled": false,
  "upgraded": false
}
```

**Read `upgraded`, not `status`.** They answer different questions:

- `settled` — AWS finished. For a cache group, every member is `available`.
- `upgraded` — every member's engine version equals `target_version`.

A resource that is `settled: true, upgraded: false` came back on its **old**
version: the upgrade did not take effect. Report that, do not report success.
The Admin portal polls every 10 seconds for up to 30 minutes and says exactly
this.

---

## GET /api/account/admin/platform/framework

Which django-mojo this installation runs, and whether the caller could update
it.

**Query parameters:** `refresh` (`1`/`true`/`yes`) re-checks PyPI.

**Response**

```json
{
  "schema_version": 1,
  "installed": "1.12.3",
  "latest": "1.13.0",
  "checked_at": "2026-08-18T17:55:00+00:00",
  "source": "pypi",
  "update_available": true,
  "pin": {"mode": "latest", "value": null},
  "can_update": true,
  "blocked_reason": null
}
```

| Field | Meaning |
|---|---|
| `pin.mode` | `latest` (unpinned), `pinned` (a specific version), or `hold` (stay on the last converged version) |
| `source` | `pypi`, `cache`, or `unavailable` when PyPI could not be reached |
| `blocked_reason` | `null`, `update_unavailable`, `requires_superuser`, or `no_converged_deployment` |

Render `blocked_reason` instead of offering a control that would fail:

- `update_unavailable` — nothing newer is published, or PyPI is not answering.
- `requires_superuser` — a pin or hold is set; clearing it is superuser-only.
- `no_converged_deployment` — nothing has ever converged on this fleet, so
  there is no proven commit to redeploy.

---

## POST /api/account/admin/platform/framework/update

Move the fleet to the newest published django-mojo.

**Body**

| Name | Type | Notes |
|---|---|---|
| `version` | string | Must equal the current `latest` |
| `confirm_version` | string | Must equal `version` exactly |

**Header:** `Idempotency-Key` is honored and passed to deploy coordination.

If a pin or hold is set, it is **cleared** — the fleet returns to the automatic
latest-version policy. The version is never written into the pin. Then the last
converged commit is redeployed, which is what actually installs the new release.

**Response**

```json
{
  "schema_version": 1,
  "requested": true,
  "version": "1.13.0",
  "cleared_pin": "1.12.0",
  "deployment": { "id": "...", "sha": "...", "status": "requested" }
}
```

Follow the deployment on the portal's **Deployments** route rather than polling
here — it carries per-node proof.

**Errors**

| HTTP | Meaning |
|---|---|
| 400 | `confirm_version` does not match `version` |
| 403 | Only a literal superuser may clear a pin |
| 409 | `version` is not what the installation is offering, or `can_update` is false |
| 440 | `reauth_required` |
| 503 | Deploy coordination unavailable |
