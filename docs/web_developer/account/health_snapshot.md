# Reading protected health snapshots

Use `GET /api/account/admin/health/snapshot`, optionally with the opaque
`evidence_cursor` from the previous response. Assistant/MCP exposes the same
service as read-only `get_health_snapshot(evidence_cursor=None)`. Source
collection runs every 300 seconds; external clients may poll every 60 seconds.
This endpoint reads the cache and never starts a collector. The existing
`get_platform_health` tool retains its current behavior.

Entry needs current global `view_platform`, `manage_platform` or `admin`.
AWS facts additionally need `manage_aws`; checked host, queue and durable job
facts need `manage_jobs`, `view_jobs` or `jobs`. Group-only grants and group
`ApiKey` identities do not authorize this installation-wide report. Validated
per-user `UserAPIKey` credentials retain their User's global grants. Permissions
are re-read from the primary User on every request. Revocation removes protected facts and
resource identities even from a previously privileged cache. Denied grant-level
sections have an unknown denominator.

## Version 2 wire fields

The contract is `django-mojo.health_snapshot`, `schema_version:2`,
`profile_version:2`. Source identity is `{installation_id,environment_id}` using
the frozen installation UUID and explicit configured environment. `source_id`
is its stable hash. A release, URL or host name is not source identity.
Missing/conflicting identity yields unknown; no read initializes it. The scheduled
cron hook skips collection for unresolved identity without dispatching a producer,
allowing later scheduled maintenance to continue.

The envelope carries:

- `publication_generation`: positive monotonic integer; gaps are allowed.
- `publication_digest`: immutable hash of the original validated cache object.
  It survives permission/expiry projection and is suitable for comparing one
  generation under different current grants.
- `config_revision`: opaque immutable configuration hash; restricted roster
  details are withheld separately.
- `generated_at`, `published_at`, response-only `served_at`, `roster_revision`,
  `collection_status`, `components`, `coverage`, and `failure_evidence`.

Components have `component_id:kind:resource_id`, kind `api|database|redis|jobman|
fleet|monitoring`, `resource_scope`, `resource_id`, `resource_owner_source_id`,
nullable `service_id`, `required`, `status`, `availability`, actual observation/
success/deadline fields, sample ID, reasons, evidence, conditions and coverage.
Primary and declared WebApps have independent API rows (up to24); shared DB,
Redis, fleet and jobman appear once. A consumer owns cross-system associations
and authoritative ownership for resources shared between sources.

`status` is `healthy|degraded|unhealthy|unknown|stale|not_applicable`;
`availability` is `up|down|unknown|not_applicable`. A proved target failure can
remain down while unrelated pressure evidence is unknown. A successful ping
alone cannot establish complete health. Coverage has expected/returned/observed/
fresh/unknown/omitted totals, complete/truncated and reasons. Null denominator
means unknown. Omitted required slots or conditions remain incomplete.

Slots retain source identity, required grant, real `observed_at`, `collected_at`,
`last_success_at`, `fresh_until`, `sample_id`, value/unit and provenance/limits.
Direct evidence expires after 420 seconds; CloudWatch expires at the earlier of
sample plus three collection periods and collection plus420 seconds. Worker
admission still requires three heartbeat intervals (15 seconds by default).
An admitted worker sample remains usable in cached snapshots for the existing
420-second window anchored to its actual heartbeat time. Provenance records
`heartbeat_interval_seconds` and `live_admission_window_seconds`. This is a
point-in-time presence observation, not continuous worker liveness; stale or
missing collection evidence is not admitted. On-demand runner evidence keeps
its live lease policy. Over30-second future/missing/invalid times are
unknown; tolerated clock skew grants no extra healthy lifetime. A read or new
publication cannot renew an old provider sample. Failed collection retains old
facts only as prior evidence. Keep expiring slots locally when polls fail.

Conditions are bounded typed `{condition_key,outcome,impact,reason,basis}` rows.
Basis entries reference authoritative slot/resource/sample/time/deadline fields.
Only independently advancing fresh causal samples can confirm one condition;
a component hash, generation or repeated poll cannot confirm twice. Recovery
needs independently advancing passes for the confirmed causes. There are at most
1,024 conditions and eight basis references each.

