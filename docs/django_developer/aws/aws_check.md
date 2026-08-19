# AWS deployment readiness (`aws-check`)

`python manage.py aws-check` audits the AWS and operational wiring required by
django-mojo. It is audit-first: a normal non-interactive invocation never
mutates AWS or Django state. Apply mode creates confirmed missing resources and
repairs missing direct-upload CORS on the system FileManager bucket. Other
existing resources that differ from defaults are reported and preserved.

## Admin System Setup convergence

The built-in Admin **System Setup** page registers four stable sections from
`mojo.apps.aws.services.aws_setup`: `aws_identity`, `aws_s3`, `aws_email`, and
`aws_monitoring`. It reuses AWS Check inventory and alarm-profile logic but
runs mutations as durable, repeatable setup operations; it never shells out to
this command.

`aws_identity` is read-only. The three mutable sections use these typed late
choices:

| Section | Choice and convergence contract |
|---|---|
| `aws_s3` | Exact `bucket` enum from complete safe-candidate discovery plus `adopt_existing: true`. Discovery proves the caller account, canonical owner/ACL, non-public policy status, fail-closed policy shape, region, website absence, and non-tenant ownership tags. Adoption keeps all four Block Public Access flags enabled, preserves objects and policies, merges tags and wildcard direct-upload CORS without deleting unrelated rules, and creates or repairs the private system-default FileManager. |
| `aws_email` | Exact verified SES `domain` enum plus a valid `sender` on that domain. Setup imports or updates the local `EmailDomain`, makes that outbound Mailbox the sole system default, and installs only missing shipped templates. Existing customized templates are never overwritten. |
| `aws_monitoring` | Usually no choice. An exact same-name untagged legacy topic produces `topic_arn` plus `adopt_existing_topic: true`; partial/conflicting tags or an unsafe publish policy fail closed before adoption. Setup preserves safe unrelated statements, owns one CloudWatch publish statement restricted by account and owned-alarm ARN, persists the receiver allowlist, converges the HTTPS subscription and full real-alarm configuration, and requires the current operation's unpredictable ALARM→OK delivery challenge after its persisted cutoff. |

S3 and SES discovery never mutate provider state. The portal does not invent a
bucket while a suitable existing private media bucket is available, and it
never silently adopts an untagged legacy operations topic.

### SES production access is user-controlled

Neither System Setup nor `aws-check` requests SES production access. Sandbox
status is a visible `WARN`, not a deployment `PENDING` or `FAIL`, and it does
not prevent the rest of an environment from reaching zero pending readiness.
`--apply --yes` does not change that rule.

The API hostname, `BASE_URL`, deployment root and selected AWS account are not
evidence of the intended email identity. The operator must explicitly choose
an already verified SES domain and a sender on that exact domain; discovery is
inventory, not authorization. Before requesting production access, the user
must confirm the public website, Privacy Policy, Terms, email consent and
unsubscribe behavior, bounce/complaint handling, any SMS consent plus STOP/HELP
policy, and least-privilege sending permissions. The user then submits the
request directly in AWS, or explicitly approves a future dedicated Admin
action. A deployment agent must never submit it as setup remediation.

Provider failures cross the shared bounded provider-call boundary. A provider
failure detail may contain only `operation`, `provider_code`, `retryable`,
`mutation_state`, an optional safe `request_id`, and the exact `iam_action` for
an authorization denial. Normal successful checks may carry their documented
bounded domain fields such as account, region, ARN, or candidate count. A
denied fix step records its IAM action in the bounded operation log; other
ambiguous provider failures remain reconciling. Raw provider messages,
credentials, exception chains, and request parameters are never retained in
UI, JSON, durable state, or application logs.

This portal integration does not change the `manage.py aws-check` flags,
section names, JSON schema, or exit codes below. The command and portal share
the provider boundary and AWS Check logic, while the portal alone owns the
durable operation protocol and protected runtime settings.

