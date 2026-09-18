# Fleet Configuration API

Use structured changes to publish application-registered settings and observe
activation across the configured fleet. Publication and fleet health are
separate results. No endpoint accepts Python source, shell commands, or an S3
object location supplied by the browser.

All routes require an active literal superuser using an interactive bearer
session. An `admin` permission grant alone is insufficient. Writes also require
a same-origin `Origin` header and the existing 600-second fresh-auth policy;
handle [step-up authentication](../step_up_auth.md) before retrying. API keys and
key-backed diagnostic sessions are denied. Deployment-level fresh-auth
configuration retains its existing behavior.

Routes below use the default API root. Response examples show the data payload
inside the framework's standard response envelope.

| Method | Route | Purpose |
|---|---|---|
| GET | `/api/account/admin/fleet` | Schema, desired values and current fleet evidence |
| GET | `/api/account/admin/fleet/history` | Bounded version metadata |
| POST | `/api/account/admin/fleet` | Publish, restore or apply |
| GET | `/api/account/admin/fleet/operation/<operation_id>` | Apply operation evidence |

## Read and render

Fleet state includes `schema_version: 1`, `revision`, `version_id`, `published`,
`loaded_revision`, `pending_restart`, `publish_configured`, `entries`, and `fleet`.
Before first publication, revision and version are null. `publish_configured`
reports static configuration availability; it does not certify S3 permissions
or versioning. Writes verify their prerequisites again.

Each entry supplies `key`, `label`, `section`, `description`, `value_type`,
`sensitive`, `restart_required`, `overridden`, and the applicable type bounds
(`min_value`, `max_value`, `max_length`, `max_items`). Supported types are
`boolean`, `integer`, `string`, `list`, and `object`. Non-secret entries include
`default` and `current`. Sensitive entries instead expose `configured`; they
never include a current secret or secret default. Keep untouched password
inputs out of the submitted changes.

`current` means the published override, falling back to the serving process's
static value/default. `loaded_revision` describes that process only. Neither
field proves that all nodes have applied the change. Group entries by their
server-owned `section` and display `restart_required` alongside the proposal.
A restart-free declaration does not add a hot-reload facility.

## Publish and restore

Submit only changed fields:

```json
{"action":"publish","expected_revision":"0123456789abcdef0123456789abcdef","changes":{"MYAPP_UPLOAD_LIMIT":{"action":"set","value":30},"MYAPP_PROVIDER_TOKEN":{"action":"set","value":"replacement-secret"}}}
```

Use `expected_revision: null` for the first publication. Omitted keys are
preserved; an empty string replacement preserves a sensitive value. Explicit
`{"action":"clear"}` removes the override, falling back to base/default
configuration. Clearing does not erase credentials stored in that base.
Unknown fields, undelegated keys, invalid types and unsupported actions fail.

History returns `versions: [{version_id, current, published_at}]` and
`truncated`. It lists at most 50 version records and never includes settings
values. `truncated: true` means the list is partial; this API has no page cursor.
Select a version ID from this history to restore it:

```json
{"action":"restore","expected_revision":"0123456789abcdef0123456789abcdef","version_id":"previous-s3-version-id"}
```

Restore verifies the historical document and current validation rules, then
publishes its settings, including secrets, as a new version. Both actions use a
conditional write against the current document. A successful response is:

```json
{"published":true,"revision":"fedcba9876543210fedcba9876543210","version_id":"new-s3-version-id","pending_restart":true,"applied":false}
```

Reload state after a rejected concurrent edit and let the operator reconcile
changes; do not silently retry against the new revision. Publication errors,
including stale revisions, currently use the normal validation-error response.
An uncertain publication failure also requires reloading state before retrying.
S3 versioning must be Enabled and the configured KMS key available. This
versioning prerequisite also applies to the existing GeoIP publisher.

## Apply and observe

```json
{"action":"apply","expected_revision":"fedcba9876543210fedcba9876543210"}
```

The response includes `operation_id`, `revision`, `status: "queued"`,
`healthy_everywhere: false`, and `nodes`. A repeated request joins an existing
active operation for the same revision and membership; its response may already
contain progress. A different active operation or stale apply revision returns
409. Poll the operation route for signed, revision-bound historical evidence.
The worker observes for up to 120 seconds; timers continue after a timeout.

Current state `fleet` and operation evidence expose `nodes`. Each node has
`hostname`, `revision`, `published`, `installed`, `restart_requested`,
`restarted`, `healthy`, `status`, and `error_code`. Node statuses include
`unknown`, `pending`, `downloaded`, `restart_requested`, `restarted`, `healthy`,
and `failed`. Display failure codes rather than interpreting an absent reply
as success. Offline expected nodes remain in the denominator.

Only `healthy_everywhere: true` means every expected node passed the checks.
Health requires matching installed bytes, a service restart after installation,
and a fresh serving-process response confirming the desired revision and
reachable database/Redis. A service-start request alone proves none of these.
Worker-only nodes report `request_service_unsupported`.

Operation results can be `queued`, `pending`, `healthy`, `superseded`,
`timed_out`, `failed`, `canceled`, `expired`, or `unknown`; operation reads also
include `job_status`. A new publication supersedes observation of the old one.
Cancellation stops observation but cannot undo an already requested service
start. Refresh fleet state for current evidence after an operation completes;
a historical successful result is not an ongoing health guarantee.

The serving-proof route is a signed machine probe, not a browser polling API.
See [registration, storage and node prerequisites](../../../django_developer/account/admin_portal/fleet_configuration.md)
for application integration, fixed-operation permissions and deployment setup.

Saved Apply reports include `observed_at`, the evidence observation timestamp
rather than the time of the browser poll. Queued responses may omit it.

If no coordinator claims Apply within five minutes, its operation read reports
`expired` with `apply_runner_unavailable`; the browser does not wait indefinitely.
