# Fleet Configuration

Fleet Configuration publishes typed application settings through the existing
S3/config-sync path. It never accepts Python source or a command to execute.
Publishing changes the remote document; it does **not** mean the fleet has
loaded it. The Admin page shows publication and per-node activation separately.

## Register application settings

Put the schema in an import-light module that works before Django starts:

```python
# myapp/fleet_settings.py
from mojo.deploy.config_override import register_setting

register_setting(
    "MYAPP_UPLOAD_LIMIT", label="Upload limit", section="Uploads",
    description="Maximum files in one upload request.",
    value_type="integer", default=20, min_value=1, max_value=100,
    restart_required=True,
)
register_setting(
    "MYAPP_PROVIDER_TOKEN", label="Provider token", section="Integrations",
    description="Credential used by the application provider.",
    value_type="string", sensitive=True, max_length=512,
)
```

Supported types are `boolean`, `integer`, `string`, `list`, and `object`.
Booleans are not integers. Nested JSON is bounded; nonfinite numbers and
unsupported values are rejected. Optional `validator(value)` must return exactly
`True`; its exception text is never returned. Defaults must pass the same
validation. Secret defaults and values are omitted from schema responses.
Conflicting registrations fail; identical registration is safe to repeat.

Load the same module in both trusted configuration planes:

```python
# Application deployment settings
ADMIN_FLEET_CONFIG_SCHEMA_MODULES = ["myapp.fleet_settings"]
ADMIN_FLEET_CONFIG_ALLOWED_KEYS = ["MYAPP_UPLOAD_LIMIT", "MYAPP_PROVIDER_TOKEN"]
ADMIN_FLEET_CONFIG_EXPECTED_NODES = ["api-1", "api-2"]
ADMIN_FLEET_CONFIG_CHANNEL = "edge"
```

```ini
# Node config-sync bootstrap, outside the published document
CONFIG_SYNC_SCHEMA_MODULES=myapp.fleet_settings
CONFIG_SYNC_OVERRIDE_ALLOWED_KEYS=MYAPP_UPLOAD_LIMIT,MYAPP_PROVIDER_TOKEN
```

Registration and delegation are independent. Nodes refuse unregistered or
undelegated keys. Modules must be installed on every target before publication.
The framework's five existing GeoIP definitions remain available. Bootstrap,
code-loading, credentials used to authorize this workflow, and its own control
settings cannot be registered. Keys owned by existing dedicated writers, including
catalog overrides, `BASE_URL`, and `GEOIP_API_KEY_MOJO`, also remain reserved.
Generic `/api/settings` cannot write global rows for registered fleet keys;
existing group-scoped behavior is unchanged. The registry loads at Account
startup to enforce this boundary.

Application code must consume these deployment-owned values through Django
settings or `mojo.helpers.settings.settings.get_static(...)`. Registration does
not change the precedence of dynamic `settings.get(...)` or group overrides;
a pre-existing database row can still shadow a dynamic read. Migrate those
callers/rows deliberately when moving an existing key into fleet ownership.

`restart_required` describes the application's consumption contract. The shared
config-sync activation still follows its configured service restart
policy; declaring a field restart-free does not introduce hot reloading.

## REST contract

The paths below use the default API root. Reads require an interactive literal
superuser; writes also use the existing 600-second fresh-auth policy and
same-origin check. API keys and key-backed diagnostic sessions cannot publish,
restore, or apply. The framework's deployment-level fresh-auth enforcement
setting retains its established behavior.

- `GET /api/account/admin/fleet`: schema, published revision, redacted values,
  and current fleet evidence. `entries[].current` is the published non-secret
  override or this process's static fallback; it is not proof that all nodes
  serve that value. Secrets return only `configured` and `overridden`.
- `GET /api/account/admin/fleet/history`: up to 50 version metadata records for
  the exact configured object; `truncated` identifies a partial history.
- `POST /api/account/admin/fleet`: one of the actions below.
- `GET /api/account/admin/fleet/operation/<operation_id>`: validates signed
  dispatch evidence, then freshly observes that revision. The dispatch job may
  already be completed while activation is pending. `observed_at` identifies
  the observation time; queued responses may omit it.

Publish only changed fields:

```json
{"action":"publish","expected_revision":"0123456789abcdef0123456789abcdef","changes":{"MYAPP_UPLOAD_LIMIT":{"action":"set","value":30},"MYAPP_PROVIDER_TOKEN":{"action":"set","value":"replacement-secret"}}}
```

Use `expected_revision: null` for the first publication. Omitted fields are
preserved. An empty secret replacement preserves its previous value. Explicit
`{"action":"clear"}` removes that override and restores the node's base/default
behavior; it does not erase a credential present in the base configuration.
The browser never reads back a secret. Submitted values are excluded from
request logs, publication responses, and audit events. State responses expose
non-secret current values.

