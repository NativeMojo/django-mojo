# Protected health snapshots and monitoring coverage

`mojo.apps.account.services.health_snapshot` publishes deterministic source
observations every 300 seconds. `GET /api/account/admin/health/snapshot` and the
read-only Assistant/MCP `get_health_snapshot(evidence_cursor=None)` return the
same cached projection. A consumer may poll every 60 seconds. A read never
starts collection; a missing, invalid, unsupported, stale or unavailable cache
remains unknown. `get_platform_health` retains its existing behavior.

The account cron hook enqueues one ordinary job on the configured
`ADMIN_FLEET_CONFIG_CHANNEL` (default `edge`). Source/300-second-slot idempotency
and a primary advisory transaction coalesce dispatch into one running attempt
plus the latest pending attempt. Pending older slots are replaced; expired jobs
exit. There is no catch-up replay, per-node producer job, agent or remediation.
Normal existing cron/job publication and channel configuration must be running.
The bounded 60-second collection starts inside `produce_snapshot(job)`.

## Identity, publication and storage

The wire contract is `django-mojo.health_snapshot`, `schema_version:2`,
`profile_version:2`. Source identity is `{installation_id, environment_id}`:
read the existing frozen `MOJO_INSTALLATION_UUID`/slug pair and file-only
`MOJO_ENVIRONMENT`, or the explicit non-provisioned fallback
`HEALTH_SNAPSHOT_ENVIRONMENT_ID`. Conflicting/missing values are
`source_identity_unresolved`; reads never initialize identity. `source_id` is
SHA-256 of canonical JSON `[installation_id,environment_id]`.

Components have source-scoped `kind:resource_id` identities. Kinds are `api`,
`database`, `redis`, `jobman`, `fleet`, and `monitoring`. Primary and declared
WebApp origins have separate API service IDs (at most 24); shared DB, Redis,
fleet and jobman appear once. Each component carries `resource_scope`,
`resource_id`, `resource_owner_source_id` and nullable `service_id`. Consumers
own cross-system associations and selection of an authoritative owner for a
resource shared by deployments. Duplicate source facts do not confirm twice.

The protected global `MOJO_HEALTH_PUBLICATION_STATE` Setting holds only each
source's monotonic generation/configuration head (at most 64 sources). A private
primary transaction reserves a generation. Gaps are allowed. Current, previous,
reservation, published marker and 75-second token lease share one Redis hash
slot. Publication validates the durable current head/configuration under its
primary row lock through the atomic Lua replacement, rechecks current source
configuration, verifies the lease and reservation, and requires increasing
generation. A lost/replaced lease, late collector, old restored Redis backup,
identity/configuration change or unavailable primary refuses publication.
Redis flush/restart cannot reset the durable counter. Current and previous
immutable publications live for 48 hours; this retention does not imply health.
Generic Settings/Setup writers cannot alter this internal counter.

The envelope includes `publication_generation`, immutable `publication_digest`,
opaque `config_revision`, collector-start `generated_at`, `published_at`,
`roster_revision`, `collection_status`, `components`, `coverage` and
`failure_evidence`; reads add `served_at`. The digest covers the complete original
cached publication and is validated before projection. Permission/expiry
projection keeps that digest and configuration hash, rather than hashing a new
body or renewing source times. A consumer can compare one generation across
different grants. Restricted roster details are withheld.

## Actual observations and conditions

Every slot carries source identity, actual `observed_at`, `collected_at`,
`last_success_at`, `fresh_until`, `sample_id`, nullable value/unit, provenance,
limits, required grant and truncation. Successfully observing a failed target
counts as a successful observation. A collector exception has no new observation;
prior facts keep their original IDs/times/deadlines as prior evidence.

