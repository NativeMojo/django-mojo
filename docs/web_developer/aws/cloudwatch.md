# AWS CloudWatch Monitoring API

Live AWS infrastructure metrics for EC2, RDS, and ElastiCache — exposed through
two endpoints that mirror the [metrics app](../metrics/metrics.md) exactly.

Slugs in all responses are **friendly names**, not raw AWS IDs:

- **EC2**: the instance's `Name` tag value (e.g. `"web-server-1"`), falling back to the instance ID when no `Name` tag is set.
- **RDS / ElastiCache**: the identifier is already human-readable (e.g. `"prod-postgres"`, `"prod-redis-001"`), so it is used as-is.

Use the `slug` value from the `resources` endpoint when targeting specific instances via the `fetch` endpoint's `slugs` parameter.

**Permission required:** `manage_aws` on all endpoints.

---

## Endpoints

| Method | URL | Description |
|---|---|---|
| GET | `/api/aws/cloudwatch/resources` | List all EC2, RDS, and ElastiCache resources with friendly names |
| GET | `/api/aws/cloudwatch/fetch` | Time-series metric data for one or more instances |

---

## GET /api/aws/cloudwatch/resources

Returns all EC2 instances, RDS DB instances, and ElastiCache clusters visible to
the configured AWS credentials.

Each resource entry includes a `slug` field — the friendly name used in chart
labels and accepted as input by the `fetch` endpoint's `slugs` parameter. Use
`slug` (not the raw `id`) when targeting a specific instance.

### Response

```json
{
  "ec2": [
    {
      "id": "i-0abc1234",
      "slug": "web-server-1",
      "name": "web-server-1",
      "state": "running",
      "instance_type": "t3.medium",
      "private_ip": "10.0.1.5",
      "public_ip": "54.1.2.3"
    }
  ],
  "rds": [
    {
      "id": "prod-postgres",
      "slug": "prod-postgres",
      "engine": "postgres 15.3",
      "status": "available",
      "instance_class": "db.t3.medium",
      "endpoint": "prod-postgres.abc.us-east-1.rds.amazonaws.com:5432"
    }
  ],
  "redis": [
    {
      "id": "prod-redis-001",
      "slug": "prod-redis-001",
      "engine": "redis 7.0.7",
      "status": "available",
      "node_type": "cache.t3.micro",
      "num_nodes": 1
    }
  ],
  "status": true
}
```

---

## GET /api/aws/cloudwatch/fetch

Fetches time-series CloudWatch metric data. The response shape is identical to
the metrics app `fetch` endpoint — the same chart components work with no changes.

When `slugs` is omitted, **all instances** for the given `account` type are
discovered automatically and returned together. Pass `slugs` only when you want
to pin the response to specific instances.

The `slugs` parameter accepts **either** the friendly name **or** the raw AWS ID
— both are resolved to the correct instance internally. Prefer the friendly name
(the `slug` value from the `resources` endpoint) since that is what appears in
chart labels.

### Query Parameters

| Parameter | Required | Default | Description |
|---|---|---|---|
| `account` | yes | — | Resource type: `ec2`, `rds`, or `redis` |
| `category` | yes | — | Metric shortname (see tables below) |
| `slugs` | no | all instances | Comma-separated friendly names or AWS IDs to target |
| `dt_start` | no | 24 hours ago | Start of range (UTC ISO-8601) |
| `dt_end` | no | now | End of range (UTC ISO-8601) |
| `granularity` | no | `hours` | `minutes`, `hours`, or `days` |
| `stat` | no | `avg` | `avg`, `max`, `min`, or `sum` |

### Examples

```
# CPU for all EC2 instances, last 24 hours, hourly buckets
GET /api/aws/cloudwatch/fetch?account=ec2&category=cpu

# Connection count for a specific RDS instance, last 7 days, daily buckets
GET /api/aws/cloudwatch/fetch?account=rds&category=conns&slugs=prod-postgres&granularity=days

# Cache hits for two Redis clusters, last hour, per-minute
GET /api/aws/cloudwatch/fetch?account=redis&category=cache_hits&slugs=prod-redis-001,prod-redis-002&granularity=minutes

# Peak CPU for a named EC2 instance (friendly name from Name tag)
GET /api/aws/cloudwatch/fetch?account=ec2&category=cpu&slugs=web-server-1&stat=max

# Peak CPU across all RDS instances
GET /api/aws/cloudwatch/fetch?account=rds&category=cpu&stat=max
```