Provider writes are followed by authoritative reads. A resumed operation writes
only state that those reads still prove missing: an interruption after S3 tags,
CORS, SNS policy/subscription, or an alarm update does not replay an already
confirmed mutation. Concurrent unrelated CORS and safe SNS policy statements
are merged and preserved.

## Modes and exit status

```bash
python manage.py aws-check                         # audit; interactive TTY may offer missing creates
python manage.py aws-check --check                 # strictly read-only, never prompts
python manage.py aws-check --json                  # read-only versioned JSON
python manage.py aws-check --apply                 # confirm each eligible create
python manage.py aws-check --apply --yes           # approve create-missing actions only
python manage.py aws-check --section s3 --check     # repeat --section as needed
```

`--region`, `--aws-profile`, and `--timeout` select the bounded AWS context.
Timeout must be 1–30 seconds. A profile overrides static `AWS_KEY` / `AWS_SECRET`;
otherwise a complete static pair is used, falling back to boto3's environment,
container, instance or task-role chain when both are empty. A partial static
pair is a failure.
`--bucket-name` and `--mailbox-email` supply non-secret create details.
`--probe-s3` separately authorizes a UUID sentinel put/get/delete test. If its
cleanup fails, the report prints the exact key. `--adopt-bucket` is an explicit
repair for an interrupted create/tag operation: ownership must be proven by
`ListBuckets`, the region must match, and existing tags are merged rather than
replaced.

`--check` and `--apply` are mutually exclusive; `--yes` requires `--apply`.
JSON mode is read-only and cannot be combined with apply, yes or probe flags.
A non-TTY invocation is audit-only unless `--apply --yes` is explicit; an S3
probe also requires apply authorization.

Exit 0 means no required check failed; WARN, PENDING and SKIP may remain. Exit
1 means readiness failed. Exit 2 means the CLI request was invalid. Statuses
mean: PASS is ready, WARN is usable but needs attention, FAIL blocks readiness,
PENDING awaits an external/operator step, and SKIP was deliberately not run.
JSON uses `schema_version=1` and stable section/status/code/message/details/
remediation/changed items:

```json
{
  "schema_version": 1,
  "generated_at": "2026-08-04T12:00:00+00:00",
  "region": "us-west-2",
  "overall": "pass",
  "counts": {"pass": 8, "warn": 1, "fail": 0, "pending": 1, "skip": 0},
  "items": [{
    "section": "monitoring",
    "status": "pending",
    "code": "sns.topic_not_allowlisted",
    "message": "Operations topic ARN is not in the receiver allowlist",
    "details": {"topic_arn": "arn:aws:sns:us-west-2:123456789012:django-mojo-example-operations"},
    "remediation": "Use System Setup to persist the exact ARN, or update the static AWS_CLOUDWATCH_ALARM_TOPIC_ARNS fallback and restart Django, then rerun.",
    "changed": false
  }]
}
```

## Sections

| Section | Checks |
|---|---|
| `prerequisites` | selected region and public HTTPS `BASE_URL` |
| `identity` | bounded STS identity using static keys, profile, environment, container or instance/task role |
| `cron` | recent dispatcher run plus jobs Redis, runner heartbeats and scheduler lock |
| `s3` | one system-default S3 FileManager, bucket/region/IAM, Public Access Block, CORS and optional sentinel |
| `email` | SES identity/DKIM/topics/receiving readiness, non-blocking sandbox warning, outbound Mailbox and shipped templates |
| `monitoring` | owned SNS topic, exact protected/static allowlist, HTTPS subscription, owned alarms and receiver receipt |
| `dns` | dnsman ACME directory/account, delegation states, certificate expiry; under `--apply`, bootstraps one domain |
| `rules` | opt-in create-only CloudWatch, version-drift and fleet-drift incident policies |
| `versions` | **opt-in** managed-service major version drift (RDS/Aurora, ElastiCache) — never part of a default run, and never reports FAIL |

### System S3 direct uploads

