# Cloud Domain Tools — Result Shapes

What the `cloud` domain's twenty-four tools return, which block renders each, and
what a client does after a mutating one is approved.

The domain is **on demand**: the model loads it with
`load_tools(domain="cloud")`. Which tools a given operator is offered depends
on their grants — see [Who sees what](#who-sees-what).

---

## Read tools

Every read returns a plain JSON object. All of them are already **bounded and
redacted** server-side (24 KB ceiling, 200-char strings, and a per-tool node
and width budget), so a client can render them without further trimming. The
documented per-tool limits are real: `list_cloud_resources` returns up to 100
rows per service and `fetch_cloud_metrics` up to 60 buckets across up to 10
slugs. A container that was cut carries `"truncated": true`.

### `get_platform_health`

```json
{
  "observed_at": "2026-08-21T23:00:00+00:00",
  "availability": {"state": "ok", "message": "Everything is running", "down": []},
  "attention": "2 incidents need review — nothing is down.",
  "sources": {
    "load_balancer": {"status": "healthy", "reason": null,
                      "observed_at": "...", "data": {"registered": 2, "healthy": 2}},
    "database": {"status": "healthy", "reason": null, "observed_at": "...", "data": {}}
  }
}
```

`status` is one of `healthy` · `unconfigured` · `stale` · `degraded` ·
`unhealthy` · `unknown` · `permission_denied`. Renders naturally as a status
grid; `availability.state` is the headline (`ok` / `down` / `unknown`).

### `get_platform_overview`

```json
{"requested": ["deployments"], "sections": {"deployments": {"status": "healthy", "data": {...}}}}
```

`sections` is **required** (max 4). Any section the caller may not read comes
back as `{"status": "unauthorized"}` rather than being omitted — render it as a
denied row, not as missing data. The `deployments` section's `items` are the
deployment projection below.

### Deployment projection

Used by `get_platform_overview(sections=["deployments"])` and returned by all
four deploy-ish mutations:

```json
{"id": "…", "sha": "…", "framework_version": "1.15.15", "status": "failed",
 "source": "github", "actor": "…", "retry_of": null, "created": "…",
 "started": "…", "finished": "…", "duration_seconds": 42.5,
 "node_summary": {"expected": 3, "proven": 2, "reported": 0, "failed": 1,
                  "dispatched": 0, "other": 0},
 "current_commits": ["…"], "desired_commit": "…"}
```

`node_evidence`, `transitions`, `diagnosis` and `frozen_roster` are **never**
present — they carry deploy stderr. `node_summary` is the bounded answer.

### `get_fleet_capacity`

```json
{"mode": "managed", "region": "us-east-1", "generated_at": "…",
 "nodes": [{"id": "i-…", "name": "mojo-api-a", "healthy": true,
            "instance_type": "m6i.large", "zone": "us-east-1a",
            "is_self": false, "primary": true, "added_by_capacity": false}],
 "databases": [{"identifier": "…", "kind": "aurora", "writer": "…",
                "readers": [], "instance_class": "db.r6g.large"}],
 "caches": [{"identifier": "…", "node_type": "cache.t4g.medium",
             "replica_count": 1, "min_replicas": 1, "cluster_enabled": false,
             "resize_impact": "rolling", "blocked_reason": null}],
 "egress": {"enabled": false, "available": true, "attached_count": 0,
            "reserved_count": 0, "pending_node_count": 2, "to_allocate": 2,
            "monthly_usd_per_address": 3.6},
 "actions": {"add_node": {"offered": true, "blocked_reason": null},
             "drain_node": {"offered": false, "blocked_reason": "last_healthy_target"}},
 "warnings": ["addresses"], "sizes": {...}}
```

**`actions` is the control surface.** A control the server does not offer must
not be a button — render `offered: false` disabled, with `blocked_reason` as
the explanation. Elastic IP allocation ids are deliberately absent.

### The rest

| Tool | Returns |
|---|---|
| `get_advanced_inventory` | `{"sections": {"hosting": …, "aws_inventory": …, "network_security": …}}` |
| `get_framework_status` | `installed`, `latest`, `checked_at`, `source`, `update_available`, `pin`, `can_update`, `blocked_reason` |
| `get_capacity_operation_status` | one operation's or batch's recorded progress (`state`, `phase`, `message`, per-step rows, `stalled`) |
| `get_managed_upgrades` | `findings[]` (`kind`, `resource`, `engine`, `current_version`, `target_version`, `deadline`, `days_remaining`, `note`, `status`, `settled`), `available`, `warnings` |
| `get_upgrade_status` | one resource's live progress — `upgraded` is the only success signal |
| `get_setup_readiness` | `overall`, `summary`, `sections[].checks[]`, plus `local_probe` |
| `get_setup_operation` | `{"active": false}` **or** `id`, `mode`, `status`, `cursor`, `steps[]`, `current_step`, `choices`, `finished_at`, last 20 `log` entries |
| `get_version_drift` | `status` (`recorded` / `no_recent_scan`), `mode`, `recorded_at`, `region`, `findings[]` |
| `list_cloud_resources` | `ec2` / `rds` / `redis` arrays of `{id, slug, name, type, state}`, plus `degraded`, `available`, `reason` |
| `fetch_cloud_metrics` | `labels[]`, `series{slug: {values[], min, max, avg, truncated}}`, `available`, `reason` |

`get_setup_readiness`'s `local_probe` reports `{"status": "unavailable",
"setting": "SYSTEM_SETUP_LOCAL_API_URL", "message": …}` when nothing is
configured and the probe fell back to port 80 — treat local-API check rows in
that report as unproven, not failing.

---

## Mutating tools and the approval card

The seven mutating tools **never execute on the model's call.** Each produces a
server-owned approval card; only the operator can approve it. The card shape,
the `POST`/`GET /api/assistant/action` contract, the WebSocket events and the
failure mapping are all in [approvals.md](approvals.md) — nothing about them is
special here. Three things are:

1. **All seven set `requires_fresh_auth: true`**, so they can only be resolved
   over REST after a step-up. The WebSocket path answers
   `{"type": "assistant_error", "code": "reauth_required", "action_id": …}`;
   re-submit the same `action_id` over REST.
2. **`apply_capacity_change` and `apply_capacity_plan` also set
   `requires_superuser: true`.** Only an active literal superuser is even
   offered them.
3. **`preview.details` is the plan**, and it is worth rendering in full — for
   the capacity plan it is the server's own ordered, worded, priced step list
   (`steps[]` with `description`, `warnings`, `monthly_delta_usd`, plus
   `total_monthly_delta_usd`, `estimate_complete`, `order_note`). The model
   cannot alter it: execution re-plans from the approved steps.

### Precondition failures are normal

The card binds a `revision` — the deployment's status, the offered release, the
upgrade target, or the fleet fingerprint. If it moves before the operator
answers, resolution returns `409 precondition_failed` and the card stays
`pending`. That is the intended common case: reload the underlying read and ask
again. It is not an error to report as a failure.

### After approval: what to poll

| Tool | Result carries | Poll |
|---|---|---|
| `retry_platform_deployment` | `queued`, `deployment` | `get_platform_overview(sections=["deployments"])` |
| `verify_platform_deployment` | `deployment` | same |
| `converge_platform_deployment` | `deployment` | same |
| `apply_framework_update` | `requested`, `version`, `cleared_pin`, `deployment` | same |
| `apply_managed_upgrade` | `requested`, `kind`, `resource`, `from_version`, `target_version`, `apply_immediately`, `operation` | `get_upgrade_status` |
| `apply_capacity_change` | `operation`, `phase`, `state`, `message` | `get_capacity_operation_status(operation=…)` |
| `apply_capacity_plan` | `batch`, `state`, `message`, `steps[]` | `get_capacity_operation_status(batch=…)` |

Every result also carries a `reconciliation` sentence. **Nothing is retried
automatically** — show it.

---

## Failure results

A mutating tool that ran and failed comes back as `200` with `state: "failed"`
and a `failure_code`. Read tools return the same `{"error", "error_code"}` shape
inline. The codes this domain uses:

| `error_code` | Meaning |
|---|---|
| `invalid_request` | A bounded input was missing, unknown, or over a cap |
| `permission_denied` | The caller is not an active superuser (System Setup reads) |
| `interactive_session_required` | The session is an API key or otherwise not an interactive Admin tab |
| `unknown_resource` | The deployment or setup operation id is not on record |
| `coordination_unavailable` | Deploy coordination could not be reached; nothing was started |
| `update_refused` | The framework update service refused (e.g. no converged commit) |
| `fleet_changed` | The fleet moved between proposal and execution; nothing was applied |
| *service codes* | Passed through verbatim from capacity/maintenance — `plan_stale`, `plan_not_found`, `plan_already_applied`, `batch_dispatch_failed`, `report_degraded`, `upgrade_not_offered`, `upgrade_in_progress`, `cache_unavailable`, `operation_not_found`, `batch_not_found`, `invalid_request` |

### Provider reason codes

Reads that touch AWS degrade instead of failing: `available: false` plus a
`reason` of `credentials_unavailable`, `denied`, `network_unavailable` or
`service_error`. Raw provider text never reaches the client.

---

## Who sees what

| Grant | Sees |
|---|---|
| `manage_aws` only | the capacity, upgrade, drift and CloudWatch **reads** |
| `manage_aws`, not a superuser | **not** `apply_capacity_change` / `apply_capacity_plan` — absent from every listing |
| `manage_aws` without `manage_platform`/`admin` | **not** `apply_managed_upgrade` |
| `admin` without being a superuser | **not** `get_setup_readiness` / `get_setup_operation` |
| external infrastructure mode | **not** `apply_framework_update`, `apply_managed_upgrade`, `apply_capacity_change`, `apply_capacity_plan` — the deploy trio and all reads remain |

## System Setup repair is not here

Every System Setup mutation is bound to the browser Origin that started it, so
it cannot be driven from chat without forging that binding. The Assistant can
audit readiness and report honest progress on an operation a human started in
the Admin; starting, advancing, choosing and cancelling stay in the Admin UI.

See also: [approvals.md](approvals.md) · [blocks.md](blocks.md) ·
[../account/system_setup.md](../account/system_setup.md)

### `get_fleet_health` and `get_node_health`

Both are read-only MCP/Assistant tools. Inputs are
`{"limit":20,"cursor":"..."}` (limit 1–20; cursor optional) and
`{"node_id":"stable-id","hours":24}` (hours 1–168). Booleans do not satisfy
integer inputs. Select `node_id` from the fleet result; hosts, regions, channels,
functions and mount paths are never accepted as tool inputs.

```json
{
  "schema_version": 1, "generated_at": "2026-10-20T12:00:00+00:00",
  "roster_revision": "...", "collection_status": "partial",
  "health": "unknown", "health_scope": "provider_and_runner_presence",
  "coverage": {"expected_total": 3, "observed_total": 2,
    "responding_total": 2, "returned": 3, "missing_total": 1,
    "ambiguous_total": 0, "complete": false, "has_more": false,
    "next_cursor": null, "reasons": ["expected_nodes_missing"]},
  "sources": {}, "nodes": [], "truncated": false
}
```

`collection_status` is `complete|partial|unavailable|unconfigured`; `health` is
`healthy|unhealthy|unknown`. They answer different questions. Expected missing
nodes remain rows. Null `expected_total` means the complete denominator cannot
be read or established. An AWS-only reader cannot establish configuration/edge
expectations. Presence alone does not establish application health, and every
continuation page (including the last) has incomplete fleet-wide coverage.
Follow `next_cursor`; refresh after `roster_changed`. Do not reinterpret
`unconfigured`, denied, missing, stale or truncated evidence as healthy.

A node contains stable `node_id`, separate hostname/instance/runner aliases,
role, proved request-service applicability, expectation provenance, identity
status, lifecycle, reasons and observation times. A node read adds `current`,
`services` and `trends`. Current families carry `status`, `reason`, `source`,
`observed_at`, `unit` and numbers/nulls. Network counters are cumulative since
boot. Historical series add exact namespace/dimensions, UTC start/end,
`period_seconds`, `aggregation`, `sample_count`, `last_sample_at`, null-preserving
`buckets` and real-sample `minimum|maximum|average`. Never display a 24-hour
average as current utilization. A missing CWAgent series is `no_series`, not 0%.

Each source enforces existing global grants: platform readers see expectation,
heartbeat and installation scheduler summaries; `manage_aws` reads provider
inventory/trends and provision declarations; jobs readers may obtain checked
host diagnostics. Other sections explicitly say `permission_denied`. Group-only
grants do not authorize these global sources. Missing runner membership,
incompatible checked protocol, draining, function unavailable, timeout, identity
mismatch and collector failure stay distinct unavailable/unknown evidence.
Worker/Sites service applicability needs proved role evidence; an installation
scheduler lock does not prove a scheduler on every node.

The fleet read has an eight-second collection budget and no per-node control or
CloudWatch fan-out. One-node diagnostics use twenty seconds, checked replies at
most five seconds. Identity-only aliases expire after 120 seconds and can be
resolved across workers; expired observed-only IDs return `unknown_resource` and
require a fresh fleet read. No diagnostic result cache is introduced here.
Bounded cluster/credential transports that cannot honor collection deadlines
return explicit unsupported/unavailable evidence. A fresh checked custom-id
alias can return `roster_changed`, requiring a fresh fleet page.

Output is bounded to 24 KiB. Omitted buckets/mounts/series carry counts and make
coverage incomplete. An oversized CloudWatch dimension set is omitted whole
(`dimensions:null`, `dimensions_omitted:true`, `omitted_series_count:1`); a
partial dimension list must never be treated as an exact series identity.

Collection deadlines cover bounded DNS, numeric connection establishment and
complete socket transfers. Slow replies cannot renew a collection deadline.
Unsupported proxy/OCSP/resolver transports remain unknown/unavailable evidence;
clients must not turn such omissions into healthy service status.

`roster_scope` is the selected environment/declaration fence used for short-lived
identity aliases. An environment or expectation change invalidates old aliases.
Fresh competing host claims remain `identity_ambiguous` even after one host
replies again; clients must refresh rather than guess a logical alias.

## Cached health and monitoring coverage

`get_health_snapshot(evidence_cursor=None)` is read-only and cache-only, with
current global platform entry permission, independent AWS/jobs projection and a
24KiB MCP ceiling. It returns `django-mojo.health_snapshot` schema2, collected
every300 seconds for60-second external polling. Its immutable publication digest
and configuration hash survive permission projections. Each causal slot retains
actual sample time and expiry; repeated reads do not renew health. MCP omissions
return the frozen checkpoint before unread failure evidence and a REST reference.

`get_monitoring_coverage(node_id=None,cursor=None)` is a read-only schema1
on-demand drilldown over the shared bounded readers. It accepts an expected
node or signed cursor, one node per page. Global roster counts stay separate
from local measurement coverage, and every multi-node page stays incomplete.
Runtime, continuous streams, recipe declaration and installation proof remain
independent; partial/capped/missing/denied requirements stay visible.

See [the health snapshot contract](../account/health_snapshot.md) for both tools,
source permissions, three-second reads, durable failure cursors, fixed deadlines
and the public-data boundary. Existing fleet/node and platform tools keep their
existing public signatures and source permissions.
