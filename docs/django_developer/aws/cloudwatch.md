# AWS CloudWatch Monitoring

Reference documentation for backend developers integrating AWS CloudWatch metrics
into a django-mojo project.

Companion REST API reference: [docs/web_developer/aws/cloudwatch.md](../../web_developer/aws/cloudwatch.md)

---

## Overview

The `CloudWatchHelper` (in `mojo/helpers/aws/cloudwatch.py`) wraps boto3 to pull
live time-series metrics from AWS CloudWatch for three resource types:

| account type | CloudWatch namespace | Resources discovered by |
|---|---|---|
| `ec2` | `AWS/EC2` | `ec2:DescribeInstances` |
| `rds` | `AWS/RDS` | `rds:DescribeDBInstances` |
| `redis` | `AWS/ElastiCache` | `elasticache:DescribeCacheClusters` |

The high-level `fetch()` method mirrors the metrics app API exactly — same
`account` / `category` / `slugs` parameters, same `periods` + `data` response
shape. Existing frontend chart components work without modification.

---

## AWS IAM Permissions Required

The IAM user or role referenced by `AWS_KEY` / `AWS_SECRET` must have at minimum:

```json
{
  "Version": "2012-10-17",
  "Statement": [
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
  ]
}
```

No new Django settings are required — `CloudWatchHelper` reads the same
`AWS_KEY`, `AWS_SECRET`, and `AWS_REGION` already used by SES, S3, and other
AWS helpers.

---

## Settings

| Setting | Default | Description |
|---|---|---|
| `AWS_KEY` | — | AWS access key ID |
| `AWS_SECRET` | — | AWS secret access key |
| `AWS_REGION` | `us-east-1` | AWS region for all CloudWatch calls |

---

## Using `CloudWatchHelper` Directly

### Instantiation

```python
from mojo.helpers.aws import CloudWatchHelper

# Uses AWS_KEY / AWS_SECRET / AWS_REGION from settings
cw = CloudWatchHelper()

# Or pass explicit credentials for multi-account scenarios
cw = CloudWatchHelper(access_key="AKIA...", secret_key="...", region="eu-west-1")
```

Boto3 clients are created lazily — no network call happens at instantiation.

---

### High-Level `fetch()`

The primary interface. Mirrors `metrics.fetch()` in signature and response shape.

> **EC2 `memory` and `disk` require the CloudWatch Agent.** These categories use
> the `CWAgent` namespace, which is only populated when the
> [CloudWatch Agent](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Install-CloudWatch-Agent.html)
> is installed and running on the instance. Instances without the agent will
> return all-zero values for these categories.

Slugs in the response are **friendly names**, not raw AWS IDs:

- **EC2**: the instance's `Name` tag value (e.g. `"web-server-1"`), falling back to the instance ID when no `Name` tag is set.
- **RDS / ElastiCache**: the identifier is already human-readable (e.g. `"prod-postgres"`, `"prod-redis-001"`), so it is used as-is.

```python
result = cw.fetch(
    account="ec2",          # resource type: "ec2", "rds", "redis"
    category="cpu",         # metric shortname (see table below)
    # slugs omitted -> all instances discovered automatically
    granularity="hours",    # "minutes", "hours" (default), or "days"
    stat="avg",             # "avg" (default), "max", "min", or "sum"
)
# result = {
#     "periods": ["10:00", "11:00", "12:00"],
#     "data": [
#         {"slug": "web-server-1", "values": [12.4, 15.1, 9.8]},
#         {"slug": "api-server-2", "values": [8.2,  9.1,  7.3]},
#     ]
# }
```

When only one slug is returned (or one slug was explicitly requested), `data` is
a plain dict instead of a list — identical to the metrics app behaviour:

```python
result = cw.fetch(account="ec2", category="cpu", slugs=["web-server-1"])
# result = {
#     "periods": ["10:00", "11:00", "12:00"],
#     "data": {"slug": "web-server-1", "values": [12.4, 15.1, 9.8]}
# }
```