A system bucket created or adopted by `aws-check` stays private and keeps all
four S3 Block Public Access controls enabled. It also receives wildcard browser
CORS (`AllowedOrigins: ["*"]`) and stores `allowed_origins=["*"]` on the
FileManager. These settings are compatible: CORS only lets a browser use a
presigned upload URL that the API already authorized; it does not make the
bucket public or grant unsigned callers any S3 permission.

For an existing system FileManager with direct uploads enabled, read-only mode
reports missing or incomplete CORS. Rerun with `--apply --section s3` to merge
the standard wildcard upload rule without replacing unrelated CORS rules.

`versions` is the one section that is not in the default set: select it with
`--section versions`. It can only report PASS, WARN or PENDING, so a run made
before its extra IAM actions are granted cannot exit 1 in CI. See
[version_drift.md](version_drift.md).

Credential material is never prompted, printed or persisted by this command.
If a FileManager or EmailDomain has stored credentials, its STS account/region
is checked separately; cross-account or cross-region apply fails closed.

### FileManagers configured with `assume_role_arn`

A FileManager may reach a bucket in another AWS account by assuming a role — see
[credentials.md](credentials.md) and
[../fileman/file_manager.md](../fileman/file_manager.md). `aws-check` does **not**
resolve that role. The `s3` section validates the manager's stored static keys —
the *source* identity — and then talks to S3 with those keys directly, rather
than through the backend's assumed session.

Two consequences for a cross-account manager:

- `fileman.cross_context` is **expected, not a defect** whenever the manager's
  stored keys or region differ from the selected ones. It reports exactly what a
  cross-account configuration is: an identity other than the one `aws-check`
  selected. Do not "fix" it by rewriting the manager's region or keys.
- When `cross_context` does *not* fire — the usual case, since the source
  identity normally lives in our own account — the `bucket.*` checks that follow
  describe the **source** identity's access to the tenant's bucket, which is
  normally none. Read them as inconclusive for this manager, and verify it with
  the manager's own `test_connection` action instead; that one goes through the
  assumed session.

## Cron deployment

Run the dispatcher at least once per minute:

```bash
python manage.py shell -c "from mojo.helpers import cron; cron.load_app_cron(); cron.run_now()"
```

Each invocation writes an expiring per-run heartbeat. Overlapping invocations
cannot overwrite one another. Heartbeat write failure is fail-open for cron but
is visible to `aws-check`.

## CloudWatch/SNS two-phase setup

The command uses the signed receiver already exposed at
`/api/aws/cloudwatch/sns/alarm`:

1. Create or discover the tagged operations topic and copy its ARN from the report.
2. Add that exact ARN to `AWS_CLOUDWATCH_ALARM_TOPIC_ARNS` and rerun. System Setup writes the protected runtime value; file configuration remains the CLI fallback.
3. Apply the HTTPS subscription and wait for confirmation.
4. Apply missing alarms. Existing owned drift is WARN; non-owned reserved-name collisions are FAIL.
5. Run the documented disposable-alarm ALARM→OK test. The durable transition receipt proves delivery.
6. Apply the default RuleSet only after delivery is verified.

### One owner for the alarms

**On a managed installation the portal owns the CloudWatch alarms.** It converges
one operations topic, `django-mojo-<slug>-operations`, tags it with
`OWNERSHIP_TAGS`, reserves the `django-mojo/<slug>/` alarm name space, and
discovers what to alarm on from the resources that actually exist rather than
from state written months ago.

There is one other thing that can create alarms for the same estate: the
skeleton's `aws/terraform/modules/observability`. **It ships off.**
`enable_alarms` is `false` in both shipped tfvars, every topic, subscription and
alarm in that module is behind it, and the root's `django_conf_fragment`
deliberately omits `AWS_CLOUDWATCH_ALARM_TOPIC_ARNS` when it is off. A current
skeleton therefore creates no second topic and no second alarm set. See
`aws/terraform/README.md` in the skeleton for that side.

