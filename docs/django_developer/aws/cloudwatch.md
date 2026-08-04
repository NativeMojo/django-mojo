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

Metric reads require no new Django settings: `CloudWatchHelper` reads the same
`AWS_KEY`, `AWS_SECRET`, and `AWS_REGION` already used by SES, S3, and other
AWS helpers. SNS alarm ingestion is configured separately with the exact-topic
allowlist below.

---

## Settings

| Setting | Default | Description |
|---|---|---|
| `AWS_KEY` | — | AWS access key ID |
| `AWS_SECRET` | — | AWS secret access key |
| `AWS_REGION` | `us-east-1` | AWS region for all CloudWatch calls |
| `AWS_CLOUDWATCH_ALARM_TOPIC_ARNS` | `[]` | Static exact SNS topic ARN allowlist for alarm ingestion; empty denies all topics |

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

## Metric REST API

The two metric endpoints under the `aws` URL prefix require `manage_aws` — gated
with `@md.requires_global_perms`, so the grant must be on the caller's
**global** `User.permissions` (no group/member fallback; see
[Global vs Group-Scoped Permission Checks](../core/permissions.md#global-vs-group-scoped-permission-checks)):

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

## SNS alarm ingestion

For deployment-wide audit and create-missing setup, use
[`python manage.py aws-check`](aws_check.md). It enforces the required two-phase
topic ARN allowlist before creating the HTTPS subscription or alarms.

django-mojo can receive CloudWatch alarm state changes through SNS at:

```text
POST /api/aws/cloudwatch/sns/alarm
```

The endpoint is public because SNS cannot authenticate as a django-mojo User,
but it is not anonymous in practice. Every envelope must have a valid AWS SNS
signature and its `TopicArn` must exactly match a static deployment allowlist:

```python
AWS_CLOUDWATCH_ALARM_TOPIC_ARNS = [
    "arn:aws:sns:us-east-1:123456789012:operations",
]
```

An empty or missing list denies every topic. The SES `EmailDomain` topic fields
are deliberately unrelated, and the alarm endpoint never honors
`SNS_VALIDATION_BYPASS_DEBUG`. Subscription confirmation is accepted only when
the signed confirmation URL is HTTPS on the matching AWS SNS service, carries
the signed topic/token, does not redirect, and returns 2xx.

`AWS_CLOUDWATCH_ALARM_TOPIC_ARNS` is read as a static setting, so change it in
deployment configuration and restart application processes when updating the
allowlist. SNS envelopes are limited to 300 KiB.

### Incident policy and lifecycle

Alarm events use `scope="aws:cloudwatch"` and
`category="aws:cloudwatch:alarm"`. The global `*` RuleSet is intentionally not
consulted: receiving alarms is safe to enable before choosing an escalation
policy. Add an explicit RuleSet to create incidents or tickets:

```python
from mojo.apps.incident.models import BundleBy, RuleSet

RuleSet.objects.create(
    category="aws:cloudwatch",
    name="Production CloudWatch alarms",
    bundle_by=BundleBy.MODEL_NAME_AND_ID,
    bundle_minutes=None,
    handler="ticket://?priority=8&category=operations&board=3",
)
```

`MODEL_NAME_AND_ID` groups one active alarm occurrence. `OK` resolves that
machine incident, records recovery on its tickets and synced Maestro item, and
clears the occurrence; a later `ALARM` opens a new occurrence. Tickets and
Maestro items remain human-owned and are not force-closed. `INSUFFICIENT_DATA`
is recorded on the active incident without resolving it.

SNS and logical-transition identities are stored durably. Replays resume any
incomplete handler dispatch with deterministic Job keys but do not create a
second Event, Incident, Ticket, or Maestro item. Older out-of-order transitions
remain audit events and cannot regress the alarm's current state.

Persisted metadata is bounded to the alarm identity, account/region, states,
reason/time, alarm type, and metric namespace/name/dimensions. Raw SNS bodies,
alarm actions/descriptions, log query text, and unexpected credential-like
fields are not stored.

### Durable data model

`CloudWatchAlarm` stores the SHA-256 identity key, exact alarm ARN, current
state/time, and pointers to the active Incident and opening transition.
`CloudWatchAlarmTransition` stores the topic/message identity, old/new states,
state-change time, linked Event/Incident, and durable handler-dispatch status.
Unique constraints cover both SNS message identity and logical transition
identity.

Both models are read-only through their generated REST surfaces
(`CAN_CREATE`, `CAN_UPDATE`, and `CAN_DELETE` are false). Their `RestMeta`
view permissions require `manage_aws` or `security`; callers cannot mutate
lifecycle rows through the API.

### AWS setup and verification

1. Create a dedicated SNS topic and place its exact ARN in
   `AWS_CLOUDWATCH_ALARM_TOPIC_ARNS`. Its topic policy should grant
   `sns:Publish` only to the intended CloudWatch alarm sources/accounts; do not
   reuse a topic that application principals can publish to.
2. Subscribe the HTTPS endpoint above and wait for the signed confirmation to
   succeed.
3. Configure the CloudWatch alarm action to publish to that topic.
4. Add an explicit CloudWatch RuleSet if the signal should become an incident,
   ticket, or Maestro item. Register Maestro first with
   `python manage.py register_maestro` when using `ticket://?board=<id>`.
5. On a disposable/test alarm, verify both directions through AWS rather than
   forging an SNS signature:

```bash
aws cloudwatch set-alarm-state \
  --alarm-name django-mojo-ingress-test \
  --state-value ALARM \
  --state-reason "django-mojo synthetic ingress test"

aws cloudwatch set-alarm-state \
  --alarm-name django-mojo-ingress-test \
  --state-value OK \
  --state-reason "django-mojo synthetic recovery test"
```

## Module Layout

```
mojo/helpers/aws/cloudwatch.py       # CloudWatchHelper, mapping tables, module helpers
mojo/apps/aws/rest/cloudwatch.py     # REST endpoints (wired via rest/__init__.py)
mojo/apps/aws/rest/sns.py            # signed SNS/CloudWatch receiver
mojo/apps/aws/services/sns.py        # SNS verification and confirmation
mojo/apps/aws/services/cloudwatch_alarms.py  # normalization and lifecycle
```

Metric reads remain live-only. Alarm ingestion stores internal
`CloudWatchAlarm` and `CloudWatchAlarmTransition` lifecycle rows.

---

## Testing

Tests live in `tests/test_aws/cloudwatch.py` and
`tests/test_aws/alarm_ingress.py`.

Permission and parameter validation tests always run (no AWS credentials needed).
Live metric tests check for `AWS_KEY` in the live server settings and call
`raise TestitSkip(...)` when credentials are absent — the same pattern used by
email/phone verification gate tests.

Run in your Django project environment:

```
python manage.py testit test_aws.cloudwatch
```