Direct observations expire after 420 seconds. CloudWatch uses the earlier of
sample time plus three declared/requested periods and successful collection
plus 420 seconds. Worker admission still requires the existing live lease of
three configured heartbeat intervals (15 seconds by default). An admitted worker
sample uses the direct-observation window in cached snapshots, anchored to its
actual heartbeat time. Its provenance records `heartbeat_interval_seconds` and
`live_admission_window_seconds`. This is a point-in-time presence observation;
it does not prove continuous worker liveness. Stale or missing collection evidence
is not admitted. On-demand runner evidence retains its live lease policy.
Source times over 30 seconds ahead, malformed times and missing times are
unknown; tolerated skew grants no extra freshness. Cached reads re-evaluate
actual deadlines. Collection, publication and served times cannot renew a source
sample. Recollecting one provider datapoint preserves its sample ID.

Each component has status/availability, source-minimum freshness and typed
conditions `{condition_key,outcome,impact,reason,basis}`. Basis entries name
actual slots, resource keys, sample IDs, observation times and deadlines. There
are at most 1,024 conditions and eight references per condition. Composite
component IDs/hashes are never sufficient confirmation proof. A proved failure
survives unrelated missing evidence; missing/denied/stale required evidence
prevents complete health. Output omissions are counted and remain incomplete.
Unknown denominators are null, including truncated roster/service enumeration.

Profile 2 records CPU/memory/filesystem/inode or proved capacity use >=90%,
direct DB/PING and RDS read/write latency >=1 second, positive comparable
deadlock/eviction/rejected-connection deltas, proved runtime overruns, and due
backlog without progress across independent samples at least 300 seconds apart.
Fresh idle or drained queues pass the stall predicate. A reset/comparison gap
is unknown. PostgreSQL deltas require the same postmaster and stats-reset epoch;
Redis deltas require the same run ID. Thresholds, actual units and capacity
provenance remain in the evidence. Values without capacity (for example
FreeableMemory/FreeStorageSpace) remain raw facts with unknown utilization.
Pressure degrades status; only proved availability failures establish down.

## Bounded sources

One private batch composes the fleet readers; it does not loop public fleet/node
tools. A cycle has a 60-second monotonic deadline, at most four concurrent source
calls, 128 expected targets, 32 provider operations (four inventory and four
metric-discovery pages), 256 metric series, four checked broadcasts with five
seconds total reply wait, 32 declared channels, 24 API services, 256 Redis
commands and 16 primary SQL operations. All default inventory, identity-cache,
roster, scheduler, checked transport, queue and metric operations charge that
same budget. Caps retain named/countable obligations as unknown or
`not_collected_budget`; assembly fits 2,048 slots and 512 KiB without raising
caps. Independent completed sources survive other sources' failures.

Primary PostgreSQL acquisition uses bounded DNS/numeric dialing and nonblocking
libpq connect/operations, a private transaction, 750 ms SQL/lock limits and closed
local resources. Unsupported engines/connection modes remain unknown. HTTP
probes use validated fixed public HTTPS origins, pinned numeric addresses,
original Host/TLS name, one 1.5-second absolute deadline, no redirects and bounded
version/HTML bodies. TLS checks its remaining budget after local context setup.
There is no detached timeout worker or unchecked collection fallback.

Raw DB facts include SELECT1 latency/reachability, transaction/deadlock counters,
active/idle-in-transaction counts and oldest transaction. Redis PING/INFO includes
memory/capacity, eviction/rejection/client/blocking facts and run identity, with
`scope:default_node`. Managed RDS/cache metrics require the selected declarations
and exact actual connection endpoint binding. An Aurora cluster endpoint also
requires its current `IsClusterWriter` member before instance dimensions are
chosen. Missing binding or member truncation remains unknown. Bounded
RedisCluster startup is explicitly unsupported; no singleton fallback or cluster
management is added. Default-node INFO is never claimed as complete cluster data.