**If you deliberately set `enable_alarms = true`** — a reasonable answer when an
infrastructure team owns monitoring — you own two topics, and you must add the
OpenTofu topic ARN to `AWS_CLOUDWATCH_ALARM_TOPIC_ARNS` in `var/django.conf`
yourself. System Setup's monitoring Fix **merges** the deployment file and the
stored value rather than replacing one with the other, so a file-configured ARN
survives the write — and is restored if an older version had dropped it. See
[settings.md](../helpers/settings.md) for how protected settings resolve across
the two planes.

The two name spaces cannot collide: alarms are `django-mojo/<slug>/...` here and
`<project>-<env>-*` there, and the tofu provider's `ManagedBy = "opentofu"` never
matches this command's `managed-by: django-mojo`. So the two owners do not fight.
**Nothing dedupes them either** — the receiver keys an incident on the alarm ARN,
so two topics watching one condition open two incidents and bill twice. That is a
choice to make knowingly, not a bug to discover later.

**This command never deletes an AWS resource.** Retiring a tofu-owned topic or
alarm set is `enable_alarms = false` and an apply, on the OpenTofu side; deleting
one through the API just means the next apply recreates it.

**External mode is not an exception to ownership.** `INFRASTRUCTURE_MODE=external`
governs whether the portal may *mutate* infrastructure — it does not hand the
alarm topic to somebody else. The portal's own operations topic, and the delivery
path into `/api/aws/cloudwatch/sns/alarm`, remain the portal's in both modes.

Discovery covers EC2, RDS, ElastiCache and **ELBv2 target groups**. Every alarm
uses stable names, ownership tags and ALARM/OK actions on the single owned topic.

| Resource | Signal | Condition | Missing data |
|---|---|---|---|
| EC2 | `StatusCheckFailed` | Maximum >= 1, 2 of 2 one-minute periods | notBreaching |
| EC2/RDS/ElastiCache | `CPUUtilization` | Average >= 90%, 3 of 3 five-minute periods | notBreaching |
| RDS (non-Aurora only) | `FreeStorageSpace` | Average <= 10 GiB, 3 of 3 | notBreaching |
| EC2/RDS (burstable only) | `CPUCreditBalance` | Average <= `AWS_CHECK_CPU_CREDIT_FLOOR` (20), 3 of 3 | notBreaching |
| ElastiCache | `Evictions` | Sum > 0, 3 of 3 | notBreaching |
| RDS | `FreeableMemory` | Average <= `AWS_CHECK_RDS_FREEABLE_MEMORY_FLOOR` (256 MiB), 3 of 3 | notBreaching |
| RDS | `DatabaseConnections` | Maximum >= `AWS_CHECK_RDS_MAX_CONNECTIONS` (500), 3 of 3 | notBreaching |
| ELBv2 target group | `UnHealthyHostCount` | Maximum >= 1, 2 of 2 one-minute periods | notBreaching |
| ELBv2 target group | `HealthyHostCount` | Minimum < 1, 2 of 2 one-minute periods | **breaching** |
| deployment-wide | `MinDaysToExpiry` | Minimum <= `AWS_CHECK_CERT_EXPIRY_DAYS` (14), 3 of 3 hourly | **breaching** |

`CPUCreditBalance` is created only on burstable families (`t2`/`t3`/`t3a`/`t4g`,
and `db.t*`), because only they publish it — elsewhere the alarm would never
receive a datapoint and read as permanently green.

**Target groups get two alarms, not one.** `UnHealthyHostCount` counts unhealthy
*registered* targets, so a group whose targets have all been deregistered
reports `0` and looks healthy. `HealthyHostCount < 1` is the only signal that
sees the total outage, and it treats missing data as breaching because a load
balancer that stops reporting is itself the outage. Target groups attached to no
load balancer are skipped — they publish nothing. Dimension values are ARN
*suffixes* (`targetgroup/<name>/<id>`, `net/<name>/<id>`); a full ARN would be
accepted and then never receive a datapoint.

