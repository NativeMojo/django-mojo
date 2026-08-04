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
`--bucket-name` and `--mailbox-email` supply non-secret create details.
`--probe-s3` separately authorizes a UUID sentinel put/get/delete test. If its
cleanup fails, the report prints the exact key. `--adopt-bucket` is an explicit
repair for an interrupted create/tag operation: ownership must be proven by
`ListBuckets`, the region must match, and existing tags are merged rather than
replaced.

Exit 0 means no required check failed; WARN, PENDING and SKIP may remain. Exit
1 means readiness failed. Exit 2 means the CLI request was invalid. JSON uses
`schema_version=1` and includes stable status/code/remediation fields.

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

## IAM outline

Audit needs STS identity; Redis/database access; S3 head/public-block/CORS;
SES identity/account/receipt-rule reads; SNS list/tag/subscription reads;
CloudWatch alarm/tag reads; and EC2/RDS/ElastiCache describe permissions.
Apply additionally needs the corresponding create/tag/subscribe,
`cloudwatch:PutMetricAlarm`, and optional S3 object permissions. Grant only the
selected sections and deployment resources. The command never deletes AWS
resources, edits deployment files, changes DNS, or sends email.
