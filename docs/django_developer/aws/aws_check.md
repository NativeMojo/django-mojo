# AWS deployment readiness (`aws-check`)

`python manage.py aws-check` audits the AWS and operational wiring required by
django-mojo. It is audit-first: a normal non-interactive invocation never
mutates AWS or Django state, and apply mode creates only confirmed missing
resources. Existing resources that differ from defaults are reported and
preserved.

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
    "message": "Operations topic ARN is not in the static receiver allowlist",
    "details": {"topic_arn": "arn:aws:sns:us-west-2:123456789012:django-mojo-example-operations"},
    "remediation": "Add the exact ARN to AWS_CLOUDWATCH_ALARM_TOPIC_ARNS, restart Django, then rerun.",
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
| `email` | SES identity/DKIM/sandbox/topics/receiving audit, outbound Mailbox and shipped templates |
| `monitoring` | owned SNS topic, exact static allowlist, HTTPS subscription, owned alarms and receiver receipt |
| `rules` | opt-in create-only CloudWatch incident policy |

Credential material is never prompted, printed or persisted by this command.
If a FileManager or EmailDomain has stored credentials, its STS account/region
is checked separately; cross-account or cross-region apply fails closed.

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
2. Add that exact ARN to file-only `AWS_CLOUDWATCH_ALARM_TOPIC_ARNS`, restart Django, and rerun.
3. Apply the HTTPS subscription and wait for confirmation.
4. Apply missing alarms. Existing owned drift is WARN; non-owned reserved-name collisions are FAIL.
5. Run the documented disposable-alarm ALARM→OK test. The durable transition receipt proves delivery.
6. Apply the default RuleSet only after delivery is verified.

Default alarms are EC2 status-check failure, EC2/RDS/ElastiCache CPU >= 90%,
and RDS free storage <= 10 GiB. They use stable names, ownership tags,
`TreatMissingData=notBreaching`, and ALARM/OK actions. Connection counts and
CWAgent memory/disk are not guessed because portable thresholds/dimensions do
not exist.

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

## Least-privilege IAM actions

Grant only actions for the selected sections and scope resource ARNs wherever
AWS supports it:

| Section/mode | Required AWS actions |
|---|---|
| identity/all AWS checks | `sts:GetCallerIdentity` |
| S3 audit | `s3:ListBucket`, `s3:GetBucketLocation`, `s3:GetBucketPublicAccessBlock`, `s3:GetBucketCORS` |
| S3 create/adopt | `s3:CreateBucket`, `s3:PutBucketPublicAccessBlock`, `s3:GetBucketTagging`, `s3:PutBucketTagging`; adoption also needs `s3:ListAllMyBuckets` |
| S3 probe | `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject` on `__django_mojo_aws_check__/*` |
| SES/email audit | `ses:GetIdentityVerificationAttributes`, `ses:GetIdentityDkimAttributes`, `ses:GetAccountSendingEnabled`, `ses:GetSendQuota`, `ses:GetIdentityNotificationAttributes`, `ses:DescribeReceiptRuleSet`, `sns:GetTopicAttributes`, `sns:ListTopics`, `sns:ListSubscriptionsByTopic`, `s3:GetBucketPolicy` |
| SES/email create-missing | `ses:VerifyDomainIdentity`, `ses:VerifyDomainDkim`, `ses:SetIdentityNotificationTopic`, `sns:CreateTopic`, `sns:Subscribe` |
| Monitoring audit | `sns:ListTopics`, `sns:ListTagsForResource`, `sns:ListSubscriptionsByTopic`, `cloudwatch:DescribeAlarms`, `cloudwatch:ListTagsForResource`, `ec2:DescribeInstances`, `rds:DescribeDBInstances`, `elasticache:DescribeCacheClusters` |
| Monitoring apply | `sns:CreateTopic`, `sns:TagResource`, `sns:Subscribe`, `cloudwatch:PutMetricAlarm`, `cloudwatch:TagResource` |

Cron, rule, mailbox and template checks use Django database/Redis access rather
than IAM. The command never deletes AWS resources, edits deployment files,
changes DNS, requests SES production access, or sends email.
