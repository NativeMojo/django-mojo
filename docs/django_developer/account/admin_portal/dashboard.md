# Admin Dashboard integration contract

The built-in Admin has seven primary navigation items, in this order: Dashboard,
Web Apps, Domains & DNS, People, Activity, Platform, and Settings. Domains & DNS appears
only with DNS read/manage authority and owns ongoing domain and public-record
work. System Setup and deployments are destinations inside Platform; Advanced
is one expert-diagnostics destination rather than a raw-resource directory.

Dashboard answers one question — **is anything down right now, and if not, what
needs attention?** `GET /api/account/admin/dashboard` returns
`schema_version: 2`: a top-level `availability` verdict, a separate `attention`
line, and the per-source `sources` matrix each row is drawn from. Each source
checks its own permission before its collector runs.

## Sources and authorities

| Source | Row | Global read authority |
|---|---|---|
| `load_balancer` | Load balancer | `view_platform`, `manage_platform`, `admin` |
| `compute` | EC2 | `view_platform`, `manage_platform`, `admin` |
| `database` | RDS | `view_platform`, `manage_platform`, `admin` |
| `cache` | Elasticache | `view_platform`, `manage_platform`, `admin` |
| `certificates` | SSL certs | `view_platform`, `manage_platform`, `admin` |
| `public_api` | Public API | `view_platform`, `manage_platform`, `admin` |
| `framework` | django-mojo | `view_platform`, `manage_platform`, `admin` |
| `last_deployment` | Deployment | `view_platform`, `manage_platform`, `admin` |
| `incidents`, `tickets` | Incidents, Tickets | `view_security`, `manage_security`, `security`, `admin` |

The endpoint itself first requires Admin source access (`view_admin`,
`manage_users`, `manage_settings`, or `admin`); those source grants do not bypass
the per-row checks above. `fleet`, `webapps`, and `security` were dropped from
Dashboard in schema 2 — they remain on Platform, which is where a fleet roster
and a detection posture belong.

## Availability is not attention

`AVAILABILITY_SOURCES` is exactly `load_balancer, compute, database, cache,
certificates, public_api`. A source lands on `availability.down` **only** when
its status is `unhealthy` — a proven failure. Nothing else reddens the page:
not a denial, not a stale read, not an amber upgrade, and never an incident
backlog.

- `state: "down"` — at least one availability source is `unhealthy`.
  `message` names it: `"Elasticache is down"`, or
  `"3 things are down: RDS, Elasticache, Public API"`.
- `state: "ok"` — no failures and at least one source reporting healthy.
  `message` is `"Everything is running"`.
- `state: "unknown"` — nothing is reporting.
  `message` is `"Status unknown — no source is reporting."`

`attention.message` is the separate muted sub-line built from the open incident
count: `"121 incidents need review — nothing is down."`, and without the tail
when something *is* down. It never changes the availability colour.

`overall` and `observable_sources` are **removed**. A single max-severity value
could not distinguish "a database is unreachable" from "an upgrade is
published", which is the whole reason the page was rebuilt.

## Per-row fact sources

- **Load balancer** — `mojo.helpers.aws.elbv2.LoadBalancerHelper.frontend()`
  behind a 60-second `django.core.cache` entry (`mojo:admin:dashboard:elbv2`).
  Elastic IPs come from the balancer's own
  `AvailabilityZones[].LoadBalancerAddresses[].AllocationId` — never
  `describe_addresses`. `unhealthy` requires a group with at least one
  registered target and zero healthy, or a serving group behind a balancer
  reporting no address at all; an empty group is `unconfigured`.
- **EC2** — the same cached `frontend()`, scoped to the balancer's registered
  instance targets, with names from one `describe_instances`. With no balancer
  it falls back to a running-instance count that is **never** `unhealthy`: a
  raw instance count proves nothing about whether traffic is served.
- **RDS** — `connection.get_database_version()` plus a vendor display name.
  Reachability decides health; every extra fact degrades to `None` on its own.
  Drift enrichment reads the newest `system:health:aws_versions` Event within
  `VERSION_DRIFT_MAX_AGE` (3 days) and is suppressed when the engine no longer
  reports the finding's `current_version`.
- **Elasticache** — a bounded `ping()` for health, then a plain `.info()` for
  `redis_version` and `used_memory_human`. Section-less INFO is deliberate:
  on `RedisCluster` redis-py routes it to the default node instead of fanning
  out to every node.
- **SSL certs** — zero managed certificates is `unconfigured` ("not managed
  here"), never a failure. Any `failed`/`revoked` row is `unhealthy`.
- **Public API** — the existing SSRF-safe `/api/version` probe, plus this
  node's own `settings.VERSION` as `node_version` so the page can say "up to
  date" or name the mismatch.
- **django-mojo** — `edge.services.framework_version.status()`. Pin-aware:
  `update_available` is true only for an unpinned fleet behind PyPI, because a
  pinned or held fleet would refuse to install the newer version anyway.
- **Deployment** — one `PlatformDeployment` projected to
  `{id, sha, status, created, finished, actor}`. Node evidence is not projected
  at all, so the deploy stderr tail is unreachable from this endpoint for every
  role rather than gated per role.

## Denials

Two different denials render the same muted "Restricted" row:

- The operator's role cannot read the source — status `permission_denied`.
- The platform's AWS identity lacks an IAM grant — status `unknown` with
  `reason_detail.iam_action` naming the exact action to add. A denied
  `DescribeTargetHealth` still returns the balancer facts already read.

A section whose every source is denied collapses to one Restricted row.

## Refresh

`?refresh=1` bypasses the 60-second provider cache and the PyPI version cache.
Every collector stays individually bounded, so a refresh cannot cost more than
an ordinary read. A daily `edge` cron job (`warm_framework_version`) keeps the
PyPI answer warm so the first operator of the day does not pay for it.

## Shared row components

The layout lives in `assets/components/rows.js` and `assets/components/rows.css`,
not in the Dashboard feature package: `rowSection`, `statusRow` (with
`valueNode` / `detailNode` / `action` extension points), `statusHeadline`, and
`rowLink`. Both files are declared in the root `manifest.json`, and the
stylesheet is linked from `index.html` — the manifest loader raises on a
declared-but-missing asset and `asset_path` 404s an undeclared one, so both
registrations are required.

Cross-feature hashes use `assets/components/routes.js`. Use `routeHref()` or
`activityHref()`; do not concatenate a second query vocabulary in a feature
package. The RDS-drift and framework-upgrade links point at `#/maintenance` and
are rendered **only** when that route exists in `featureDescriptors`, so the
link appears once the maintenance feature ships rather than sending an operator
to a silent fallback.

Ignored, resolved, and closed incidents are terminal. One shared predicate
excludes them from the security roster and every Platform/Dashboard open count.

Secret-bearing request and response bodies are classified before views run.
This includes People password and API-key actions, WebApp deployment-key
linkage, DNS credential linking, registrar quote/purchase confirmation, and
certificate material. Authorized reveal responses remain deliberately usable,
but request logging stores only a fixed server-owned marker.

For deterministic visual review, `bin/admin_preview` supports
`--dashboard-state healthy|degraded|down|denied|unknown`. `down` is Elasticache
unhealthy with 121 open incidents (the headline names Elasticache, the backlog
stays muted); `degraded` is engine drift plus a published framework release with
availability still green. Every launch resets provider rows and preview events.
