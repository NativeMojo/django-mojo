# Platform and Advanced Admin controls

The built-in Admin separates platform operations into two feature-owned lanes.
**Platform** owns public API and local sanity proof, deployment history, fleet,
jobs/scheduler, database, Redis, certificate/security evidence, the public
WebApp summary contract, and System Setup. **Advanced** owns hosting inventory,
bounded opt-in AWS inventory, raw network controls, and typed settings.

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

The API probe pins public DNS to a global address, follows no redirects, caps
the response body, and reports latency, HTTP status, and version. Local sanity
reports migration status. Jobs evidence distinguishes edge runner heartbeats
from scheduler leadership. Security evidence distinguishes absent/stale cron
heartbeats and monitoring-delivery proof from healthy evidence and includes a
capped open-incident roster. AWS inventory is file-opt-in, uses bounded SDK
timeouts and one capped page per service, omits endpoints/IPs, and never creates
EC2, RDS, or ElastiCache resources.

The WebApp lane consumes only `webapp_onboarding.summary_for()` schema version
1 and redacted onboarding evidence. Registrar-versus-DNS provider evidence
comes from the durable onboarding operation. No deployment key or provider
secret enters the Platform response.

## Permissions and settings

Reads use dedicated global grants: `view_platform`, `view_platform_security`,
`view_advanced`, `view_advanced_inventory`, `view_advanced_security`, and
`view_advanced_settings`; the corresponding manage grant or `admin` also
passes. Writes require `manage_platform` or `manage_advanced`, reject API-key
and group-token sessions, and require authentication no older than 600 seconds.
Settings writes additionally re-read an active literal `account.User`
superuser.

`AUTH_CONFIG` and `EDGE_EXPECTED_TOPOLOGY` are protected from generic Setting
create, update, rename, and delete. Advanced's auth writer merges presentation
fields only, preserves unknown keys, validates the final config, and retains
password login for administrative recovery. Deploy/AWS/KMS/security settings
are file-only and read-only.

The migration is `edge.0010_platformdeployment`, directly after
`edge.0009_webapp_onboarding`. `bin/admin_preview` provides feature-owned,
deterministic Platform and Advanced evidence and action fixtures.