Target-group discovery has its own failure guard, separate from EC2/RDS/
ElastiCache discovery: a deployment whose IAM policy predates
`elasticloadbalancing:DescribeTargetGroups` reports `resources.elbv2_denied`
(WARN) and simply has no load-balancer alarms desired — EC2, RDS and
ElastiCache are still discovered and alarmed normally. Letting that denial
reach the shared discovery guard instead would report `resources.discovery_denied`
and desire *no* alarms for anything.

`DatabaseConnections` ships a deliberately forgiving default. RDS derives
`max_connections` from instance memory (roughly 112 on `db.t3.micro`, ~405 on
`db.t3.medium`), so no single default is right everywhere. `500` errs high: it
will not fire spuriously, and a chronically-firing alarm gets muted — which
silences the entire operations topic and every other alarm on it. The cost is
that on an instance class whose ceiling is below 500 it cannot fire at all; tune
it to ~80% of the class's `max_connections`.

### Certificate expiry

The one alarm that catches every cause of a stalled renewal at once — publisher
down, challenge misrouted, credentials wrong, delegation record deleted. Two
halves:

- a dnsman cron (`publish_certificate_expiry`, hourly) publishing the fewest days
  remaining across every certificate as `DjangoMojo/Certificates/MinDaysToExpiry`,
  dimensioned by deployment;
- the alarm, with **`TreatMissingData = "breaching"`** — deliberately opposite to
  every other profile, because a dead publisher must alarm or the monitoring
  fails silently in exactly the scenario it exists to catch.

`aws-check` will not create the alarm until the metric actually exists. A
breaching alarm created before its publisher has ever run goes straight to ALARM
and pages during bootstrap, which teaches operators to ignore the topic.

| Code | Status | Meaning |
|---|---|---|
| `alarms.cert_metric_unpublished` | PENDING | The metric has never been published. Run the dnsman cron, then rerun. |
| `alarms.cert_metric_unknown` | PENDING | `cloudwatch:ListMetrics` was denied, so whether the metric exists is unknown. The rest of the section still runs. |

Aurora (`aurora`, `aurora-mysql`, `aurora-postgresql`, and any future `aurora-*`
engine) publishes **no per-instance free-space metric** — its storage is a shared
auto-scaling cluster volume, and local scratch space is reported as
`FreeLocalStorage`, whose safe threshold varies by instance class. A
`FreeStorageSpace` alarm on an Aurora instance therefore never receives a
datapoint and, with `TreatMissingData=notBreaching`, sits permanently green while
monitoring nothing. `aws-check` no longer creates one and instead reports the
gap explicitly:

| Code | Status | Meaning |
|---|---|---|
| `alarms.aurora_storage_unmonitored` | WARN | Aurora instances were discovered and have no storage alarm. Details list the instance ids. Add a `FreeLocalStorage` alarm sized to each instance class, or watch the cluster volume in the RDS console. |
| `alarms.stale_aurora_storage` | WARN | Owned `FreeStorageSpace` alarms created by an earlier django-mojo version still exist on Aurora instances. Details list the exact alarm names and remediation gives the `aws cloudwatch delete-alarms --alarm-names ...` command. |

Both are reported before the SNS allowlist and subscription-confirmation gates,
so a half-configured deployment still sees them. `aws-check` never deletes AWS
resources, so the stale alarms must be removed by hand; an owned alarm on the
reserved name that has already been hand-edited to a different metric (for
example `FreeLocalStorage`) is preserved and reported as `alarms.drifted`
instead.

The topic name is `django-mojo-<deployment>-operations`; alarm names are
`django-mojo/<deployment>/<resource-type>/<resource-id>/<signal>`. Owned
resources carry `managed-by=django-mojo`, `purpose=cloudwatch-incidents`, and
`deployment=<slug>`. Existing owned drift is reported and preserved; a
same-name resource without those ownership tags is a conflict.