Profile2 reports >=90% proved CPU/memory/filesystem/inode/capacity consumption,
>=1-second DB/PING and RDS read/write latency, positive comparable deadlock/
eviction/rejection deltas, due backlog with no progress across independent
300-second samples and proved runtime overruns as degraded pressure. Fresh idle
or drained queues pass the stall predicate. Counter reset is unknown; unknown
capacity remains raw facts rather than invented percentages. Scheduler lock
presence has installation scope and no independent heartbeat. Missing detector
evidence is explicit unknown.

The fleet component's existing `source:request_service` slot carries current
signed request readiness for each required node, with the same interpretation
as node diagnostics and monitoring coverage. It requires exact node/host,
current runner start, revision and root-bound role/profile agreement. Installed
configuration or deployment completion alone is not readiness. A known boolean
`healthy:false` produces a failing availability condition and can keep the fleet
down while other sources are unknown. Missing/stale/mismatched proof remains
Unknown; explicit proved exclusion is `not_applicable` and adds no availability
pass. Request evidence expires 420 seconds after actual proof completion;
collection/publication/polling times cannot renew it. Removing jobs permission
removes the slot and its causal condition. `get_fleet_health` still reports only
provider/runner presence, without request-proof fan-out or cached service health.

## Failure cursor and limits

One read has a three-second budget, a 512KiB cached publication and one200-row
failure page. Failure pages include existing failed/expired JobEvents and a
bounded current terminal Job fallback, with canonical UUID hex IDs. No payload,
error text or routine completed/retry jobs are emitted. Dedupe events by event ID
and fallback by terminal tuple. Manual retry publishes a replacement and keeps
the original terminal fields.

The signed cursor freezes source/profile/query, closed UTC start/end, stream
phase, `(at,id)` position and a fixed one-hour expiry. The first window is24h;
completed windows overlap600 seconds. DB errors return a signed unread checkpoint,
including the first page. Continuation does not slide a window or renew expiry.
MCP has a24KiB ceiling: omitted data is incomplete, the REST reference is returned,
and its cursor points to the start of omitted failure rows, never past them.

`capture_status:best_effort` and `limitations` disclose event-write failures,
unproven retention, old-job prune/cascade, manual purge and commits beyond600s
of overlap. They do not permanently mark current queue health unknown. Observed
read failure, page backlog, output truncation, expired window or an interval
beyond24h appear under `gaps`, with unknown lost counts. Empty pages cannot prove
lossless zero failures. Persist available evidence independently of replacement
snapshots.

The producer uses one60-second cycle, four concurrent calls,128 targets,
32 provider operations,256 metric series,32 channels,24 APIs,256 Redis commands,
16 SQL operations and a512KiB/2,048-slot publication. Caps preserve omitted
obligations. Current/previous publications are retained48h; TTL does not imply
health. The durable generation/configuration fence rejects old collectors and
restored Redis state. Bounded RedisCluster startup is unsupported and appears as
a collection/read gap; no default connection fallback supplies green status.

## Monitoring coverage drilldown

`get_monitoring_coverage(node_id=None,cursor=None)` accepts either an exact known
expected node or an opaque signed cursor. It returns schema1, one expected node
per page, global roster counts and local `measurement_coverage` with
`scope:returned_node`. Unknown/ambiguous nodes and stale cursors are refused.
Alias/role/roster changes require restarting from a fresh page.

Each measurement has stable key, node, role provenance, optional mount and
independent runtime/continuous-source slots. Coverage distinguishes covered,
stale, missing, unknown, unsupported, not_configured, permission_denied,
collector/provider errors, not_applicable, incomplete and not_collected_budget.
Partial provider series retain usable samples while staying incomplete. Known
root/data mounts remain named when missing or capped. Root alone cannot prove
complete filesystem enumeration. Unknown roles preserve unknown obligations.

CloudWatch uses actual samples in a fixed previous30-minute window: CPU300s,
memory60s and disk300s, exact namespace/full dimensions, four discovery pages,
twelve operations and eight collected mount series. Runtime has20-second
freshness. Recipe declaration does not prove installation;
`agent_installation.status` stays unknown. Fresh runtime cannot hide a missing
continuous stream. Every multi-node page remains incomplete, including the last;
full traversal of one unchanged revision is required for fleet coverage.

No read installs an agent, changes a detector or runs remediation. No anonymous
raw endpoint is added. Public status must be a separately authorized safe
projection of persisted consumer state, without source internals.
