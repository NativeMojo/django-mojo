# System Setup and platform evidence

`GET /api/account/admin/platform` is the bounded evidence endpoint for public
API and local sanity proof, fleet, jobs/scheduler, database, Redis,
certificate/security evidence, the deployment journal, and the public WebApp
summary contract. `GET /api/account/admin/advanced` is the same for hosting
inventory, bounded opt-in AWS inventory, and network posture. Ongoing
configuration has one first-class [Settings](settings.md) home.

**Neither endpoint has a page of its own any more.** The Platform health grid
and the Advanced diagnostics page were dissolved: their evidence is summarized
on the Dashboard rows, and the raw payload sits behind each row's Details
drill-in. What the `platform` feature still owns is work an operator starts on
purpose — three routes, `setup`, `metrics` and `maintenance` — and `advanced`
owns the first-class Domains & DNS destination plus the raw hosting routes.

Consequently `platform_overview` is consumed **only through `?sections=`**:

| Caller | Request |
|---|---|
| Deployments lane (`webapps` feature) | `?sections=deployments,api` |
| Dashboard EC2 drill-in | `?sections=fleet` |
| Dashboard Incidents drill-in | `?sections=security` |

`advanced_overview()` and `GET /api/account/admin/advanced` are **unchanged and
still served** — they simply have no portal caller now. Stated plainly:
`view_advanced`, `view_advanced_inventory`, and `view_advanced_security` grant
API access with no browser surface behind them, and `registrar_vs_dns` (inside
`webapps.data`) is API-only pending the Domains & DNS review.

The deployment journal's **browser surface** — the attempt list, its drill-in,
and the same-SHA recovery controls — renders in the merged Deployments lane
(`#/deployments`, the `webapps` feature). The attempt drill-in is
**explanation-first**: a failed attempt opens on the failure story (phase,
runner, rollback target and outcome, sanitized stderr tail — from the durable
`diagnosis` journal, with a transition-detail fallback for rows written before
it existed), then a compact transition timeline and a "currently serving"
line, and only then the controls. Controls are **state-specific** — failed:
Retry same SHA + Verify; in-flight (requested/canary/fleet): none; verified/
partial/unknown: Verify + Converge; converged: Verify; superseded: none — and
the list **polls** every 10 seconds while a deploy is in flight (a live
coordination lease or an unsettled newest attempt), capped at 36 ticks,
guarded on the `#/deployments` hash, and cleared on reload and teardown. The
backend evidence and action contracts below remain this page's subject.

## Deployment journal

`edge.PlatformDeployment` is the durable record for every external endpoint,
GitHub webhook, and Admin retry. Its UUID travels through the Redis target and
lease, orchestrator and node job payloads, update-script argv,
`deploy_status`, node proof, evidence rows, and every compare-and-set. SHA alone
is never attempt identity: a late callback for an older attempt is refused even
when a newer attempt deploys the same SHA.

The row freezes only live runners advertising the `edge` channel. It stores
bounded transitions, one latest sanitized observation per frozen runner
(`node_evidence`), and the append-only, bounded `diagnosis` journal (item
2225): the terminal failure per runner with its phase, `rollback_to` target
and sanitized stderr tail, plus rollback-outcome entries from update.sh's
second report and from the lease-independent post-restart sweep. The latest
observation is what a later probe *replaces*; the diagnosis is what survives
it — see [edge/deploy.md](../../edge/deploy.md) for the write rules and
bounds. GitHub's delivery id is conditionally unique; caller idempotency keys
are also durable. Coordination, queue publication, canary proof, fleet
dispatch, verification, convergence, partial/unknown outcomes, publish
failure, and supersession remain queryable after Redis expires. A five-minute
reconciler marks abandoned active attempts `unknown` after their lease
vanishes.

The existing `POST /api/edge/deploy` contract is unchanged and accepts a
validated arbitrary SHA. Admin cannot choose a new SHA: it can only retry the
SHA on an existing row, request UUID-bound verification, or publish existing
desired hosting state and re-verify it.

## Evidence contract

`GET /api/account/admin/platform` and `GET /api/account/admin/advanced` return
schema version 1. Each section has `status`, `observed_at`, `stale_after`, and a
bounded `data` object. Stable states include `healthy`, `unhealthy`,
`unauthorized`, `unavailable`, `timeout`, and `unconfigured`. Raw exceptions
and provider responses are never returned, and permission is checked before a
collector runs.

The platform overview accepts an optional `?sections=` comma-separated
allowlist naming which sections to collect — now the only way it is called
(see the caller table above). It narrows work, never authority: each named
section keeps its own permission tuple, unknown names are ignored, and an
absent or empty parameter collects the full roster exactly as before. A
parameter naming no known section collects nothing.