After a signed delivery receipt, apply can create the opt-in RuleSet
`AWS CloudWatch - Operations` in category `aws:cloudwatch`, bundling by alarm
model and ID. Its handler is `notify://perm@manage_security`; it does not page,
open a direct ticket, block an IP, or bypass the incident lifecycle. The normal
global default-rule seeder never installs it on non-AWS deployments.

The `rules` section reports and can create a **second** opt-in RuleSet,
`Health - AWS Version Drift` in category `aws:versions`, whose handler is
`notify://perm@manage_security,ticket://?priority=8&category=aws-version-drift&maestro=1`.
It does **not** sit behind the `monitoring_ready` / `delivery_seen` gate above:
that gate exists because CloudWatch alarms need a confirmed SNS receiver,
whereas version-drift events are generated in-process. See
[version_drift.md](version_drift.md).

A **third** opt-in RuleSet, `Health - Infrastructure Drift` in category
`infra:drift`, is reported and can be created the same way. Its handler is
`notify://perm@manage_security` — notify only, no ticket. It is outside the
receiver gate for the same reason. See [infra_drift.md](infra_drift.md).

## The `dns` section

Audits dnsman's ACME state, and under `--apply` bootstraps one domain end to end.
It lives here rather than in a separate `dnsman_bootstrap` command so it inherits
the audit/apply split, per-action confirmation, `--json`, the exit-code contract
and secret redaction — one tool to learn instead of two.

The audit is read-only and reports:

| Code | Status | Meaning |
|---|---|---|
| `dnsman.not_installed` | SKIP | dnsman is not in `INSTALLED_APPS`; nothing to audit. |
| `acme.staging_directory` | WARN | `DNSMAN_ACME_DIRECTORY_URL` points at Let's Encrypt **staging**, which is the shipped default. Certificates issued there are untrusted by browsers. |
| `acme.account_missing` | PENDING | No ACME account is registered for the configured directory yet; it registers itself on first issuance. |
| `acme.account_registered` | PASS | A production ACME account exists for the configured directory. |
| `delegation.available` / `delegation.direct_only` | PASS | Whether an ACME hub is configured. An absent hub is not a fault — direct Route53/GoDaddy issuance is fully supported. |
| `delegation.broken` | WARN | Delegations in the `broken` state, which will not renew. |
| `delegation.states` | PASS | No delegations are broken; details carry a count by state. |
| `certificates.none` | PENDING | No certificates have been issued yet. |
| `certificates.expired` | FAIL | One or more certificates are past `not_after`. |
| `certificates.renewing` | WARN | Certificates inside `DNSMAN_CERT_RENEW_DAYS`. |
| `certificates.healthy` | PASS | Every certificate is outside its renewal window. |

The staging default is deliberate — an unconfigured deploy must not be able to
burn production rate limits — so the section names it in words rather than
printing a bare URL. Bootstrapping against staging while believing you are live
is the likeliest way to misread this section.

> **`--json` output changed sensitivity class.** `dns` is in the default section
> set, so a plain `aws-check --json` now embeds tenant domain names and
> certificate common names in a report that previously carried only AWS
> infrastructure data. Treat the output accordingly, or select sections
> explicitly when producing a report for somewhere less trusted.

### Bootstrapping a domain

```bash
python manage.py aws-check --apply --section dns --dns-domain example.com --dns-group 7
```

`--dns-group` takes a group id or an exact name and is required; an unnamed or
ambiguous owner fails closed with `dns.group_required` before any mutation.
Three checks run before anything is written, none of which prompt:

- the domain must normalize cleanly, or the run stops at `dns.domain_invalid`;
- if a `Domain` already exists for that name under a **different** group, the
  run stops at `dns.domain_not_owned` — `initiate()` only enforces ownership
  when handed an existing `Domain`, so the bootstrap resolves and passes one
  explicitly;
- if the named group already has a **verified** delegation for the domain, the
  bootstrap reports `delegation.already_verified` and jumps straight to
  requesting a certificate, skipping allocation and CNAME proof entirely.
  `prove_alias()` is not a free read-only lookup — on failure it can retire a
  delegation that has previously verified — so a rerun must never re-prove one
  that already works.

