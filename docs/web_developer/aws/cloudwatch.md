# AWS CloudWatch Monitoring API

Live AWS infrastructure metrics for EC2, RDS, and ElastiCache are exposed
through two read endpoints that mirror the
[metrics app](../metrics/metrics.md) exactly. A separate public endpoint
receives signed CloudWatch alarm deliveries from AWS SNS.

Slugs in all responses are **friendly names**, not raw AWS IDs:

- **EC2**: the instance's `Name` tag value (e.g. `"web-server-1"`), falling back to the instance ID when no `Name` tag is set.
- **RDS / ElastiCache**: the identifier is already human-readable (e.g. `"prod-postgres"`, `"prod-redis-001"`), so it is used as-is.

Use the `slug` value from the `resources` endpoint when targeting specific instances via the `fetch` endpoint's `slugs` parameter.

**Permission required for the two GET endpoints:** `manage_aws`, checked as a
**global** `User.permissions` grant only (`@md.requires_global_perms`); a
`manage_aws` permission held only at the group/member level does not authorize
metric reads. The SNS alarm POST is public and instead requires a valid AWS SNS
signature from an exactly allowlisted topic.

Admin System Setup is the preferred configuration surface. It creates or
explicitly adopts the installation-owned operations topic, persists the exact
ARN through a protected superuser-only setting, creates the HTTPS subscription
and owned alarms, then waits for a signed delivery probe before reporting the
section ready. The probe is evidence-only and never opens an Event, Incident,
Ticket, or normal alarm dispatch.

An existing same-name untagged topic is not adopted silently. System Setup
returns an exact `topic_arn` enum and requires
`adopt_existing_topic: true`; partial or conflicting ownership tags fail
closed. It preserves unrelated topic-policy statements and owns only the
CloudWatch publish statement restricted to the selected AWS account.

---

## Endpoints

| Method | URL | Description |
|---|---|---|
| GET | `/api/aws/cloudwatch/resources` | List all EC2, RDS, and ElastiCache resources with friendly names |
| GET | `/api/aws/cloudwatch/fetch` | Time-series metric data for one or more instances |
| POST | `/api/aws/cloudwatch/sns/alarm` | Public AWS SNS delivery endpoint; signature and exact-topic allowlist required |

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
| `dr_start` | no | 24 hours ago | Start of range as Unix timestamp (preferred) |
| `dr_end` | no | now | End of range as Unix timestamp (preferred) |
| `dt_start` | no | 24 hours ago | Alias for `dr_start`, accepts ISO-8601 datetime |
| `dt_end` | no | now | Alias for `dr_end`, accepts ISO-8601 datetime |
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

### Response

The response shape is identical to the metrics app — `data` is a dict of
`{slug: [values]}` and `labels` is the shared time axis.

