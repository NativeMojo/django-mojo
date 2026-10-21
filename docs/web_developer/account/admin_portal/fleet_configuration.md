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
changes; do not silently retry against the new revision. Stale publication revisions return HTTP 409. Other publication errors use the
normal bounded validation-error response.
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
409. Poll the operation route for fresh, revision-bound evidence after signed
dispatch. The dispatch job returns before its own engine drains. A completed
`job_status` does not mean activation finished: keep polling while operation
`status` is pending. Observation reports a timeout after 120 seconds from
dispatch; timers and graceful job draining continue independently.

Current state `fleet` and operation evidence expose `nodes`. Each node has
`hostname`, `revision`, `published`, `installed`, `restart_requested`,
`restarted`, `healthy`, `status`, and `error_code`. Node statuses include
`unknown`, `pending`, `draining`, `downloaded`, `restart_requested`, `restarted`, `healthy`,
and `failed`. Display failure codes rather than interpreting an absent reply
as success. Offline expected nodes remain in the denominator.

Only `healthy_everywhere: true` means every expected node passed the checks.
Health requires matching installed bytes, fresh loaded-revision proof from
exactly one supervised engine and scheduler, and reachable database/Redis.
Request-serving nodes additionally require a service restart after installation
and a fresh serving-process response for that revision. Worker-only nodes can
pass without an API service when their sealed role disables it. Missing,
duplicate, stale or draining jobs components never count as healthy. The
`health_scope` is `request_service_jobs_and_dependencies`.

Operation results can be `queued`, `pending`, `healthy`, `superseded`,
`timed_out`, `failed`, `canceled`, `expired`, or `unknown`; operation reads also
include `job_status`. A new publication supersedes observation of the old one.
Cancellation before dispatch can prevent queued work. After dispatch the
completed job cannot be canceled, and stopping browser polling does not undo
activation. Operation reads after dispatch recheck current convergence; refresh
fleet state after a timeout for later evidence. Long-running jobs finish before
their engine exits; this workflow never force-kills them. A healthy observation
is not an ongoing health guarantee.

The serving-proof route is a signed machine probe, not a browser polling API.
See [registration, storage and node prerequisites](../../../django_developer/account/admin_portal/fleet_configuration.md)
for application integration, fixed-operation permissions and deployment setup.

Fresh observations include `observed_at`. Queued responses may omit it;
persisted signed dispatch reports include `dispatched_at` separately.
Job completion is dispatch completion only, never a substitute for node health.

If no coordinator claims Apply within five minutes, its operation read reports
`expired` with `apply_runner_unavailable`; the browser does not wait indefinitely.

A correlated checked runner rejecting new work during drain reports
`engine_draining`. A completed dispatch job without valid signed evidence
reports a terminal `failed` result instead of leaving the browser polling
forever. Legacy request-service-only health cannot certify jobs activation.

## Shared-validation topic

These private routes use the same interactive literal-superuser, same-origin and
600-second fresh-auth rules, additionally denying user API keys on writes and
reads. All responses, including denials, are `Cache-Control: no-store`.
`GET /api/account/admin/fleet/shared-validation` accepts no fields and returns:

```json
{"schema_version":1,"available":true,"unavailable_reasons":[],"catalog_revision":"<64 hex>","profiles":[{"id":"approved_hub","label":"Hub","origin":"https://hub.example","consumer_project_uuid":"<32 hex>","consumer_label":"Original recipient"}],"selected_profile_id":"approved_hub","expected_identity":{"consumer_project_uuid":"<32 hex>","consumer_label":"Original recipient"},"loaded_identity":{"state":"verified","profile_id":"approved_hub","observed_project_uuid":"<32 hex>","checked_at":"<timestamp>","error_code":null,"sharing_ready":false},"credential":{"configured":true,"source":"published","overridden":true},"revision":"<32 hex>","loaded_revision":"<32 hex>","pending_restart":false,"fleet":{"status":"pending","nodes":[],"healthy_everywhere":false},"setup_destination":{"route":"setup","section":"aws","capacity_route":"capacity","actor":"deployment_operator","adoption_item":8903}}
```

`available` means the configuration workflow's installed prerequisites are present;
`sharing_ready` means this fresh authenticated observation reports shared activation.
An authenticated v2 UUID with sharing disabled is a valid configurable credential.
Display both states: operators must be able to configure credentials before hub
activation. Credential `source` is `published`, `static`, `none`, or `unknown`.
It never includes a token, length or hint. Expected identity comes from installed
trusted code; loaded identity comes from a timestamped authenticated capabilities
check of this process's static configuration. Neither proves the entire fleet.

The browser chooses an installed opaque profile ID; it cannot choose an origin,
recipient UUID as authority, path, socket, module, command, bucket or grant rule.
A missing catalog, schema, either delegation or publisher prerequisite produces
bounded `unavailable_reasons`. Show the supplied Setup/AWS/Capacity operator
handoff. Application adoption installs approved profiles and permissions for the
existing estate and future launches; a successful local source test does not
certify that delivery or live transport exists.

Preview a candidate without saving a credential or verification state:

```json
{"action":"preview","expected_revision":"<32 hex>","expected_catalog_revision":"<64 hex>","profile_id":"approved_hub","key_action":"replace","replacement_key":"<secret>"}
```