Otherwise the flow proceeds, each step confirmed separately:

1. Allocate the delegation and report `delegation.cname_required`, carrying
   `source_name` and `target_name` as structured fields so `--json` consumers get
   the CNAME, not just prose. **This is the output you hand to the domain owner.**
2. Verify the `_acme-challenge` CNAME authoritatively resolves. If it does not,
   stop at `delegation.cname_unverified` and request nothing.
3. Bind or create the `Domain`, then queue issuance.

Step 2 is not a formality. Let's Encrypt rate-limits **failed** validations at 5
per account per hostname per hour, so firing at an unpropagated record burns the
budget and blocks retries for an hour — during a bootstrap, which is exactly when
someone is iterating. The DNS lookup is free; the rate limit costs an hour.

Issuance itself runs on the job runner, so the last step reports `PENDING`
(`certificate.requested`) and never `PASS` — the command cannot observe the
outcome, only that the work was queued. Rerun the section to see the result.

Unlike every other `--apply` path, this one does **not** require verified AWS
credentials: it writes dnsman rows and talks to the ACME hub over HTTPS, so
gating it on AWS identity would refuse the bootstrap on a correctly-configured
box that simply has no AWS keys.

## Least-privilege IAM actions

Grant only actions for the selected sections and scope resource ARNs wherever
AWS supports it:

| Section/mode | Required AWS actions |
|---|---|
| identity/all AWS checks | `sts:GetCallerIdentity` |
| S3 audit | `s3:ListBucket`, `s3:GetBucketPublicAccessBlock`, `s3:GetBucketCORS` |
| S3 create/adopt/apply | `s3:CreateBucket`, `s3:GetBucketLocation`, `s3:PutBucketPublicAccessBlock`, `s3:GetBucketTagging`, `s3:PutBucketTagging`, `s3:GetBucketCORS`, `s3:PutBucketCORS`; adoption also needs `s3:ListAllMyBuckets` |
| S3 probe | `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject` on `__django_mojo_aws_check__/*` |
| SES/email audit | `ses:GetAccount`, `ses:GetIdentityVerificationAttributes`, `ses:GetIdentityDkimAttributes`, `ses:GetIdentityNotificationAttributes`, `ses:DescribeReceiptRuleSet`, `sns:GetTopicAttributes`, `sns:ListSubscriptionsByTopic`, `s3:ListBucket` for optional inbound storage |
| SES/email create-missing | `ses:VerifyDomainIdentity`, `ses:VerifyDomainDkim`, `ses:SetIdentityNotificationTopic`, `sns:ListTopics`, `sns:ListTagsForResource`, `sns:CreateTopic`, `sns:TagResource`, `sns:Subscribe` |
| Monitoring audit | `sns:ListTopics`, `sns:ListTagsForResource`, `sns:ListSubscriptionsByTopic`, `cloudwatch:DescribeAlarms`, `cloudwatch:ListTagsForResource`, `cloudwatch:ListMetrics`, `ec2:DescribeInstances`, `rds:DescribeDBInstances`, `elasticache:DescribeCacheClusters`, `elasticloadbalancing:DescribeTargetGroups` |
| Monitoring apply | `sns:CreateTopic`, `sns:TagResource`, `sns:Subscribe`, `cloudwatch:PutMetricAlarm`, `cloudwatch:TagResource` |
| System Setup S3 discovery/adoption | `s3:ListAllMyBuckets`, `s3:GetBucketLocation`, `s3:GetBucketWebsite`, `s3:GetBucketTagging`, `s3:GetBucketPolicy`, `s3:GetBucketPublicAccessBlock`, `s3:GetBucketCORS`, `s3:PutBucketPublicAccessBlock`, `s3:PutBucketTagging`, `s3:PutBucketCORS` |
| System Setup SES import | `ses:ListIdentities`, `ses:GetIdentityVerificationAttributes`; sender, local domain import, and missing-only templates use the Django database |
| System Setup monitoring convergence/probe | Monitoring audit actions above plus `sns:GetTopicAttributes`, `sns:SetTopicAttributes`, `sns:CreateTopic`, `sns:TagResource`, `sns:Subscribe`, `cloudwatch:PutMetricAlarm`, and `cloudwatch:SetAlarmState` |
| Versions audit (opt-in) | `rds:DescribeDBClusters`, `rds:DescribeDBInstances`, `rds:DescribeDBEngineVersions`, `rds:DescribeDBMajorEngineVersions`, `elasticache:DescribeCacheClusters`, `elasticache:DescribeCacheEngineVersions` |

