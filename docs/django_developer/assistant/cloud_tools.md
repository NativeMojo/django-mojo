# Cloud Domain Tools

Twenty on-demand tools (`domain="cloud"`, none core) that let an operator ask
the Assistant what is healthy, what needs attention, and — after an explicit
approval — perform the same bounded cloud and fleet tasks the built-in Admin
already offers.

The rule the whole domain is built on: **a tool is never easier to reach
through chat than through the portal.** Every tool mirrors exactly one Admin
endpoint, calls the same shared service that endpoint calls, and declares the
same gates. Assistant access alone grants nothing.

Module: `mojo/apps/assistant/services/tools/cloud/`
(`common.py` · `reads.py` · `actions.py`).

---

## Read tools

All are `mutates=False`, declare no approval gate, and return a **bounded**
projection (see [Bounding and redaction](#bounding-and-redaction)).

| Tool | Mirrors | Service call | Permission |
|---|---|---|---|
| `get_platform_health` | `GET /api/account/admin/dashboard` | `admin_platform.dashboard_overview` | `view_admin`, `manage_users`, `manage_settings`, `admin` |
| `get_platform_overview` | `GET /api/account/admin/platform?sections=…` | `admin_platform.platform_overview` | `view_platform`, `view_platform_security`, `manage_platform`, `admin` |
| `get_advanced_inventory` | `GET /api/account/admin/advanced` | `admin_platform.advanced_overview` | `view_advanced`, `view_advanced_inventory`, `view_advanced_security`, `manage_advanced`, `admin` |
| `get_framework_status` | `GET /api/account/admin/platform/framework` | `admin_platform.framework_overview` | `view_platform`, `manage_platform`, `admin` |
| `get_fleet_capacity` | `GET /api/aws/capacity` | `capacity.report` | `manage_aws` |
| `get_capacity_operation_status` | `GET /api/aws/capacity/status` | `capacity.operation_status` / `capacity.batch_status` | `manage_aws` |
| `get_managed_upgrades` | `GET /api/aws/maintenance/versions` | `maintenance.report` | `manage_aws` |
| `get_upgrade_status` | `GET /api/aws/maintenance/status` | `maintenance.resource_status` | `manage_aws` |
| `get_setup_readiness` | `GET /api/account/admin/setup/readiness` | `system_readiness.run` | `admin` **+ active superuser + interactive session** |
| `get_setup_operation` | `GET /api/account/admin/setup/options` + `/detail` | `system_setup.serialize` | `admin` **+ active superuser + interactive session** |
| `get_version_drift` | the Dashboard's recorded drift row | newest in-window `incident.Event` | `view_platform`, `manage_platform`, `admin` |
| `list_cloud_resources` | `GET /api/aws/cloudwatch/resources` | `CloudWatchHelper.list_*` | `manage_aws` |
| `fetch_cloud_metrics` | `GET /api/aws/cloudwatch/fetch` | `CloudWatchHelper.fetch` | `manage_aws` |

A permission **list** is an OR, matching `user.has_permission`.

### Two inputs are required on purpose

`sections` on `get_platform_overview` and `section` on `get_setup_readiness`.
A bare `platform_overview` collects all ten sections including per-app HTTPS
probes; a bare readiness run fans out across AWS, DNS and certificates. A chat
turn should ask for what it needs. `sections` is capped at four per call.

The shim passes `sections` **comma-joined**, because the service filter honours
only a string (`isinstance(wanted, str)`) — a JSON array would silently read as
"no filter" and trigger the full fan-out.

### The actor shim

`common.actor_request(user, **data)` is an `objict` carrying `user`, `DATA`,
`QUERY_PARAMS`, `META`, `ip="assistant"`, `path`, `method`, `bearer=None`,
`group=None`. It
exists because the four `admin_platform` overviews are the only services in
scope that still take a request, and they read exactly `request.user` (through
`_permitted`) plus `request.DATA.get("sections")`.

It is confined to **reads**. The one mutating service that read a request —
`apply_framework_update` — was refactored to
`apply_framework_update(actor, version, idempotency_key=None)` instead: no
mutation should run behind a fabricated request object.

### System Setup is read-only here, deliberately

Every `system_setup` mutation (`create` / `choose` / `advance` / `cancel`) is
bound to the browser Origin that started it (`system_setup.request_origin`,
`_assert_bound_origin`) and refuses any session that is not an interactive
same-origin superuser tab. Reaching setup **repair** from chat would mean
forging that binding, so it is out of scope. Readiness audit and honest
progress on an operation a human started in the Admin are in scope.

The twin endpoints also carry `@md.denies_key_backed_session()` and
`require_request_admin`, which demands `request.bearer == "bearer"`. The chat
path expresses that through `request_meta`, so both handlers refuse when the
meta is missing, non-bearer, or key-backed — see
[request_meta](#request_meta-on-both-transports).

---

## Mutating tools

Every row is `mutates=True` and `fresh_auth_seconds=600` (each mirrored
endpoint carries `@md.requires_fresh_auth(seconds=600)`), and declares both
`summarize` and `preview`.

| Tool | Mirrors | superuser | managed infra | Bound `revision` |
|---|---|---|---|---|
| `retry_platform_deployment` | `POST .../platform/deploy/retry` | no | **no** | `<deployment>:<status>` |
| `verify_platform_deployment` | `POST .../platform/deploy/verify` | no | **no** | `<deployment>:<status>` |
| `converge_platform_deployment` | `POST .../platform/deploy/converge` | no | **no** | `<deployment>:<status>` |
| `apply_framework_update` | `POST .../platform/framework/update` | no | yes | `<latest>:<converged deployment>` |
| `apply_managed_upgrade` | `POST /api/aws/maintenance/apply` | no | yes | `<kind>:<resource>:<from>:<to>` |
| `apply_capacity_change` | `POST /api/aws/capacity/apply` | **yes** | yes | `<action>:<resource>:<fleet fingerprint>` |
| `apply_capacity_plan` | `POST /api/aws/capacity/plan` + `/plan/apply` | **yes** | yes | `<fleet fingerprint>` |

`requires_managed_infrastructure` is true **exactly where the mirrored handler
calls `infrastructure.refuse()`** — copying the Admin, not tightening it. The
three deploy endpoints do not call it, so those three tools keep working on an
installation whose estate is applied by external IaC, precisely as the Admin
controls do.

### Compound authority the `permission` argument cannot express

Two of the Admin's gates are hand-written ANDs, and `requires_global_perms`
composes with OR, so they are expressed with `authorize=`:

- `apply_managed_upgrade` → `common.maintenance_tier` (superuser OR
  `manage_platform` OR `admin`), mirroring
  `mojo/apps/aws/rest/maintenance.py::_require_manage_tier`. A `manage_aws`-only
  holder is never offered the tool.
- `apply_capacity_change` / `apply_capacity_plan` → `common.can_system_admin`,
  the listing-time twin of `requires_superuser`. **A `manage_aws` holder who is
  not a literal superuser never sees these two tools in any listing** —
  `get_tools_for_user`, `get_core_tools_for_user`, `get_domain_tools_for_user`
  or `get_available_domains` — and is refused at dispatch and proposal.
- `get_setup_readiness` / `get_setup_operation` → `common.can_system_admin`
  too, because `requires_superuser` is legal only alongside `mutates=True` and
  these are reads.

`can_system_admin` is a plain attribute read, deliberately without a
per-request query — it runs for every registry entry on every listing build.
The authoritative re-read stays where the authority is: `requires_superuser` at
resolution, and `common.is_system_admin` (which wraps
`system_settings.require_system_admin`) inside the two read handlers.

### Typed echoes do not translate to chat

`confirm_resource` / `confirm_version` prove that an operator can reproduce an
identifier they are looking at. In chat the model would type them, proving
nothing — so no tool exposes a `confirm_*` field, and the approval card is the
confirmation. **The server-side half of every echo is kept** in `preview`:

- the version must equal what this installation is offering;
- the upgrade target must be the one `offered_target` returned;
- the capacity action must be one `report()["actions"]` currently offers;
- the deployment's status must be one the Admin offers that control for,
  read from `platform_deploy.ACTIONS_BY_STATUS` / `ACTIVE_STATUSES`.

That last table was hoisted out of the browser
(`admin_portal/assets/features/webapps/api.js`) in this item, because the
endpoints enforce no status at all. The tools are therefore **stricter than the
endpoints** — retry only on `failed`, verify on
`failed`/`verified`/`partial`/`unknown`/`converged`, converge on
`verified`/`partial`/`unknown`, and none of them while the attempt is
`requested`/`canary`/`fleet`, because the orchestrator is driving it.

### The bound revision for capacity is the fleet fingerprint

Not the plan id. `PLAN_TTL` is 300s and the approval window defaults to 600s,
so a bound plan id would routinely expire before the operator answers. The
fingerprint (`capacity.fleet_revision()`, added in this item over the existing
`_fleet_fingerprint`) is what the plan's safety actually depends on, and
`apply_batch` already re-derives it against a fresh read and refuses
`plan_stale`.

`preview` reads the 120-second report cache, which is right at proposal time —
the operator is looking at that same picture. **The capacity handlers re-derive
the fingerprint with `refresh=True` before mutating**, because an early check
against a cached envelope can pass on a fleet that has already moved.

**The fingerprint is the FIRST field of the revision**
(`<fingerprint>:<action>:<resource>`), and `_fingerprint_of` reads it from the
front. The registry stores `str(revision)[:128]` into a 128-character column,
so only what is composed *after* the 64-character digest can be clipped — and a
clipped digest would still round-trip through `_require_bound_revision` (both
sides truncate identically), claim the record, and then fail the live
comparison forever with a false "the fleet changed". An RDS identifier alone
can be 63 characters, so this ordering is load-bearing, not cosmetic.

`apply_capacity_plan`'s `preview` is the one preview that writes: `plan_batch`
stores the plan under `PLAN_TTL`. Accepted, because it is the identical bounded
cache write the Admin performs on every debounced stepper tweak, it touches no
provider and no durable state, and rendering the server's own worded, priced
plan is the whole point of the card. Execution re-plans from the **stored**
steps and applies back to back, so the model cannot alter the approved plan.

---

## Bounding and redaction

`common.bounded(value, depth=4)` is the one bounding function, applied to every
projection. A per-source key allowlist across fourteen dashboard collectors
would be a second schema to maintain; one deny rule plus one budget is
auditable in a single test.

| Rule | Default | Notes |
|---|---|---|
| Node budget | 400 | per call |
| Byte ceiling | 24 KB | **never raised** — the outer bound in every case |
| Per container | 40 keys or items | per call |
| Strings | 200 characters, marker included | through `logit.mask_sensitive_data` |
| Depth | per tool (3–6) | the marker is a **scalar**, so depth N returns N levels |
| Dropped by name | `stderr_tail`, `node_evidence`, `transitions`, `diagnosis`, `frozen_roster` | at any depth |
| Masked by name | anything in `logit.SENSITIVE_KEYS` or containing `password`, `secret`, `token`, `auth_key`, `onetime_code` | key kept, value replaced |

Depth, width and the node budget are all **per call**, because the envelopes
are not the same shape and because a tool that has already applied its own
documented cap must not have it silently undercut here. An `_aws_inventory` row
sits at depth 4–5 under `sections → envelope → data → resources → ec2[]`, so a
flat depth-2 cap would leave a caller holding only status strings; and a tool
that promises 100 rows must actually return 100.

Where a tool raises the defaults, these are the real numbers:

| Tool | Depth | Items | Nodes |
|---|---|---|---|
| `list_cloud_resources` | 4 | 100 (`MAX_RESOURCE_ROWS`) | 2200 |
| `fetch_cloud_metrics` | 4 | 60 (`MAX_METRIC_BUCKETS`) | 1200 |
| `get_platform_overview` — `deployments` section | 6 | 40 | 400 |
| `get_platform_overview` — every other section | 4 | 40 | 400 |
| `get_advanced_inventory` | 5 | 40 | 400 |
| `get_setup_readiness` | 5 | 40 | 400 |
| everything else | 3–4 | 40 | 400 |

The `deployments` section gets depth 6 because its useful fields sit two levels
deeper than the rest (`sections → envelope → data → items[] → item →
node_summary → counts`) and it is already a named allowlist projection, so the
extra depth adds no new surface. `max_bytes` is never raised: widening the
result never removes the size bound.

Named projections on top of that:

- `project_deployment` keeps `id`, `sha`, `framework_version`, `status`,
  `source`, `actor`, `retry_of`, `created`, `started`, `finished`,
  `duration_seconds`, `node_summary`, `current_commits`, `desired_commit` —
  and never the evidence journals, whose `detail.stderr_tail` exists precisely
  because the redactor has gaps a credential survives.
- `project_capacity` withholds Elastic IP allocation ids and the raw `assign`
  map: they are inputs to a mutation this domain does not offer. Egress is
  reported as counts and booleans.
- `project_series` keeps the 60 most recent buckets with `truncated`, plus
  `min`/`max`/`avg` per series.

### Provider failures

`common.provider_reason(exc, operation)` runs `map_error(exc, operation)` and
returns one of `credentials_unavailable`, `denied`, `network_unavailable`,
`service_error` — `detail()` is the only provider-exception shape safe to
record. Anything that is not a provider failure returns `None` and the caller
re-raises, keeping the assistant's ordinary tool-error path.

### Expected refusals return, they do not raise

`_execute_tool` turns a raised exception into "encountered an internal error"
plus a level-6 `assistant:error`, which is the wrong report for "you are not a
superuser". Handlers return `{"error": <sentence>, "error_code": <code>}` for
permission, session, unknown-resource, not-offered, external-mode and
provider-degraded outcomes; the approval boundary lands such a return as
`state="failed"` with that exact `failure_code`. Only genuine bugs propagate.

---

## `request_meta` on both transports

`request_meta` gained two fields in this item so a tool can tell an interactive
operator session from a machine credential:

| Field | REST | WebSocket |
|---|---|---|
| `bearer` | `request.bearer` | the consumer's server-stamped `_bearer` |
| `key_backed` | `request_helpers.is_key_backed_session(request)` | `bearer != "bearer"` |

Both **fail closed**: an unreadable request is treated as key-backed. The WS
value is built by `agent.build_ws_request_meta(bearer)` from the `_bearer` that
`mojo/apps/realtime/handler.py` stamps on every delivered message, always
overwriting what the client sent — so it is a server fact, not a claim.

---

## Audit

Each mutating handler calls `admin_platform.audit_after_commit(user, action,
target)` with the **exact** action string the mirrored REST handler uses, so the
two trails cannot drift:

| Tool | Action string |
|---|---|
| `retry_platform_deployment` | `retry_same_sha` |
| `verify_platform_deployment` | `verify` |
| `converge_platform_deployment` | `converge` |
| `apply_framework_update` | `framework_update` (written by the service) |
| `apply_managed_upgrade` | `aws_engine_upgrade` |
| `apply_capacity_change` | `capacity_rest.AUDIT_ACTIONS[action]` |
| `apply_capacity_plan` | `capacity_rest.BATCH_AUDIT_ACTION` |

`common.audit` then writes one `logit.Log` row carrying the conversation id, so
the Admin operation record ties back to the turn that asked for it. The
approval boundary already files `assistant:approval:*` and
`assistant:tool:<name>`; nothing here duplicates them.

---

## Reconciliation posture

Ambiguous provider outcomes are **never retried automatically**. Every mutating
result carries a `reconciliation` sentence naming the authoritative follow-up
read:

| After | Poll |
|---|---|
| the three deploy actions, framework update | `get_platform_overview(sections=["deployments"])` |
| `apply_managed_upgrade` | `get_upgrade_status` — `upgraded` is the only success signal; `settled` means AWS finished, not that the engine moved |
| `apply_capacity_change` | `get_capacity_operation_status(operation=…)` |
| `apply_capacity_plan` | `get_capacity_operation_status(batch=…)` — `batch_status` flags a stalled runner after 180s of silence |

---

## Out of scope

Arbitrary AWS queries or actions, shell/SSH access, IAM policy authoring,
requesting SES production access, manual deployment promotion, new cloud
providers, System Setup repair, DNS/certificate/edge-route management, S3
bucket administration, and WebApp deploys. GitHub Actions remains the WebApp
deployment control plane; this domain does not introduce a second release path.

## Key files

- `mojo/apps/assistant/services/tools/cloud/common.py` — the shim, `bounded`, the refusals, provider reasons, audit
- `mojo/apps/assistant/services/tools/cloud/reads.py` — the thirteen read tools and their projections
- `mojo/apps/assistant/services/tools/cloud/actions.py` — the seven mutating tools
- `tests/test_assistant/40_test_cloud_tools_registry.py`, `41_test_cloud_projections.py`, `42_test_cloud_read_tools.py`, `43_test_cloud_mutations.py`
- `tests/test_assistant_extended_serial/40_test_cloud_infrastructure.py`
- [Approvals](approvals.md) · [aws/capacity.md](../aws/capacity.md) · [aws/maintenance.md](../aws/maintenance.md) · [aws/infrastructure_mode.md](../aws/infrastructure_mode.md)
