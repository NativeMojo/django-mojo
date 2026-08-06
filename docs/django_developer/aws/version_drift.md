# AWS managed-service version drift

A daily, read-only inventory of the AWS managed services this deployment runs,
answering one question a console tour cannot answer cheaply: **is any managed
database or cache on a major version AWS is about to stop supporting, and
when?**

The scanner lives in
[`mojo/apps/aws/services/version_drift.py`](../../../mojo/apps/aws/services/version_drift.py);
the schedule is `mojo/apps/aws/cronjobs.py` and the work is
`mojo/apps/aws/asyncjobs.py`.

## What is inventoried

| Service | Unit | Upgrade signal | End-of-life date |
|---|---|---|---|
| RDS (non-Aurora) | DB instance | `ValidUpgradeTarget` entries with `IsMajorVersionUpgrade` | `DescribeDBMajorEngineVersions` standard support |
| Aurora | DB **cluster** | same | same |
| ElastiCache | replication group (or standalone cluster) | a higher major in `DescribeCacheEngineVersions` **for the same engine** | none — see below |

Aurora member instances carry a `DBClusterIdentifier` and are skipped: the
cluster is the upgrade unit, so counting the members too would report the same
upgrade three times.

## What is deliberately NOT done

- **No autonomous apply.** Nothing here ever calls `ModifyDBCluster`,
  `ModifyDBInstance` or `ModifyReplicationGroup`. A major version upgrade is a
  planned, tested change with downtime; the scanner's whole job is to make sure
  a human decides it on time rather than late.
- **No ElastiCache end-of-life dates.** The ElastiCache service model exposes
  no lifecycle or deprecation member anywhere. Those findings carry
  `deadline: null` and say so in their note. A hardcoded EOL table would be a
  guess with a silent decay date, so there isn't one.
- **No minor-version reporting.** RDS auto-minor-version-upgrade already
  handles that, so a version whose only upgrade targets are minor is not a
  finding at all.
- **No AMI / EC2 image leg**, and no "newest AMI" resolution. Deferred.

## Standard support vs extended support

The single most important detail in this feature. `DescribeDBMajorEngineVersions`
returns `SupportedEngineLifecycles[]`, and **Aurora PostgreSQL returns two
entries**:

| `LifecycleSupportName` | Meaning |
|---|---|
| `open-source-rds-standard-support` | AWS maintains the major version. **This is the deadline.** |
| `open-source-rds-extended-support` | Paid extension, roughly three years later. |

The scanner selects the standard entry **by name**. A `max()` over the two
would report the deadline about three years late — a silent wrong answer on the
one output this feature exists to produce. The extended date is carried
separately as `extended_deadline`, purely as context. When no standard entry
exists, `deadline` is `null`; it never falls back to extended.

## The event contract

One event per run, at most, filed through
`mojo.apps.incident.reporter.report_event` (not `record_event` — only
`report_event` calls `Event.publish()`, which is what runs the rules engine).

| Field | Value |
|---|---|
| `category` | `system:health:aws_versions` |
| `scope` | `aws:versions` |
| `hostname` | the deployment slug (`AWS_MONITORING_NAME`, else the `BASE_URL` host) |
| `metadata.findings` | the finding rows |
| `metadata.warnings` | APIs that were denied, by exact IAM action |

Each finding row:

```json
{
  "kind": "rds-cluster",
  "resource_id": "prod-aurora",
  "engine": "aurora-postgresql",
  "current_version": "13.12",
  "available_major": "16.1",
  "deadline": "2027-02-28T00:00:00+00:00",
  "extended_deadline": "2030-02-28T00:00:00+00:00",
  "days_remaining": 60,
  "note": "...",
  "release_notes_url": "https://docs.aws.amazon.com/..."
}
```

### Two categories, on purpose

The **event** category is `system:health:aws_versions`, so the finding appears
on `/api/incident/health/summary` next to the other subsystem rows. The
**RuleSet** category is `aws:versions`, matched through the event's `scope`
(`Event.publish()` looks up by scope first, then by category) — exactly how the
CloudWatch alarm policy is wired.

They differ because `mojo/apps/incident/cronjobs.py` bootstraps the default
health rules only when they are absent, and a RuleSet sitting anywhere in the
`system:health:` namespace used to satisfy that guard. Keeping the RuleSet on
`aws:versions` means it can never suppress `Health - Runner Down`. (The guard
itself now matches the exact rule-set names, so both halves are safe.)

### Level scale

| Level | Meaning |
|---|---|
| 1 | Everything is current. **No event is filed.** |
| 4 | An API was denied, so the inventory is incomplete. |
| 5 | A major upgrade is available with no near deadline. |
| 8 | A published standard-support deadline falls inside `AWS_VERSION_DRIFT_DEADLINE_DAYS`. |
| 10 | The deadline has already passed. |

