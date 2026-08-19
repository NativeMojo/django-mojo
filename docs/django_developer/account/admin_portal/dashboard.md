# Admin Dashboard integration contract

The built-in Admin's primary navigation is, in this order: Dashboard,
Deployments, Domains & DNS, People, Activity, Metrics, Maintenance, Settings,
and — in its own **System** group at the bottom — System Setup. Domains & DNS
appears only with DNS read/manage authority and owns ongoing domain and
public-record work. Deployments is the merged lane for the API service, the
django-mojo framework, and every web app.

**There is no Platform page and no Advanced diagnostics page.** Both were
grids of raw evidence reached by clicking through a page whose other job was
linking elsewhere. Their evidence is now on the Dashboard rows below — one
sentence per row, with the exact collector payload behind a per-row
**Details** drill-in — and System Setup is a first-class sidebar entry.

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
| `jobs` | Jobs | `view_platform`, `manage_platform`, `admin` |
| `sanity` | (annotates Public API) | `view_platform`, `manage_platform`, `admin` |
| `incidents`, `tickets` | Incidents, Tickets | `view_security`, `manage_security`, `security`, `admin` |

The endpoint itself first requires Admin source access (`view_admin`,
`manage_users`, `manage_settings`, or `admin`); those source grants do not bypass
the per-row checks above. `fleet`, `webapps`, and `security` are still not in
this payload — a fleet roster and a detection posture are drill-in material,
fetched on demand through `?sections=` (see "Drill-ins" below).

## Availability is not attention

`AVAILABILITY_SOURCES` is exactly `load_balancer, compute, database, cache,
certificates, public_api` — unchanged by the two sources added above. Neither
`jobs` nor `sanity` can ever redden the headline, and neither collector may
return `unhealthy` at all. A source lands on `availability.down` **only** when
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
- **Jobs** — `_dashboard_jobs` wraps the Platform `_jobs` collector and adds
  `failed_recent`: jobs whose status is `failed` and whose `modified` falls
  inside `JOBS_FAILURE_WINDOW` (**1 hour**; both columns are indexed). Only
  that window colours the row — the all-time `jobs.failed` ledger is permanently
  large on a long-lived queue and is drill-in material. A stalled scheduler is
  `degraded`/`scheduler_inactive` and recent failures are
  `degraded`/`recent_failures`; the wrapper deliberately downgrades the
  `unhealthy` that `_jobs` itself reports for a stalled scheduler, because a
  backed-up queue is not proof that customers cannot use the system.
- **Sanity** — `_dashboard_sanity` wraps `_sanity` and narrows it twice. The
  `local request` check is **dropped** unless `local_target_source ==
  "configured_static"`: an inferred local target that does not answer is
  evidence about the inference, not about this node (the same suppression
  `operatorChecks` applies in the Setup report). And a failing check is
  `degraded`, never `unhealthy` — these are cheap local liveness probes.
  The collector passes a bounded Redis client into `sanity.run` through
  `check_redis`'s additive `redis_client` option (the seam mirrors
  `check_migrations`'s `migration_executor_cls`), so the checks cannot inherit
  the process-wide 60-second socket timeout. `_sanity()` called bare — which is
  what `platform_overview` still does — injects nothing and behaves exactly as
  before.

## Denials

Two different denials render the same muted "Restricted" row:

- The operator's role cannot read the source — status `permission_denied`.
- The platform's AWS identity lacks an IAM grant — status `unknown` with
  `reason_detail.iam_action` naming the exact action to add. A denied
  `DescribeTargetHealth` still returns the balancer facts already read.

A section whose every source is denied collapses to one Restricted row.

## Drill-ins

`assets/features/dashboard/inspectors.js` owns every row's **Details** link.
The affordance rule is one line: Details takes the row's `action` slot, unless
that slot already holds a cross-page link (Certificates, Review, the
`#/maintenance` upgrade link), in which case it rides in `detailNode`. No row
lost a destination when the drill-ins landed.

Each drill-in renders an observed-at line, a `<dl>` of plain-words facts, and a
`<details class="dash-technical">` disclosure holding
`JSON.stringify(source.data, null, 2)` — the exact collector payload, nothing
summarized away. All of it is built with `h()`; the whole dashboard package is
free of `innerHTML`.

Two drill-ins read more than the Dashboard payload, and only when opened:

| Drill-in | Extra read | Gate |
|---|---|---|
| EC2 → edge runner roster | `GET /api/account/admin/platform?sections=fleet` | `features.platform.capabilities.view` |
| Incidents → secure posture | `GET /api/account/admin/platform?sections=security` | `features.platform.capabilities.security` |

The caller does not offer a drill-in whose capability is absent, and the
inspector guards again itself. A failed fetch replaces only its own block with
`errorState` — the evidence already proven above it stays on screen. Nothing
here widens authority: `?sections=` narrows *work*, and each named section
still runs its own permission tuple.

The Public API row also carries the `sanity` verdict, in words. `SANITY_COPY`
maps each check name to a sentence (`migrations` → "database migrations are not
applied"); the row shows the first failure plus ` · +N more`, and **never** an
"N of M checks passing" count — a count tells an operator to go and count.
A version mismatch is only reported when sanity is clean or absent.

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

`_section_map`'s pool ceiling is `min(12, len(permitted))`, so both of today's
rosters — the Dashboard's ten platform-tier sources and Platform's ten sections
— run in one wave. Each `_collect` response stays individually bounded, and
every provider call keeps its own SDK/RPC timeout.

Sidebar entries may declare a numeric `order`; `navigationFor` sorts by it and
the sort is stable, so an entry without one keeps its descriptor position.
System Setup uses `order: 100` and `section: 'System'` to sit below daily work,
and renders an amber `.nav-badge` dot when `bootstrap.capabilities`
(and `features.platform.capabilities`) carry `setup_attention` — true only for
a superuser on an installation with no `BASE_URL` yet.

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
`--dashboard-state healthy|degraded|down|jobs_stalled|sanity_failed|denied|unknown`.
`down` is Elasticache unhealthy with 121 open incidents (the headline names
Elasticache, the backlog stays muted); `degraded` is engine drift plus a
published framework release with availability still green; `jobs_stalled` is a
stopped scheduler with four failures in the window; `sanity_failed` fails the
`migrations` check. The last two keep the headline green on purpose. Every
launch resets provider rows and preview events.