The API probe pins public DNS to a global address, follows no redirects, caps
the response body, and reports latency, HTTP status, and version. Local sanity
reports migration status and the bounded local-target source:
`configured_static`, `request_server_port`, or `default_80`. The report never
returns the raw deployment setting. `sanity.check_redis` accepts an optional
`redis_client` in its options (an additive seam beside `check_migrations`'s
`migration_executor_cls`) so a caller on a response budget can inject a bounded
client; called without one it uses `get_client()` exactly as before. Jobs
evidence distinguishes edge runner heartbeats from scheduler leadership. Security evidence distinguishes absent/stale cron
heartbeats and monitoring-delivery proof from healthy evidence, names disabled
HTTPS redirect/secure-cookie/HSTS controls as boolean posture, and includes a
capped open-incident roster. Ignored, resolved, and closed incidents are
terminal and excluded from both the roster and every Dashboard/Platform count.
AWS inventory is file-opt-in, uses bounded SDK
timeouts and one capped page per service, omits endpoints/IPs, and never creates
EC2, RDS, or ElastiCache resources.

The `webapps` evidence section consumes only `webapp_onboarding.summary_for()`
schema version 1 and redacted onboarding evidence, then independently probes
each configured HTTPS origin with the existing DNS-pinned, no-redirect public
probe. The collector is capped at 24 applications, four workers, 1.5 seconds
per probe, and a 2.5-second collector deadline. Each result carries
`observed_at` and `stale_after`; timeout, unsafe, missing, and unknown
evidence never becomes green. Current public health, configured origin,
historical onboarding state, and deployment-key state are separate axes, so
`not_started` history does not claim a currently healthy site is down.
Registrar-versus-DNS provider evidence
comes from the durable onboarding operation. No deployment key or provider
secret enters the Platform response.

## Permissions and owner APIs

Reads use dedicated global grants: `view_platform`, `view_platform_security`,
`view_advanced`, `view_advanced_inventory`, `view_advanced_security`, and
`view_advanced_settings`; the corresponding manage grant or `admin` also
passes. Writes require `manage_platform` or `manage_advanced`, reject API-key
and group-token sessions, and require authentication no older than 600 seconds.
Typed AUTH_CONFIG/topology owner writes additionally re-read an active literal
`account.User` superuser.

The feature owns four routes: `setup`, `metrics`, `maintenance`, and `fleet`
(the dissolved `platform` route is gone; `deployments` belongs to the merged
Deployments lane). `setup` requires an active literal superuser, published as
`features.platform.capabilities.setup`. `metrics` and `maintenance` are gated
separately on `manage_aws`, published as
`features.platform.capabilities.metrics` and
`features.platform.capabilities.maintenance` and enforced on `/api/aws/*` by
`@md.requires_global_perms("manage_aws")`. `fleet` rides
`features.platform.capabilities.capacity` — superuser AND `manage_aws`, the
same gate as the Dashboard's capacity drill-in. An operator holding only
`manage_aws` therefore sees the Metrics and Maintenance sidebar entries and
nothing else in this lane. See [Admin Metrics](metrics.md).

`features.platform.capabilities.setup_attention` rides alongside `setup`: true
only for a superuser on an installation with no `BASE_URL`. It badges the
System Setup sidebar entry, which is the one unmissable reason to open Setup
now that it is a destination rather than a page-grid card. It is computed in
`on_admin_bootstrap` and is always `false` when `setup` is false.

### Fleet Scaling

