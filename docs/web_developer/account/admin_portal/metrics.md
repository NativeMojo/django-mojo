# Admin Metrics API

The Admin portal's **Metrics** page charts CloudWatch time series for the EC2,
RDS, and ElastiCache resources an installation can see, so an operator does not
need an AWS console login to answer "is this box busy?".

Route: `#/metrics`. It lives inside the Platform feature but is its own sidebar
entry.

## Permission

Both endpoints require the global `manage_aws` permission. The bootstrap payload
exposes it twice:

- `capabilities.manage_aws` — the raw grant.
- `features.platform.capabilities.metrics` — the feature lane. **Gate UI on
  this one.** A future change to what Metrics needs moves in one place.

Without the grant the sidebar entry is absent and the route renders a permission
notice.

## Endpoints

### `GET /api/aws/cloudwatch/resources`

Lists everything chartable. Each entry carries a `slug` — the friendly name used
in chart labels and accepted by `fetch`. For EC2 that is the `Name` tag (falling
back to the instance id); for RDS and ElastiCache the identifier is already
human-readable.

```json
{
  "status": true,
  "ec2":   [{"id": "i-0a1b", "name": "mojo-api-a", "state": "running",
             "instance_type": "m6i.large", "slug": "mojo-api-a"}],
  "rds":   [{"id": "mojo-prod-postgres", "engine": "postgres 16.3",
             "status": "available", "slug": "mojo-prod-postgres"}],
  "redis": [{"id": "mojo-prod-redis-001", "engine": "redis 7.1",
             "status": "available", "slug": "mojo-prod-redis-001"}],
  "degraded": {},
  "available": true,
  "reason": null
}
```

The three services are read independently:

- `degraded` maps failed service names (`ec2` / `rds` / `redis`) to a reason
  code. The other services still list normally.
- `available` is `false` only when **every** service failed.
- `reason` names the single cause when `available` is `false`, otherwise `null`.

### `GET /api/aws/cloudwatch/fetch`

| Parameter | Meaning |
|---|---|
| `account` | `ec2`, `rds`, or `redis` (required) |
| `category` | metric shortname (required) — see the matrix below |
| `slugs` | comma-separated slugs; omit for every resource of that type |
| `dt_start` / `dt_end` | ISO datetimes (`dr_start` / `dr_end` accept Unix seconds) |
| `granularity` | `minutes`, `hours` (default), or `days` |
| `stat` | `avg` (default), `max`, `min`, or `sum` |

```json
{"status": true,
 "data": {"data": {"mojo-api-a": [12.5, 14.0]},
          "labels": ["14:00", "15:00"],
          "available": true, "reason": null}}
```

**`available` and `reason` are inside `data`, not beside it.** The portal client
returns `payload.data ?? payload`, so a top-level flag would never reach the
page. A degraded fetch returns an empty `data` and `labels` with
`available: false`.

Invalid parameters are still a `400` — an unknown `account`, an unknown
`category`, or a `category` the account type does not support.

## Degraded responses

Both endpoints return **200** when AWS itself is the problem. Reason codes and
the copy the Admin page shows:

| `reason` | Copy |
|---|---|
| `credentials_unavailable` | No AWS credentials are configured for this installation. |
| `denied` | The configured AWS identity was refused — check its IAM policy or whether its credentials are current. |
| `network_unavailable` | AWS did not answer. Try again shortly. |
| `service_error` | AWS returned an error; details are in the server log. |

Raw provider messages are never returned. A genuine server-side bug is still a
`500`.

## Controls

| Control | Values |
|---|---|
| Resource type | EC2 instances · RDS databases · ElastiCache clusters |
| Metric | the categories supported for that resource type (see below) |
| Time range | 1h · 6h · 24h (default) · 48h · 7d · 30d |
| Granularity | repopulated per range |
| Statistic | Average (default) · Maximum · Minimum · Sum |

Granularity offered per range: 1h and 6h → per minute; 24h and 48h → hourly;
7d → hourly or daily (daily by default); 30d → daily or hourly (daily by
default).

Rows in the resource list carry a checkbox for the selected resource type. With
nothing checked the request omits `slugs` and every resource of that type is
charted.

### Supported metric categories

| Category | EC2 | RDS | ElastiCache |
|---|---|---|---|
| `cpu` | ✅ | ✅ | ✅ |
| `net_in` / `net_out` | ✅ | ✅ | ✅ |
| `conns` | — | ✅ | ✅ |
| `free_storage`, `free_memory`, `read_iops`, `write_iops`, `read_latency`, `write_latency` | — | ✅ | — |
| `cache_memory`, `cache_hits`, `cache_misses`, `replication_lag` | — | — | ✅ |
| `disk_read`, `disk_write`, `status_check` | ✅ | — | — |
| `memory`, `disk` | ✅ (CloudWatch Agent) | — | — |

`memory` and `disk` read the `CWAgent` namespace, so they are flat zero unless
the CloudWatch Agent is installed on the instance. The page labels them
accordingly. Unsupported pairs are never offered — requesting one is a 400.

## Deep links

Metrics uses only canonical route-state keys:

| Key | Meaning |
|---|---|
| `tab` | resource type (`ec2` / `rds` / `redis`) |
| `category` | metric shortname |
| `focus` | slug to preselect |

Example: `#/metrics?category=cpu&focus=mojo-api-a&tab=ec2`. Time range,
granularity, and statistic are page-local and are not serialized.