Level 1 files nothing deliberately. The catch-all RuleSet matches `Level >= 1`
and has no handler, and `Event.publish()` creates an Incident whenever a
RuleSet matched — so a level-1 "all clear" would manufacture a permanent
Incident in the operator's queue on every single run. Liveness is already
covered by the cron heartbeats `aws-check --section cron` reads.

## The opt-in RuleSet and the board item

```python
from mojo.apps.incident.models import RuleSet
RuleSet.ensure_aws_version_rules()
```

or `python manage.py aws-check --apply --section rules`.

| | |
|---|---|
| Name | `Health - AWS Version Drift` |
| Category | `aws:versions` |
| Rule | `level >= 5` — a level-1 event can never open a ticket |
| Handler | `notify://perm@manage_security,ticket://?priority=8&category=aws-version-drift&maestro=1` |

It is **not** part of `RuleSet.ensure_default_rules()`; like the CloudWatch
policy it stays opt-in so non-AWS deployments never get it.

`maestro=1` makes `TicketHandler` push the Ticket to the Maestro integration's
default board, so the upgrade lands as a tracked work item and the Ticket
itself is the approval artifact — approving the upgrade is closing the item.

> **`MAESTRO_API_KEY` is a precondition for the board item.**
> `TicketHandler._push_to_maestro` swallows `maestro_sync.get_config()`
> failures. With the key unset you get a perfectly good local Ticket and a
> *silently absent* board item. If you expect the item and it is not there,
> check the key first.

### Escalation on later runs

`TicketHandler`'s reuse branch adds only a "Recurring incident #N" note; it
touches neither `description` nor `priority`, and `Ticket`'s Maestro sync fires
only from REST saves. So the asyncjob updates the open drift ticket itself —
rewriting the description with the current findings, raising `priority` to the
event level once it passes 8, and enqueueing the Maestro sync directly.
Without that the board item would stay frozen at whatever the first run said
while the deadline approached, which is the opposite of the point.

## Schedule

`@schedule(minutes="0", hours="7")` — **daily**, gated on
`AWS_VERSION_DRIFT_ENABLED`. `mojo.apps.aws` is in `INSTALLED_APPS`, so
`cron.load_app_cron()` discovers the module with no registration step.

Daily rather than monthly on purpose: `cron.run_scheduled_functions` has no
per-function `try` (one sibling raising aborts the batch), and
`find_scheduled_functions` is pure wall-clock matching with no last-run state
and no catch-up. A monthly single-minute window would be the most
miss-sensitive schedule in the codebase guarding the slowest-moving deadline —
one bad tick costs 30 days. The scan is a handful of read-only API calls and
the job is idempotent.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `AWS_VERSION_DRIFT_ENABLED` | `False` | Arms the daily cron. File-only (`settings.get_static`). |
| `AWS_VERSION_DRIFT_DEADLINE_DAYS` | `180` | A standard-support deadline this close (or closer) raises the event to level 8. File-only. |

`AWS_REGION`, `AWS_KEY`, `AWS_SECRET` and `AWS_MONITORING_NAME` are shared with
the rest of the AWS app — see [credentials.md](credentials.md).

## Credentials and IAM

Two failure modes, treated very differently:

- **No credentials at all** (`NoCredentialsError`, `PartialCredentialsError`,
  an unreachable endpoint) → `status: "unavailable"`. The job logs and files
  nothing. This is the normal state on every dev machine and in the test suite,
  and the cron is off by default anyway.
- **Credentials present, one API denied** (`ClientError`) → the exact missing
  IAM action is recorded in `warnings`, the scan continues with partial
  findings, and a **level-4 event is still filed**. Silence here would make a
  correctly-locked-down deployment indistinguishable from "nothing is out of
  date" — precisely the failure this feature exists to prevent.

Least-privilege read actions:

```
rds:DescribeDBClusters
rds:DescribeDBInstances
rds:DescribeDBEngineVersions
rds:DescribeDBMajorEngineVersions
elasticache:DescribeCacheClusters
elasticache:DescribeCacheEngineVersions
```

`rds:DescribeDBMajorEngineVersions` is also guarded with `hasattr`, so an older
botocore degrades to `deadline: null` instead of raising.

## Running it by hand

```bash
python manage.py aws-check --check --section versions
```

The `versions` section is opt-in (never part of a default run) and can only
report `pass`, `warn` or `pending` — never `fail` — so a first run before the
IAM grant is in place cannot exit 1 in CI. See
[aws_check.md](aws_check.md).
