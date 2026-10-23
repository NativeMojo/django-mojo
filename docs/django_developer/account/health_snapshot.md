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
If source identity is unresolved, the cron hook returns
`{"status":"unknown","reason":"source_identity_unresolved"}` without
initializing identity or dispatching a producer, so later scheduled maintenance
can continue. Other snapshot errors still propagate.

## Identity, publication and storage

The wire contract is `django-mojo.health_snapshot`, `schema_version:2`,
`profile_version:2`. Source identity is `{installation_id, environment_id}`:
read the existing frozen `MOJO_INSTALLATION_UUID`/slug pair and file-only
`MOJO_ENVIRONMENT`, or the explicit non-provisioned fallback
`HEALTH_SNAPSHOT_ENVIRONMENT_ID`. Conflicting/missing values are
`source_identity_unresolved`; reads never initialize identity. `source_id` is
SHA-256 of canonical JSON `[installation_id,environment_id]`.

Components have source-scoped `kind:resource_id` identities. Kinds are `api`,
`database`, `redis`, `jobman`, `fleet`, `monitoring`, and `service`. Primary and declared
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

Jobman's `scheduler_detector` slot consumes only the independently cached
three-minute OS-cron result, including its actual observation time and detector
identity. `scheduler_available` and `expected_runners_available` use that source
as their causal basis. Missing, failed or expired detector evidence stays
Unknown; snapshot generation and cache reads never create another detector
sample. `scheduler_lock` remains optional, noncausal installation metadata.
An enrolled snapshot source collects quietly with the existing health-incident
flag off; enrollment does not enable notifications.

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

The existing per-node `source:request_service` slot in `fleet:installation`
uses the same checked service projection as node diagnostics and monitoring
coverage. A fresh signed local socket response must bind the physical host,
current admitted runner start, exact logical node, selected revision and
root-controlled request role/profile. Activation receipts alone remain progress
evidence. A validated boolean `healthy:false` is a known availability failure
and keeps the fleet down despite unrelated coverage gaps. Missing, stale,
mismatched or conflicting proof remains Unknown; only explicit proved role
exclusion is not applicable and it adds no availability pass. The slot's
420-second expiry starts at actual proof completion; collection, publication
and polling cannot renew it. Jobs grant removal removes the slot and its causal
condition. The on-demand fleet `report()` retains provider/runner presence scope
and never gathers request proofs or caches health in the identity roster.

## Trusted project service checks

The default-off, file-only `HEALTH_SNAPSHOT_PROJECT_SERVICES` setting extends
this same scheduled producer. It accepts exactly `module` and `checks`:

```python
HEALTH_SNAPSHOT_PROJECT_SERVICES = {
    "module": "myproject.services.health_checks",
    "checks": [
        {"id": "mcp", "impact": "availability"},
        {"id": "sites-dns", "impact": "operational"},
    ],
}
```

There are at most eight unique lowercase slug IDs, each at most 64 characters;
`project-checks` is reserved for the invalid-declaration Unknown sentinel.
Each declaration has exactly `id` and `impact`. No HTTP parameter, database
Setting or callback result selects executable code. An absent setting preserves
the original configuration hash and publication behavior.

The trusted synchronous module exports these two functions:

- `configuration(checks, deadline, budget)` returns a dict with exactly every
  declared ID, mapping each to its actual non-secret JSON configuration. This
  function is strictly offline: no network, credential resolution or node calls.
  It participates in producer acquisition, publication fencing and cached reads.
  Values must be finite JSON, at most 4 KiB per check and 32 KiB total. The module,
  roster, impacts and actual configuration determine the frozen fingerprint and
  existing publication `config_revision`.
- `collect(check, deadline, budget)` returns exactly
  `{outcome, observed_at, reason}`. Outcome is `pass|fail|unknown`. Pass/fail need
  an actual timezone-aware ISO observation; Unknown requires null observation.
  `check` is frozen `{id, impact, config}`; configuration receives frozen
  `{id, impact}` declarations. Nested mappings are read-only and lists become
  tuples. Both functions receive the actual absolute monotonic deadline and
  shared `CycleBudget`.

Callback `reason` is null or one fixed code: `configuration_unavailable`,
`credential_unavailable`, `authentication_failed`, `protocol_invalid`,
`deadline_exceeded`, `provider_unavailable`, `unsupported`, `cleanup_failed`,
`dns_mismatch`, `dns_no_answer`. Raw provider bodies, exception messages,
credentials and callback-supplied provenance never enter evidence. Callbacks own
their service predicate and bounded I/O, and must include cleanup in their
deadline. Collection runs in the existing four-worker pool, charges one of eight
`project_services` allowances and receives at most five seconds or the remaining
acquisition time. Late results are Unknown. Trusted Python must cooperate with
the bound; there is no detached timeout thread or forced cancellation claim.
When project checks are configured, native, Redis, HTTP and project calls finish
before fleet acquisition starts in the same pool. Fleet receives the remaining
original acquisition time; exhaustion stays Unknown. Without project checks,
the original scheduling order is unchanged.

The framework constructs `service:<id>`, with service scope, matching
`resource_id`/`service_id`, platform identity grant, one required platform `check`
slot (`source:project_service`) and one `service_check` condition. Slot identity
contains `module`, `check_id`, `impact` and `project_fingerprint`; actual config
is not returned. A pass/fail is a boolean true/false observation with the existing
sample ID, last-success and 420-second freshness rules. Operational-only checks
can degrade health but never establish availability. Missing/import/invalid/late
checks retain Unknown obligations. Prior facts survive failed collection only
when their project fingerprint and declared identity still match.

When current declarations or cached service components exist, the three-second
read path compares the current offline fingerprint and exact identities with
the cached slots. Changed, removed, unavailable or late configuration refuses
the whole publication as Unknown. It never calls `collect`, updates observation
times, or rewrites the facts/digest of one generation. The publication-time
configuration race fence still applies. Schema/profile remain 2; the closed
component ceiling is 37 (29 built-in plus eight service checks), with the same
512 KiB, 2,048-slot and 1,024-condition limits. Consumers lacking service-kind
support must reject unsupported snapshots safely.

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
libpq connect/operations, 750 ms SQL/lock limits and closed local resources.
Standalone configuration and grouped database/job reads each use one autocommit
SELECT. Generation reservation and publication retain their transactions and
row locks, including an injected configuration recheck on the same locked
cursor. Core collection plus a two-query project check and publication costs
14 of the unchanged 16 SQL operations; five are reserved until finalization.
Genuine exhaustion remains `not_collected_budget`. Unsupported engines and
connection modes remain unknown. HTTP
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