```json
{"action":"restore","expected_revision":"0123456789abcdef0123456789abcdef","version_id":"previous-s3-version-id"}
```

Restore reads and verifies a historical document, validates it against the
current schema and delegation, then publishes it as a **new** version under a
conditional write against the current head. It does not delete versions or
silently overwrite a concurrent edit. Restoring a version also restores its
secret values. Old schema values that are no longer valid must be corrected
through a new structured publication instead.

```json
{"action":"apply","expected_revision":"0123456789abcdef0123456789abcdef"}
```

Apply returns `operation_id` immediately. A job invokes only the fixed node
config-sync operation and returns after saving signed dispatch evidence. This
lets its own engine finish draining. Operation reads observe convergence for
up to 120 seconds from dispatch before reporting a timeout. Repeated clicks
join an active operation. A changed publication supersedes the old
operation. Cancellation before dispatch can prevent the queued work; once
dispatch completes, activation cannot be canceled through that completed job.
The browser may stop polling, but service activation continues. Timers continue independently after timeout.
Apply intents and results are signed and bound to the operation, actor,
revision, and expected node list; ordinary job editing cannot invent authority
or a successful result.

Expected membership comes from `ADMIN_FLEET_CONFIG_EXPECTED_NODES`, or the
protected `EDGE_EXPECTED_TOPOLOGY.nodes` fallback. Configure actual hostnames
for all configuration-consuming nodes, including workers and those currently
offline. Membership is bounded at 128; a live-only runner list is never used
as the completion denominator. Worker-only nodes omit API checks only when
config-sync publishes independent root-controlled evidence of an explicitly
disabled request-service role and the installation time. The apply coordinator runs on the existing `default` jobs channel;
target runners use `ADMIN_FLEET_CONFIG_CHANNEL` (default `edge`).

Each node reports publication, downloaded/installed, restart requested,
restarted, healthy, or a fixed failure code. Healthy requires the desired file
digest, exactly one supervised engine and scheduler with fresh startup-loaded
revision proof, and database/Redis reachability. Each proof matches the live
PID and kernel start identity; duplicate, old, missing or draining processes
cannot pass. Request-serving nodes additionally need a service start after
installation and the serving process's revision/health response to a signed challenge.
A successful systemctl enqueue or completed dispatch job proves none of that. The serving probe
uses a short-lived signed challenge, and exposes no configuration values.

## Storage and deployment prerequisites

Use the existing `ADMIN_FLEET_CONFIG_BUCKET`, `ADMIN_FLEET_CONFIG_PREFIX`,
optional `ADMIN_FLEET_CONFIG_FILENAME` (default `django.override.json`), and
`ADMIN_FLEET_CONFIG_KMS_KEY_ID`. Existing AWS config location/KMS fallbacks are
unchanged. S3 bucket versioning must be **Enabled**, including for legacy
GeoIP publication. Existing deployments need this prerequisite before saving.

The publisher role needs `s3:GetObject`, `s3:GetObjectVersion`, and
`s3:PutObject` on the **exact** configuration object; `s3:GetBucketVersioning`
on its bucket; and `s3:ListBucketVersions` constrained to that exact object
prefix. Listing filters exclude similarly prefixed sibling objects. Grant
KMS encrypt/decrypt/data-key access only to the configured key, with the
existing configuration-location encryption-context restriction. Nodes retain
read-only configuration access; no write grant is needed for Apply now.
Do not broaden an existing role to all objects or keys to enable this page.

The S3 document and generated `var/django.conf` can contain application
secrets. Keep the existing restricted S3/KMS policies and atomic 0640 file
installation. Documents are bounded at 32 KiB and 64 values, carry SHA-256
metadata, and use conditional writes. Audit history records actor, action,
revision, and changed key names, never values.

See [node deployment tooling](../../deploy/README.md#admin-fleet-overrides)
for schema loading, fixed service permissions, socket access, and receipts.

If no coordinator claims Apply within five minutes, its operation read reports
`expired` with `apply_runner_unavailable`; the browser does not wait indefinitely.

Automatic activation sends SIGTERM to the managed engine and scheduler after
checking their installed cron supervisor and launch permissions. The engine
stops claiming new jobs, retains heartbeat/visibility protection while active
jobs and startup hooks finish, then exits. The scheduler finishes its current
popped batch before releasing leadership. The existing every-minute cron
starts replacements; configuration activation never escalates to SIGKILL.
A long job or unavailable replacement keeps activation unconfirmed, including
after the observation timeout. Refresh fleet state for a later observation.
`CONFIG_SYNC_RESTART=false` installs files without retiring processes.