### Response — multiple instances (slugs omitted or multiple slugs)

```json
{
  "data": {
    "data": [
      {"slug": "web-server-1", "values": [12.4, 15.1, 9.8]},
      {"slug": "api-server-2", "values": [8.2,  9.1,  7.3]}
    ],
    "periods": ["10:00", "11:00", "12:00"]
  },
  "status": true
}
```

### Response — single instance (one slug)

When exactly one slug is provided the inner `data` is unwrapped to a plain dict,
matching the metrics app single-slug behavior:

```json
{
  "data": {
    "data": {
      "slug": "web-server-1",
      "values": [12.4, 15.1, 9.8]
    },
    "periods": ["10:00", "11:00", "12:00"]
  },
  "status": true
}
```

---

## Categories

Categories are the same regardless of account type where the metric applies.
Passing a category that is not supported for the given account returns a `400`.

### Universal (all account types)

| Category | Description |
|---|---|
| `cpu` | CPU utilization % |
| `net_in` | Bytes/throughput received |
| `net_out` | Bytes/throughput sent |

### EC2 only

| Category | Description |
|---|---|
| `disk_read` | Disk read operations |
| `disk_write` | Disk write operations |
| `status_check` | Status check failures (0 = healthy) |
| `memory` | Memory used % ¹ |
| `disk` | Root filesystem used % ¹ |

¹ Requires the [CloudWatch Agent](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Install-CloudWatch-Agent.html)
installed and running on the instance. The agent pushes these metrics to the `CWAgent`
namespace — instances without the agent will return all-zero values for these categories.
The `disk` category always targets the root filesystem (`path="/"`).

### RDS only

| Category | Description |
|---|---|
| `conns` | Active database connections |
| `free_storage` | Free storage space (bytes) |
| `free_memory` | Freeable memory (bytes) |
| `read_iops` | Read I/O operations per second |
| `write_iops` | Write I/O operations per second |
| `read_latency` | Average read latency (seconds) |
| `write_latency` | Average write latency (seconds) |

### Redis / ElastiCache only

| Category | Description |
|---|---|
| `conns` | Current client connections |
| `cache_memory` | Memory used by cached data (bytes) |
| `cache_hits` | Successful key lookups |
| `cache_misses` | Failed key lookups |
| `replication_lag` | Replica lag behind primary (seconds) |

---

## Granularity and Period Labels

| `granularity` | Bucket size | Label format | Example |
|---|---|---|---|
| `minutes` | 60 s | `HH:MM` | `14:32` |
| `hours` (default) | 3600 s | `HH:MM` | `14:00` |
| `days` | 86400 s | `YYYY-MM-DD` | `2025-06-01` |

Buckets with no CloudWatch data are filled with `0.0`. The `periods` and `values`
arrays are always the same length and span the full requested range.

AWS CloudWatch enforces a minimum period of 60 seconds and may restrict finer
granularities for data older than 15 days. Use `granularity=days` for ranges
longer than a week.

---

## IAM Permissions Required

The AWS user or role configured in your project needs:

```json
{
  "Effect": "Allow",
  "Action": [
    "cloudwatch:GetMetricStatistics",
    "ec2:DescribeInstances",
    "rds:DescribeDBInstances",
    "elasticache:DescribeCacheClusters"
  ],
  "Resource": "*"
}
```

No additional Django settings are needed — the CloudWatch helper reuses
`AWS_KEY`, `AWS_SECRET`, and `AWS_REGION` already configured for SES and S3.

---

## Error Responses

| Status | Meaning |
|---|---|
| `400` | Missing `account` or `category`, unknown `account` value, or `category` not supported for the given `account` type |
| `401` | Not authenticated |
| `403` | Authenticated but missing `manage_aws` permission |
| `500` | AWS API error — check server logs for details |