`fleet` puts the whole fleet on one page — app nodes, the Redis replication
group, and the database with its readers (live size dropdowns included, fed
by the report's curated `sizes` ladder) — as steppers over a staged desired
state. Nothing mutates while the operator edits: the staged changes are sent
to `POST /api/aws/capacity/plan`, and the bottom bar renders the **server's**
plan — its plain-English step descriptions and warnings, its execution order
(additions → resizes → removals, a terminate pinned behind its drain), and
its per-step and total monthly cost delta from provision's price table
(unpriced types are an honest null, never a silent $0). One confirmation
applies that exact plan by its `plan_id`
(`POST /api/aws/capacity/plan/apply`); the server then runs the steps as one
batch **through the unchanged capacity actions** (`add_node`, `drain_node` +
`terminate_node`, `set_cache_replicas`, `resize_cache`, `resize_database`,
`add_reader`, `remove_reader`), each re-validated against the live fleet the
moment it runs, and the page polls one batch status
(`GET /api/aws/capacity/status?batch=…`) to a terminal state — a failed step
stops everything after it, and the bar says exactly which steps completed,
failed, and were not attempted. This page adds no new authority: both batch
endpoints carry the full `/api/aws/capacity/apply` gate (superuser AND
`manage_aws`, key denial, 600-second fresh auth, `INFRASTRUCTURE_MODE`
refusal), an expired or fleet-drifted plan is refused (`plan_not_found` /
`plan_stale`) and re-requested rather than silently re-applied, and a
control the server's report does not offer is disabled with the server's
`blocked_reason` in plain words. On an `external`-mode installation the page
renders read-only.

All three packaged Add Node surfaces expose the API's optional placement
without changing its automatic default: v2 Infrastructure Capacity, legacy
Fleet Scaling, and the legacy Dashboard Capacity drill-in. The source picker
contains healthy nodes only and labels each option with node name, target-group
fleet name, availability zone, and current subnet. The subnet field is free
text with a datalist of distinct in-use subnets; choosing a source narrows that
guidance to its zone but does not fill or erase what the operator typed. Empty
controls omit both `source_instance` and `subnet_id`.

The batch pages keep source and subnet in controlled `want` state and copy the
same non-empty placement onto every staged `add_node` step. Subnet keystrokes
update the helper's summary, `aria-invalid`, and inline alert without replacing
the page (and losing focus). They immediately invalidate a cached plan and
disable Apply; change/blur commits through the existing debounced plan request.
The Dashboard appends the helper's `.element` to its typed-echo modal and
requires both literal `add_node` and valid placement before enabling its
button. Local validation is deliberately cheap: an empty value is automatic;
a non-empty value must begin `subnet-` and include an identifier. AWS remains
authoritative, so a syntactically valid but unusable subnet is shown using the
server's human-readable refusal.

`bin/admin_preview` mirrors this contract with two healthy sources in distinct
target-group fleets, zone/subnet facts, placement-aware single and batch
descriptions, and `subnet-0refused` as a deterministic
`subnet_not_usable` response. The preview response writer preserves the
production browser-wire error envelope —
`{status:false,error,error_code,data}` for non-2xx provider dictionaries —
while successful dictionaries remain `{status:true,data:...}`. Asset tests are
structural guards only; desktop and 375px browser checks still prove focus,
wrapping, validation, and refusal presentation on all three surfaces.

### Maintenance

`maintenance` is the only Platform route that changes infrastructure outside
this installation, and it needs **two** grants, not one: `manage_aws` opens the
page, and applying an upgrade additionally requires superuser, `manage_platform`,
or `admin`. The decorator composes its permissions with OR, so the AND is an
explicit in-body check on `POST /api/aws/maintenance/apply`. `manage_aws` alone
is the grant that reads CloudWatch charts; it is not the grant that reboots the
production database.

The same page owns the framework update, on the platform grants rather than the
AWS one (the Deployments lane's django-mojo row surfaces the same two
endpoints with the same typed-echo contract — one mechanism, two doorways):

- `GET /api/account/admin/platform/framework` (`view_platform`,
  `manage_platform`, `admin`) reports installed vs published, the pin, and a
  single `blocked_reason` — `update_unavailable`, `requires_superuser`,
  `no_converged_deployment`, or `infrastructure_external`. **The read is never
  gated**, in any mode: what runs here and what is published are facts every
  surface needs.
- `POST /api/account/admin/platform/framework/update` (`manage_platform`,
  `admin`, plus key denial and 600-second fresh auth) **clears** any pin and
  redeploys the last converged commit. It never writes a version into the pin:
  that would freeze the fleet at today's release instead of updating it, and
  the mistake would stay invisible until the next release.

  It is **disabled outright** when `INFRASTRUCTURE_MODE` is `external` — 403
  `infrastructure_external`, refused as the first statement in the endpoint
  body, before the caller's grants and before the offered-version re-check,
  with a matching service-layer backstop in `apply_framework_update` for
  non-REST callers. On such an installation the fleet's django-mojo version is
  the IaC pipeline's to change. See
  [aws/infrastructure_mode.md](../../aws/infrastructure_mode.md), which also
  explains why an external installation must pin `EDGE_FRAMEWORK_VERSION`.

`platform_deploy.last_converged_deployment()` is the row-returning sibling of
`last_converged_framework()` — converged, because that status is the
reconciler's proof the commit actually runs on this fleet.

Full contract, IAM actions, and error codes:
[aws/maintenance.md](../../aws/maintenance.md).

`AUTH_CONFIG` and `EDGE_EXPECTED_TOPOLOGY` are protected from generic global
Setting create, update, rename, and delete. Their compatibility writer merges appearance,
login-method, and registration-method fields only, preserves unknown keys, and
validates the final config. Login must retain `password` for administrative
recovery; enabled registration must retain at least one method. Navigation,
API-base, redirect, and external-CSS URL fields are not writable. Deploy,
AWS, KMS, and security settings are owner-managed or file-only. Advanced no
longer renders a duplicate typed form; Settings is the browser UI home.

The migrations are `edge.0010_platformdeployment` (directly after
`edge.0009_webapp_onboarding`) and `edge.0012_platformdeployment_diagnosis`.
`bin/admin_preview` provides feature-owned, deterministic Platform and
Advanced evidence and action fixtures.
