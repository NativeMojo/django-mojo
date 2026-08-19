# Admin Dashboard API

The packaged Admin uses seven primary destinations: Dashboard, Deployments,
Domains & DNS, People, Activity, Platform, and Settings. Domains & DNS is permission-gated by
DNS read/manage authority because it is an ongoing application control, not a
setup-only surface. Deployments is the merged lane for the API service, the
django-mojo framework, and every web app; literal-superuser System Setup lives
under Platform; Advanced is one expert-diagnostics destination rather than a
resource list.

`GET /api/account/admin/dashboard` requires the same global source-access grant
as the built-in Admin (`view_admin`, `manage_users`, `manage_settings`, or `admin`). It then checks
each source independently before collecting it and returns:

```json
{
  "schema_version": 2,
  "observed_at": "2026-08-10T18:00:00+00:00",
  "availability": {
    "state": "down",
    "message": "Elasticache is down",
    "down": ["Elasticache"]
  },
  "attention": {"message": "121 incidents need review."},
  "sources": {
    "load_balancer": {"status": "healthy", "observed_at": "...", "data": {}},
    "cache": {"status": "unhealthy", "reason": "cache_unreachable", "data": {}},
    "compute": {
      "status": "unknown",
      "reason": "provider_denied",
      "reason_detail": {"iam_action": "ec2:DescribeInstances"},
      "data": {}
    },
    "incidents": {"status": "degraded", "data": {"open": 121, "oldest_age_days": 6}}
  }
}
```

Accept `?refresh=1` as the page's refresh control: it bypasses the server's
60-second provider cache and its cached PyPI version lookup. Every collector
remains individually bounded, so a refresh costs no more than an ordinary read.

## Read the verdict, not the maximum

`availability` is the only thing that may be rendered red. A source contributes
to `availability.down` **only** when its status is `unhealthy`. Denials, stale
reads, amber upgrades, and open incidents never do.

| `availability.state` | Meaning | `message` |
|---|---|---|
| `ok` | Nothing has failed and something is reporting | `Everything is running` |
| `down` | At least one availability source is `unhealthy` | `Elasticache is down` / `3 things are down: …` |
| `unknown` | No source is reporting | `Status unknown — no source is reporting.` |

The availability sources are `load_balancer`, `compute`, `database`, `cache`,
`certificates`, and `public_api`. `framework`, `last_deployment`, `incidents`,
and `tickets` are shown as rows but never colour the verdict.

`attention.message` is a separate muted line derived from the open incident
count. Render it under the headline; do not merge it into the availability
sentence, and do not let a nonzero count imply an outage.

**`overall` and `observable_sources` no longer exist.** They collapsed
"a database is unreachable" and "an upgrade is published" into one value. Read
`availability.state` instead, and read each row's own `status` for its colour.

## Source statuses

Statuses are semantic and must not be collapsed: `permission_denied` means the
collector was not run; `unknown` means no authoritative answer returned;
`unconfigured` is observed absence of configuration; `stale` is old evidence;
`degraded` needs attention; `unhealthy` is a proven failure. Dashboard does not
invoke the System Setup readiness API.

| Source | Read authority |
|---|---|
| `load_balancer`, `compute`, `database`, `cache`, `certificates`, `public_api`, `framework`, `last_deployment` | `view_platform`, `manage_platform`, `admin` |
| `incidents`, `tickets` | `view_security`, `manage_security`, `security`, `admin` |

Two denials look alike to a client and should render the same muted
"Restricted" row: `status: "permission_denied"` (the operator's role), and
`status: "unknown"` with `reason_detail.iam_action` (the platform's AWS identity
lacks that grant — show the action so an operator knows what to add). A section
whose every source is denied should collapse to a single Restricted row.

## Notable row payloads

- `load_balancer.data` — `registered`, `healthy`, `balancer.elastic_ips`, and
  `elastic_ip_missing` (an NLB address with no allocation id; worth saying, not
  an outage).
- `compute.data` — `total`, `up`, `instances[].name`, and `source`
  (`target_group` when scoped to the balancer, `ec2` for the fallback count).
  The `ec2` fallback is never `unhealthy`.
- `database.data` — `engine`, `version`, and `drift` (`available_major`,
  `deadline`, `note`) when a managed-engine upgrade is published and still
  relevant. Render drift as amber, not red.
- `cache.data` — `engine`, `version`, `memory_used`.
- `certificates.data` — `total`, `failing`, `soonest_renew`. `total: 0` arrives
  as `unconfigured`: show "not managed here", never a warning.
- `public_api.data` — `probe.version` (what the public origin served) and
  `node_version` (what this node would serve). Equal means up to date; a
  mismatch is amber; only a failed probe is red.
- `framework.data` — `installed`, `latest`, `update_available`, `pin.mode`
  (`latest` / `pinned` / `hold`). Offer an upgrade only when
  `update_available` is true; a pinned fleet still reports `latest` and must be
  shown as informational.
- `last_deployment.data.items[0]` — `{id, sha, status, created, finished,
  actor}` only. Node evidence and the deploy stderr tail are not exposed here
  for any role; use the Platform deployments endpoint for diagnostics.
- `incidents.data` / `tickets.data` — `open`, `oldest_created`,
  `oldest_age_days`. Hide the Tickets row entirely at zero.

Packaged cross-links use one canonical hash state for `subject_type`,
`subject_id`, `subject_model`, `inspector`, Activity filters, bounded `focus`,
and bounded `return`. Unknown keys are rejected by Activity rather than
broadening a filtered query. Missing Public API configuration opens Setup on
`django.base_url`, the incident row opens the Activity incidents tab, and the
deployment row opens Platform deployments. Upgrade links point at
`#/maintenance` and must be rendered only when that route is registered.
Ignored, resolved, and closed incidents never contribute to the open count.

Secret reveal endpoints are exceptions by exact route and response. All later
status/detail/list/aggregate responses remain non-reveal, and request/response
logging records only fixed sensitivity markers for password, API-key, WebApp
deployment-key, DNS credential, registrar confirmation, and certificate
material routes.