```json
{
  "data": {
    "data": {
      "web-server-1": [12.4, 15.1, 9.8],
      "api-server-2": [8.2,  9.1,  7.3]
    },
    "labels": ["10:00", "11:00", "12:00"]
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

## Granularity and Labels

| `granularity` | Bucket size | Label format | Example |
|---|---|---|---|
| `minutes` | 60 s | `HH:MM` | `14:32` |
| `hours` (default) | 3600 s | `HH:MM` | `14:00` |
| `days` | 86400 s | `YYYY-MM-DD` | `2025-06-01` |

Buckets with no CloudWatch data are filled with `0.0`. The `labels` array and each
`data` values array are always the same length and span the full requested range.

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

No additional Django settings are needed for metric reads — the CloudWatch
helper reuses `AWS_KEY`, `AWS_SECRET`, and `AWS_REGION` already configured for
SES and S3. Alarm delivery requires the deployment to set
`AWS_CLOUDWATCH_ALARM_TOPIC_ARNS` to the exact accepted SNS topic ARN(s).

---

## Metric endpoint error responses

| Status | Meaning |
|---|---|
| `400` | Missing `account` or `category`, unknown `account` value, or `category` not supported for the given `account` type |
| `401` | Not authenticated |
| `403` | Authenticated but missing `manage_aws` permission |
| `500` | AWS API error — check server logs for details |

---

## CloudWatch alarm events

The SNS alarm endpoint is AWS-facing infrastructure, not a browser API. Clients
should not POST alarm JSON themselves: SNS signs the outer envelope and the
server rejects unsigned messages or topics absent from the deployment's exact
allowlist.

**Authentication/permission:** no User session or Django permission is used.
Authorization consists of the SNS signature, strict AWS certificate URL, and
an exact `TopicArn` match in `AWS_CLOUDWATCH_ALARM_TOPIC_ARNS`. A missing or
empty allowlist denies all delivery. Deployments should use a dedicated topic
whose AWS policy restricts `sns:Publish` to the intended CloudWatch alarm
sources/accounts; an SNS signature alone does not prove that CloudWatch authored
the inner message.

### Request format

SNS posts its standard JSON envelope as the raw request body (commonly with
`Content-Type: text/plain`). A notification has this shape; `Message` is itself
a JSON-encoded CloudWatch alarm state-change document:

```json
{
  "Type": "Notification",
  "MessageId": "11111111-2222-3333-4444-555555555555",
  "TopicArn": "arn:aws:sns:us-east-1:123456789012:operations",
  "Message": "{\"AlarmName\":\"api-errors\",\"AlarmArn\":\"arn:aws:cloudwatch:us-east-1:123456789012:alarm:api-errors\",\"AWSAccountId\":\"123456789012\",\"NewStateValue\":\"ALARM\",\"OldStateValue\":\"OK\",\"NewStateReason\":\"Threshold crossed\",\"StateChangeTime\":\"2026-08-04T03:00:00.000+0000\",\"Trigger\":{\"Namespace\":\"AWS/ApplicationELB\",\"MetricName\":\"HTTPCode_Target_5XX_Count\",\"Dimensions\":[]}}",
  "Timestamp": "2026-08-04T03:00:00.000Z",
  "SignatureVersion": "2",
  "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-example.pem",
  "Signature": "base64-signature"
}
```

Clients should never synthesize this envelope. Configure an SNS subscription
and let AWS sign and deliver it. The endpoint also accepts signed SNS
`SubscriptionConfirmation` envelopes and follows only a strict, non-redirecting
AWS confirmation URL.

Accepted state changes appear in the incident feed with:

- `scope`: `aws:cloudwatch`
- `category`: `aws:cloudwatch:alarm`
- bounded metadata for alarm/account/region/state/reason/time and metric
  namespace/name/dimensions

Alarm ingestion is opt-in at the policy layer. Without a CloudWatch-specific
RuleSet, the Event is retained but no Incident or Ticket is created. With a
matching rule, `ALARM` can open an incident and run configured handlers such as
`ticket://?board=<id>`.

System Setup's owned delivery probe is the exception: the persisted transition
has `is_delivery_probe: true` and is evidence-only, so it never creates an
Event, Incident, Ticket, or rule dispatch. Apply migration
`aws.0012_cloudwatchalarmtransition_is_delivery_probe` before using the
monitoring setup section. The transition's REST graph exposes the boolean so an
Admin client can distinguish proof rows from operational alarm history.

`INSUFFICIENT_DATA` records uncertainty without closing current work. `OK`
resolves the matching Incident and adds recovery history/notes. An existing
Ticket or Maestro item stays open for the human workflow to close; a later
`ALARM` starts a new incident occurrence. SNS replays do not duplicate these
records.

### Response format

An accepted notification returns the durable transition identifier and whether
the delivery matched an existing SNS/logical transition:

```json
{
  "status": true,
  "data": {
    "duplicate": false,
    "transition_id": 123
  }
}
```

A confirmed subscription returns:

```json
{
  "status": true,
  "data": {
    "confirmed": true,
    "status_code": 200
  }
}
```

| Status | Meaning |
|---|---|
| `200` | Notification accepted (including an idempotent duplicate), or subscription confirmed |
| `400` | Malformed SNS/CloudWatch payload or unsupported SNS message type |
| `403` | Generic signature, certificate-URL, header-consistency, or topic authorization failure |
| `405` | Method other than POST |
| `503` | Certificate fetch, confirmation, processing, or durable handler dispatch should be retried by SNS |
