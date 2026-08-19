# Platform and Advanced Admin controls

The built-in Admin separates platform operations into two feature-owned lanes.
**Platform** owns public API and local sanity proof, fleet,
jobs/scheduler, database, Redis, certificate/security evidence, the public
WebApp summary contract, System Setup, and CloudWatch Metrics. **Advanced** owns hosting inventory,
bounded opt-in AWS inventory and raw network controls. Ongoing configuration
has one first-class [Settings](settings.md) home. Platform
is an operational evidence surface, not another directory: ongoing Domains &
DNS and web-app work stays in those first-class destinations, while Advanced is
one expert-diagnostics link rather than an expanded resource menu. The
deployment journal's **browser surface** — the attempt list, its drill-in, and
the same-SHA recovery controls — renders in the merged Deployments lane
(`#/deployments`, the `webapps` feature); the backend evidence and action
contracts below are unchanged and remain this page's subject.

## Deployment journal

`edge.PlatformDeployment` is the durable record for every external endpoint,
GitHub webhook, and Admin retry. Its UUID travels through the Redis target and
lease, orchestrator and node job payloads, update-script argv,
`deploy_status`, node proof, evidence rows, and every compare-and-set. SHA alone
is never attempt identity: a late callback for an older attempt is refused even
when a newer attempt deploys the same SHA.

The row freezes only live runners advertising the `edge` channel. It stores
bounded transitions and one latest sanitized observation per frozen runner.
GitHub's delivery id is conditionally unique; caller idempotency keys are also
durable. Coordination, queue publication, canary proof, fleet dispatch,
verification, convergence, partial/unknown outcomes, publish failure, and
supersession remain queryable after Redis expires. A five-minute reconciler
marks abandoned active attempts `unknown` after their lease vanishes.

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
allowlist naming which sections to collect (the merged Deployments lane calls
`?sections=deployments,api`). It narrows work, never authority: each named
section keeps its own permission tuple, unknown names are ignored, and an
absent or empty parameter collects the full roster exactly as before. A
parameter naming no known section collects nothing.

The API probe pins public DNS to a global address, follows no redirects, caps
the response body, and reports latency, HTTP status, and version. Local sanity
reports migration status and the bounded local-target source:
`configured_static`, `request_server_port`, or `default_80`. The report never
returns the raw deployment setting. Jobs evidence distinguishes edge runner heartbeats
from scheduler leadership. Security evidence distinguishes absent/stale cron
heartbeats and monitoring-delivery proof from healthy evidence, names disabled
HTTPS redirect/secure-cookie/HSTS controls as boolean posture, and includes a
capped open-incident roster. Ignored, resolved, and closed incidents are
terminal and excluded from both the roster and every Dashboard/Platform count.
AWS inventory is file-opt-in, uses bounded SDK
timeouts and one capped page per service, omits endpoints/IPs, and never creates
EC2, RDS, or ElastiCache resources.

The WebApp lane consumes only `webapp_onboarding.summary_for()` schema version
1 and redacted onboarding evidence, then independently probes each configured
HTTPS origin with the existing DNS-pinned, no-redirect public probe. The
collector is capped at 24 applications, four workers, 1.5 seconds per probe,
and a 2.5-second collector deadline. Each result carries `observed_at` and
`stale_after`; timeout, unsafe, missing, and unknown evidence never becomes
green. Current public health, configured origin, historical onboarding state,
and deployment-key state are separate axes, so `not_started` history does not
claim a currently healthy site is down. Registrar-versus-DNS provider evidence
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

The feature owns four routes: `platform`, `setup`, `metrics`, and
`maintenance` (the `deployments` route belongs to the merged Deployments
lane). The first two follow the grants above; the last two are
gated separately on `manage_aws`, published as
`features.platform.capabilities.metrics` and
`features.platform.capabilities.maintenance` and enforced on `/api/aws/*` by
`@md.requires_global_perms("manage_aws")`. An operator holding only
`manage_aws` therefore sees the Metrics and Maintenance sidebar entries and
nothing else in this lane. See [Admin Metrics](metrics.md).

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
  single `blocked_reason` — `update_unavailable`, `requires_superuser`, or
  `no_converged_deployment`.
- `POST /api/account/admin/platform/framework/update` (`manage_platform`,
  `admin`, plus key denial and 600-second fresh auth) **clears** any pin and
  redeploys the last converged commit. It never writes a version into the pin:
  that would freeze the fleet at today's release instead of updating it, and
  the mistake would stay invisible until the next release.

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

The migration is `edge.0010_platformdeployment`, directly after
`edge.0009_webapp_onboarding`. `bin/admin_preview` provides feature-owned,
deterministic Platform and Advanced evidence and action fixtures.