Job facts are grouped primary queries for due pending/oldest due, running/oldest
started, latest completed, proved `max_exec_seconds` overruns and five-minute
completed/failed/expired/retry aggregate counts. Future scheduled work is not
overdue. Pipelined Redis queue/processing/scheduled counts and earliest processing
score are separate facts; that score is a visibility deadline, not exact runtime.
Scheduler-lock presence is installation-only with unknown heartbeat freshness.
The independent stopped-scheduler/runner detector is separately owned; absent
detector evidence stays `detector_evidence_unavailable`.

## Authorization and failure evidence

Entry requires current global `view_platform|manage_platform|admin`. The service
re-reads the actual primary User permissions for every request, so an old loaded
User cannot retain revoked grants. AWS slots require `manage_aws`; checked
runtime, queue and durable job pages require `manage_jobs|view_jobs|jobs`.
Group-only grants and group `ApiKey` identities do not authorize this report.
Validated per-user `UserAPIKey` credentials retain their User's global grants.
Denied slots and canonical resource/service/owner identities are removed;
grant-level denial has an unknown
denominator. No denied source is queried. Fast reads use one private primary
Redis acquisition (500 ms commands), bounded primary reads (750 ms operations),
a three-second total deadline and one 200-row failure page. MCP fits 24 KiB and
provides the REST reference with explicit omissions.

Durable pages contain only existing failed/expired JobEvent
`id,job_id,channel,event,at,attempt` and bounded current terminal Job fallback
`id,channel,status,attempt,finished_at`. UUIDs are canonical hex strings. No job
payload, error, query text, username or routine success/retry job stream is
returned. Dedupe events by ID and fallback by its terminal tuple. Manual retry
creates a replacement and preserves original terminal fields.

The signed `evidence_cursor` binds source/profile/query, a closed UTC window,
stream phase, `(at,id)` position and fixed one-hour expiry. The initial window is
24 hours. Exhausted windows reread 600 seconds of overlap. Unread rows never
advance the window; DB errors and MCP omissions return the signed checkpoint at
the start of unread rows, even on the first page. Continuation never renews the
window/expiry. Available evidence survives snapshot replacement independently.

`capture_status:best_effort` always lists event-write failure, unproven retention,
old-job prune/cascade, manual purge and commit delay beyond overlap under
`limitations`. These standing limitations do not create a permanent gap or
change current queue health. Observed DB failure, page backlog, truncation,
expired window/cursor or an interval beyond 24 hours appear under `gaps`; exact
lost counts remain unknown. An empty page never proves lossless zero failures.

## On-demand coverage

`fleet_health.coverage(actor,node_id=None,cursor=None)` /
`get_monitoring_coverage` accepts an exact expected node or signed cursor, never
both. It returns schema 1, one expected node per page, a scope/revision/profile
fence, global roster counts and separate `measurement_coverage.scope:returned_node`.
Unknown/ambiguous nodes are refused before detail reads. Checked custom aliases
are reconciled together and cached; conflicting proofs stay ambiguous. A changed
alias/roster requires refreshing the page.

CPU, memory, root/known data filesystems, inodes, filesystem enumeration,
runner/scheduler and role-bound services retain independent runtime and
CloudWatch slots. Runtime freshness is 20 seconds; CloudWatch uses actual samples
from the previous 30 minutes at CPU300/memory60/disk300-second periods and three
period freshness. Exact namespaces/full dimensions and partial-series flags are
preserved. PartialData remains incomplete even when a retained value is usable.
The node read allows 20 seconds, four discovery pages, twelve CloudWatch operations
and eight collected mount series. Known capped mounts remain named budget gaps.
Every multi-node page is incomplete, including the last. A complete fleet claim
requires every page of one unchanged revision with no gap/denial/omission.

Recipe configuration is declaration, never installation proof.
`agent_installation` remains unknown; missing samples cannot establish an absent
agent. Fresh runtime evidence does not fill a missing continuous stream.
Unknown roles preserve unknown obligations. A scheduler lock never becomes a
per-node detector heartbeat. Nothing installs agents or changes production
monitoring. There is no anonymous endpoint or public projection; any public
status product must use an explicitly approved safe consumer projection.