The `slugs` parameter accepts **either** the friendly name **or** the raw AWS ID
— both are resolved to the underlying instance ID before the CloudWatch call is
made. This means you can pass `"web-server-1"` or `"i-0abc1234"` and get the
same result.

Buckets with no CloudWatch data points are filled with `0.0` so `periods` and
`values` are always the same length and cover the full requested range.

---

### `fetch()` Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `account` | str | required | Resource type: `ec2`, `rds`, `redis` |
| `category` | str | required | Metric shortname (see table below) |
| `slugs` | list or str | `None` | Friendly names or AWS IDs. Omit to fetch all instances automatically. |
| `dt_start` | datetime | 24 h ago | Start of range (UTC) |
| `dt_end` | datetime | now (UTC) | End of range (UTC) |
| `granularity` | str | `"hours"` | `"minutes"`, `"hours"`, or `"days"` |
| `stat` | str | `"avg"` | `"avg"`, `"max"`, `"min"`, or `"sum"` |

---

### Category Reference

| category | EC2 | RDS | Redis |
|---|---|---|---|
| `cpu` | CPUUtilization | CPUUtilization | CPUUtilization |
| `net_in` | NetworkIn | NetworkReceiveThroughput | NetworkBytesIn |
| `net_out` | NetworkOut | NetworkTransmitThroughput | NetworkBytesOut |
| `conns` | — | DatabaseConnections | CurrConnections |
| `free_storage` | — | FreeStorageSpace | — |
| `free_memory` | — | FreeableMemory | — |
| `read_iops` | — | ReadIOPS | — |
| `write_iops` | — | WriteIOPS | — |
| `read_latency` | — | ReadLatency | — |
| `write_latency` | — | WriteLatency | — |
| `cache_memory` | — | — | BytesUsedForCache |
| `cache_hits` | — | — | CacheHits |
| `cache_misses` | — | — | CacheMisses |
| `replication_lag` | — | — | ReplicationLag |
| `disk_read` | DiskReadOps | — | — |
| `disk_write` | DiskWriteOps | — | — |
| `status_check` | StatusCheckFailed | — | — |
| `memory`      | mem_used_percent ¹ | — | — |
| `disk`        | disk_used_percent ¹ | — | — |

¹ Requires the CloudWatch Agent installed on the instance. Pushed to the `CWAgent`
namespace, not `AWS/EC2`. Instances without the agent return all-zero values.
The `disk` category targets the root filesystem (`path="/"`).

A `ValueError` is raised when `category` is not valid for the given `account`
type (e.g. `cache_hits` on `ec2`). The REST layer converts this to a `400`.

---

### Granularity and Period Labels

| granularity | period_seconds | Label format | Example |
|---|---|---|---|
| `minutes` | 60 | `HH:MM` | `14:37` |
| `hours` | 3600 | `HH:MM` | `14:00` |
| `days` | 86400 | `YYYY-MM-DD` | `2025-06-01` |

---

### Resource Discovery

`fetch()` calls these automatically when `slugs` is omitted. You can also call
them directly:

```python
# Returns [{id, slug}, ...] — slug is the friendly chart label
# EC2: slug = Name tag, or instance ID if no Name tag is set
# RDS / ElastiCache: slug = id (already human-readable)
resources = cw.list_resource_slugs("ec2")   # or "rds" / "redis"

# Returns [{id, name, state, instance_type, private_ip, public_ip}, ...]
instances = cw.list_ec2_instances()

# Returns [{id, engine, status, instance_class, endpoint}, ...]
dbs = cw.list_rds_instances()

# Returns [{id, engine, status, node_type, num_nodes}, ...]
clusters = cw.list_elasticache_clusters()

# Convenience — returns a plain list of raw AWS ID strings
ids = cw.list_instance_ids("ec2")   # or "rds" / "redis"
```

All three list methods use boto3 paginators and handle large accounts correctly.

---

### Namespace Resolution

Most categories use a fixed namespace per account type (e.g. `AWS/EC2`, `AWS/RDS`).
A small number of categories require a different namespace — `resolve_namespace()`
handles this transparently inside `fetch()`:

```python
from mojo.helpers.aws.cloudwatch import (
    resolve_namespace,
    CATEGORY_NAMESPACE_OVERRIDE,
    CATEGORY_EXTRA_DIMENSIONS,
)

resolve_namespace("ec2", "cpu")     # -> "AWS/EC2"   (default)
resolve_namespace("ec2", "memory")  # -> "CWAgent"   (override)
resolve_namespace("ec2", "disk")    # -> "CWAgent"   (override)

# The override table (for reference):
# CATEGORY_NAMESPACE_OVERRIDE = {
#     ("ec2", "memory"): "CWAgent",
#     ("ec2", "disk"):   "CWAgent",
# }

# Some CWAgent categories require extra fixed dimensions beyond the primary
# instance dimension.  EC2 disk needs path="/" to target the root filesystem.
# CATEGORY_EXTRA_DIMENSIONS = {
#     ("ec2", "disk"): [{"Name": "path", "Value": "/"}],
# }
```

To add support for a new category that lives in a non-default namespace, add an
entry to `CATEGORY_NAMESPACE_OVERRIDE`. If it also requires extra fixed dimensions
(e.g. a filesystem path), add those to `CATEGORY_EXTRA_DIMENSIONS`.

---

### Low-Level `get_metric()`

For namespaces not covered by the three convenience wrappers, call `get_metric`
directly:

```python
data = cw.get_metric(
    namespace="AWS/Lambda",
    metric_name="Invocations",
    dimensions=[{"Name": "FunctionName", "Value": "my-lambda"}],
    dt_start=datetime.datetime(2025, 1, 1),
    dt_end=datetime.datetime(2025, 1, 2),
    period_seconds=3600,
    stat="Sum",
)
# Returns: {periods, values, slug, namespace, dimension}
```

---

## Module-Level Helpers

These are exported from `mojo/helpers/aws/cloudwatch.py` and used internally
by the REST layer.

```python
from mojo.helpers.aws.cloudwatch import resolve_metric, resolve_namespace, granularity_to_seconds, normalize_stat

resolve_metric("rds", "conns")         # -> "DatabaseConnections"
resolve_metric("ec2", "memory")        # -> "mem_used_percent"
resolve_metric("ec2", "disk")          # -> "disk_used_percent"
resolve_metric("ec2", "cache_hits")    # -> raises ValueError
resolve_namespace("ec2", "cpu")        # -> "AWS/EC2"
resolve_namespace("ec2", "memory")     # -> "CWAgent"
resolve_namespace("ec2", "disk")       # -> "CWAgent"
granularity_to_seconds("hours")        # -> 3600
normalize_stat("max")                  # -> "Maximum"
```

---

## REST API

Two endpoints under the `aws` URL prefix, both requiring `manage_aws`:

| Method | URL | Description |
|---|---|---|
| `GET` | `/api/aws/cloudwatch/resources` | List EC2, RDS, and ElastiCache resources with friendly names |
| `GET` | `/api/aws/cloudwatch/fetch` | Time-series metric data (mirrors metrics/fetch) |

The `resources` endpoint now includes a `slug` field on every entry — the same
friendly name that will appear in chart labels. Use this `slug` value (not the
raw `id`) when targeting a specific instance via `fetch`'s `slugs` parameter.

See the [web developer reference](../../web_developer/aws/cloudwatch.md) for full
request/response documentation.

---

## Module Layout

```
mojo/helpers/aws/cloudwatch.py       # CloudWatchHelper, mapping tables, module helpers
mojo/apps/aws/rest/cloudwatch.py     # REST endpoints (wired via rest/__init__.py)
```

No models or migrations — all data comes live from CloudWatch.

---

## Testing

Tests live in `tests/test_aws/cloudwatch.py`.

Permission and parameter validation tests always run (no AWS credentials needed).
Live metric tests check for `AWS_KEY` in the live server settings and call
`raise TestitSkip(...)` when credentials are absent — the same pattern used by
email/phone verification gate tests.

Run in your Django project environment:

```
python manage.py testit test_aws.cloudwatch
```