`ses:PutAccountDetails` is intentionally absent: no audit or apply mode may
request SES production access. Cron, rule, mailbox and template checks use
Django database/Redis access rather than IAM. The `dns` section talks to dnsman
and the ACME hub, not to AWS, so it needs no IAM actions of its own. The command
never deletes AWS resources, edits deployment files, changes DNS, requests SES
production access, or sends email.

### A different identity: the job runner

One action in this feature does **not** belong to the operator running
`aws-check`:

| Identity | Required AWS actions |
|---|---|
| dnsman job runner (certificate-expiry publisher) | `cloudwatch:PutMetricData` on namespace `DjangoMojo/Certificates` |

Without it the hourly publisher fails with AccessDenied, the metric never
appears, and the expiry alarm sits permanently at
`alarms.cert_metric_unpublished` — which reads like "not set up yet" rather than
"broken". Grant it to whatever identity the job runners use, not to the operator.

## Not the same as `python -m mojo.deploy.check_setup`

django-mojo ships a second AWS auditor, `mojo.deploy.check_setup`
([deploy/README.md](../deploy/README.md#check_setup)). They answer different
questions and neither replaces the other:

| | `manage.py aws-check` | `python3 -m mojo.deploy.check_setup` |
|---|---|---|
| Needs Django | yes — reads settings, models, Redis | no; runs before `django.conf` exists |
| Scope | this deployment's own resources, named by configuration | the whole AWS account, whatever is in it |
| Mutates | yes, with `--apply` — creates missing buckets, topics, subscriptions, alarms | never; every call is a Describe/Get/List |
| Covers | S3/SES/SNS/CloudWatch wiring, cron, mailboxes, templates, incident rule defaults | EC2 and security-group posture, load balancer, RDS/Aurora, ElastiCache, EBS/backup/CloudTrail/GuardDuty, account-wide S3, IAM users and keys |
| Run from | the app box, as the app | anywhere with credentials, including a laptop |

Where they overlap, `check_setup` is deliberately reduced to the delta:

- **Monitoring.** `aws-check`'s monitoring section owns SNS topic existence and
  the alarm inventory, discovers them account-wide, and can create what is
  missing. `check_setup` therefore checks only what this command does not:
  subscriptions stuck at `PendingConfirmation`, alarms sitting in
  `INSUFFICIENT_DATA`, and whether application log groups exist and expire.
- **Load balancer.** Both look at ELBv2 now, at different things. `check_setup`
  audits *posture* — AZ coverage, deletion protection, access logs, and current
  target health — and never creates anything. `aws-check` owns the *alarms* on
  target groups, because every alarm has to reach the one owned SNS topic and its
  single allowlist entry; a second creator would mean a second delivery path,
  which is exactly what the two-phase design prevents. Read `check_setup` for "is
  this load balancer built correctly", `aws-check` for "will I be told when it
  breaks".
- **S3.** This command inspects the public-access block on the one bucket
  FileManager is configured with. `check_setup` sweeps *every* bucket in the
  account and additionally reports versioning, default encryption and public
  bucket policies.

Use `aws-check` to get a deployment working and keep it wired up. Use
`check_setup` to answer "is this whole account set up sanely", especially
before a security review — and note that its topology assertions are off unless
you ask for them.