The response includes `can_publish`, `revision`, `catalog_revision`, `profile_id`,
`expected_identity`, safe `identity`, `credential_changed`, and `consequences`.
Use `action:"publish"` with the same fields to reconstruct and reverify the
candidate against the current head/catalog. `expected_revision:null` selects the
first publication. Unknown fields are rejected. `key_action:"keep"` sends no
replacement, and blank replacement preserves the current effective credential.
Keep may adopt a loaded legacy static credential only when its normalized origin
and authenticated UUID match the chosen approved profile. The publish success DTO
is the existing `{published:true,revision,version_id,pending_restart:true,applied:false}`.
Then call the existing Fleet Apply/operation APIs and observe every expected node.

`key_action:"clear"` explicitly disables the connection by writing all three
empty overrides together, masking any base key. It does not revoke the remote key
or allocation trust. Existing shared connections, including retired identity rows,
make disable ineligible. A connected origin/recipient change requires a separate
identity migration. Generic Fleet edits and historical restore enforce the same
bundle policy; per-field removal or restoring a pre-bundle version cannot expose
unverified fallback values. An unchanged bundle does not probe the hub during an
unrelated GeoIP publication.

Stale revision/catalog is 409; invalid fields or profile/identity are 400; denied
authority is 403; unavailable dependencies are 503. Stable codes include
`profile_catalog_unavailable`, `profile_catalog_changed`, `profile_not_approved`,
`hub_identity_mismatch`, `hub_identity_migration_required`,
`connection_disable_ineligible`, `fleet_schema_unavailable`,
`fleet_delegation_unavailable`, `publisher_unavailable`,
`node_enrollment_unavailable`, `fleet_revision_changed`, `configuration_busy`, `publication_uncertain`
and bounded `hub_*` codes. A failed or uncertain publish requires authoritative
state reload before another preview. Never blindly replay a secret-bearing write.

`configuration_busy` is HTTP 409 for a competing setup/configuration identity
owner, including preview, publish and historical restore. It refuses before a
conditional S3 write and does not mean the key or identity changed. Show the busy
state, Refresh authoritative configuration, preview again, and let the operator
deliberately retry. Never automatically replay the mutation. An unchanged complete
managed tuple still permits unrelated Fleet/GeoIP publication without identity
admission or a hub probe while that other owner is active.

Each expected node retains safe `shared_validation` evidence: `state`, `profile_id`,
`observed_project_uuid`, `checked_at`, `error_code`, and `sharing_ready`. Offline
nodes remain present with no proof. Configuration health requires its actual loaded
credential to authenticate the approved UUID, plus existing installed-byte,
revision/restart-age, engine and scheduler proofs. A serving node also requires
matching request-process identity. The root-sealed fixed profiles support
`/opt/api/var/asgi.sock` and `/run/mojo/asgi.sock`; this is enrollment metadata,
never a browser-selectable path. One successful probe or completed dispatch does
not certify other nodes, hub activation, or future launch coverage.

Generic Fleet state also reports `shared_validation_required` and safe
`shared_validation_identity` (`profile_id`, `consumer_project_uuid`) for the exact
published target. Every managed node must match both fields. Unmanaged,
undelegated and explicitly disabled topics preserve legacy Fleet/GeoIP health
without contacting a legacy static hub; their topic-specific readiness remains a
separate status.


## Enable hub sharing

The existing authenticated Admin family provides one small hub enable surface:

| Method/path | Exact input | Result |
|---|---|---|
| `GET /api/account/admin/fleet/shared-validation/hub` | None | Read-only status |
| `POST /api/account/admin/fleet/shared-validation/hub` | `{"deployment_uuid":"<canonical hyphenated UUID>"}` | Deliberate enablement, then fresh status |

Both routes reuse the existing literal-superuser actor and exclude every
key-backed session. POST also uses the existing same-origin and fresh-auth
checks. Responses use the existing private no-store envelope. Never replay
POST automatically after fresh authentication; refresh and let the human act.

Status has exactly:

```json
{"version":1,"enabled":false,"configured":true,"deployment_uuid":"11111111-1111-4111-8111-111111111111","can_enable":true,"reason":null}
```

The candidate UUID is null when no deployment exists. `enabled:true` has
`can_enable:false,reason:null`. Otherwise `can_enable` is true only when
`reason` is null. Safe refusal reasons are `hub_unconfigured`, `hub_unavailable`,
`deployment_required`, `deployment_in_progress`, `deployment_not_converged`,
`deployment_proof_unavailable`, and `framework_upgrade_required`. A changed
selected UUID returns 409 `deployment_changed`; malformed input returns 400.
Use existing deployment/configuration links and Refresh for these conditions.

A qualifying release must be newer than 1.39.1 and equal to the serving process
framework. Every frozen-roster node needs fresh ordinary proof of that exact
UUID, SHA and framework. An active, stale, incomplete or mismatched deployment
cannot enable sharing. The enable transaction binds original owners and
retains installation identity; status reads never activate it. Consumers still
require their separate explicit trust grants. See the
[normal rollout guide](../../../django_developer/dnsman/HubUpgrade.md).
