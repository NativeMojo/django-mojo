"""What watches the account: CloudTrail, GuardDuty, and the log groups.

An account gets ONE multi-region trail and ONE GuardDuty detector — AWS enforces
the second and charges twice for a duplicate of the first. So both are adopted
when they already exist, with a finding saying so, and never duplicated. That is
the common case: an organisation-wide trail set up years ago is exactly what
should be left alone.

THE THREE LOG GROUPS are created here, at 90-day retention:

    /mojo/<project>-<env>/nginx
    /mojo/<project>-<env>/app
    /mojo/<project>-<env>/cloud-init

Their names come from `spec.names()`, which is the single source the node's
CloudWatch agent configuration is generated from and asserted against — one
place to change, not two that must agree. The node role's inline policy carries
the matching scoped write grant (see `identity.AGENT_LOG_ACTIONS`).

Retention is stated rather than left at "never expire", because a log group with
no retention is a bill that only goes up and nobody notices for a year. An
existing group set to something else is reported as drift with the current value
named, and `apply` changes the retention in place — a log group is never
replaced, because replacing it would throw away everything already in it.
"""

import json

from mojo.deploy.provision import report
from mojo.deploy.provision import spec as spec_module


STEP = "observability"


def ensure_observability(clients, spec, observed, apply=False):
    findings, actions = [], []
    result = report.Result()

    _ensure_log_groups(clients, spec, observed, findings, actions, apply,
                       result)
    _ensure_trail(clients, spec, observed, findings, actions, apply, result)
    _ensure_guardduty(clients, spec, observed, findings, actions, apply,
                      result)
    return findings, actions, result


# ── log groups ──────────────────────────────────────────────────────────────

def _ensure_log_groups(clients, spec, observed, findings, actions, apply,
                       result):
    names = spec_module.names(spec)
    logs = clients.get("logs")
    present = observed.get("log_groups") or {}
    created = []

    for kind, group_name in sorted(names["log_groups"].items()):
        existing = present.get(group_name)
        if existing is None:
            findings.append(report.missing(
                STEP, f"log_group.{kind}.missing",
                f"log group {group_name} does not exist",
                f"apply creates it at {spec_module.LOG_RETENTION_DAYS}-day "
                f"retention"))
            actions.append(report.Action(
                STEP, "create", group_name,
                f"{spec_module.LOG_RETENTION_DAYS} day retention"))
            if apply:
                made = report.safe(
                    findings, STEP, "logs.create_log_group",
                    lambda n=group_name: logs.create_log_group(
                        logGroupName=n,
                        tags=spec_module.TAGS(spec, "observability")))
                if made is not None:
                    report.safe(
                        findings, STEP, "logs.put_retention_policy",
                        lambda n=group_name: logs.put_retention_policy(
                            logGroupName=n,
                            retentionInDays=spec_module.LOG_RETENTION_DAYS))
                    created.append(group_name)
            continue

        retention = existing.get("retentionInDays")
        if retention == spec_module.LOG_RETENTION_DAYS:
            findings.append(report.existing(
                STEP, f"log_group.{kind}.ok",
                f"{group_name} is in place at {retention}-day retention"))
            created.append(group_name)
            continue

        findings.append(report.drift(
            STEP, f"log_group.{kind}.retention",
            f"{group_name} retains for "
            f"{retention if retention else 'ever'}; this topology declares "
            f"{spec_module.LOG_RETENTION_DAYS} days",
            "apply changes the retention in place — the group and everything "
            "already in it are kept"))
        actions.append(report.Action(
            STEP, "modify", group_name,
            f"retention {spec_module.LOG_RETENTION_DAYS} days"))
        if apply:
            report.safe(
                findings, STEP, "logs.put_retention_policy",
                lambda n=group_name: logs.put_retention_policy(
                    logGroupName=n,
                    retentionInDays=spec_module.LOG_RETENTION_DAYS))
        created.append(group_name)

    result.set("log_group_names", sorted(names["log_groups"].values()))
    result.set("log_groups_present", created)


# ── cloudtrail ──────────────────────────────────────────────────────────────

def trail_bucket_policy(bucket, account_id, trail_name, region):
    """What CloudTrail needs to be allowed to write into the bucket.

    The `aws:SourceArn` condition is the confused-deputy guard: without it, any
    account's trail could be pointed at this bucket.
    """
    arn = f"arn:aws:s3:::{bucket}"
    source = (f"arn:aws:cloudtrail:{region}:{account_id or '*'}:trail/"
              f"{trail_name}")
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AWSCloudTrailAclCheck",
                "Effect": "Allow",
                "Principal": {"Service": "cloudtrail.amazonaws.com"},
                "Action": "s3:GetBucketAcl",
                "Resource": arn,
                "Condition": {"StringEquals": {"aws:SourceArn": source}},
            },
            {
                "Sid": "AWSCloudTrailWrite",
                "Effect": "Allow",
                "Principal": {"Service": "cloudtrail.amazonaws.com"},
                "Action": "s3:PutObject",
                "Resource": (f"{arn}/AWSLogs/{account_id or '*'}/*"),
                "Condition": {
                    "StringEquals": {
                        "s3:x-amz-acl": "bucket-owner-full-control",
                        "aws:SourceArn": source,
                    },
                },
            },
            {
                "Sid": "DenyInsecureTransport",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": [arn, f"{arn}/*"],
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            },
        ],
    }


def _ensure_trail(clients, spec, observed, findings, actions, apply, result):
    names = spec_module.names(spec)
    trails = observed.get("trails") or []

    adopted = None
    for trail in trails:
        if trail.get("Name") == names["cloudtrail"]:
            adopted = trail
            break
    if adopted is None:
        for trail in trails:
            if trail.get("IsMultiRegionTrail"):
                adopted = trail
                break

    if adopted is not None:
        findings.append(report.existing(
            STEP, "cloudtrail.ok",
            f"multi-region trail {adopted.get('Name')} is already recording "
            f"this account"
            + ("" if adopted.get("Name") == names["cloudtrail"]
               else " — adopted rather than duplicated, because a second "
                    "multi-region trail records the same events twice and "
                    "bills for both")))
        if not adopted.get("LogFileValidationEnabled"):
            findings.append(report.drift(
                STEP, "cloudtrail.validation",
                f"trail {adopted.get('Name')} does not have log file "
                f"validation enabled",
                "apply enables it"))
            actions.append(report.Action(STEP, "modify", adopted.get("Name"),
                                         "log file validation"))
            if apply:
                cloudtrail = clients.get("cloudtrail")
                report.safe(
                    findings, STEP, "cloudtrail.update_trail",
                    lambda: cloudtrail.update_trail(
                        Name=adopted.get("Name"),
                        EnableLogFileValidation=True))
        result.set("trail_name", adopted.get("Name"))
        return

    bucket = names["cloudtrail_bucket"]
    findings.append(report.missing(
        STEP, "cloudtrail.missing",
        "this account has no multi-region CloudTrail trail",
        f"apply creates {names['cloudtrail']} writing into {bucket}, with log "
        f"file validation on"))
    actions.append(report.Action(STEP, "create", names["cloudtrail"]))
    result.set("trail_name", names["cloudtrail"])
    if not apply:
        return

    if not _ensure_trail_bucket(clients, spec, observed, bucket, findings,
                                actions):
        return

    cloudtrail = clients.get("cloudtrail")
    created = report.safe(
        findings, STEP, "cloudtrail.create_trail",
        lambda: cloudtrail.create_trail(
            Name=names["cloudtrail"], S3BucketName=bucket,
            IsMultiRegionTrail=True, IncludeGlobalServiceEvents=True,
            EnableLogFileValidation=True,
            TagsList=spec_module.tag_list(spec, "observability",
                                          name=names["cloudtrail"])))
    if created:
        # A trail that exists but is not logging is the worst of both worlds:
        # it looks like coverage and records nothing.
        report.safe(findings, STEP, "cloudtrail.start_logging",
                    lambda: cloudtrail.start_logging(Name=names["cloudtrail"]))


def _ensure_trail_bucket(clients, spec, observed, bucket, findings, actions):
    """The trail's own bucket. Same S3 tagging exception as the config bucket —
    `CreateBucket` takes no tags, and this bucket is found by name."""
    s3 = clients.get("s3")
    if observed.get("cloudtrail_bucket"):
        return True
    actions.append(report.Action(STEP, "create", bucket))
    request = {"Bucket": bucket}
    if spec.region != "us-east-1":
        request["CreateBucketConfiguration"] = {
            "LocationConstraint": spec.region}
    created = report.safe(findings, STEP, "s3.create_bucket",
                          lambda: s3.create_bucket(**request))
    if created is None:
        return False
    report.safe(
        findings, STEP, "s3.put_bucket_tagging",
        lambda: s3.put_bucket_tagging(
            Bucket=bucket,
            Tagging={"TagSet": spec_module.tag_list(spec, "observability",
                                                    name=bucket)}))
    report.safe(
        findings, STEP, "s3.put_public_access_block",
        lambda: s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True, "IgnorePublicAcls": True,
                "BlockPublicPolicy": True, "RestrictPublicBuckets": True}))
    names = spec_module.names(spec)
    report.safe(
        findings, STEP, "s3.put_bucket_policy",
        lambda: s3.put_bucket_policy(
            Bucket=bucket,
            Policy=json.dumps(trail_bucket_policy(
                bucket, spec.account_id or observed.get("account_id"),
                names["cloudtrail"], spec.region))))
    return True


# ── guardduty ───────────────────────────────────────────────────────────────

def _ensure_guardduty(clients, spec, observed, findings, actions, apply,
                      result):
    detectors = observed.get("detector_ids") or []
    if detectors:
        findings.append(report.existing(
            STEP, "guardduty.ok",
            f"GuardDuty detector {detectors[0]} is already enabled in "
            f"{spec.region} — adopted, not duplicated; AWS allows one per "
            f"region per account"))
        result.set("detector_id", detectors[0])
        return

    findings.append(report.missing(
        STEP, "guardduty.missing",
        f"GuardDuty is not enabled in {spec.region}",
        "apply enables a detector"))
    actions.append(report.Action(STEP, "create", "guardduty detector"))
    if not apply:
        return
    guardduty = clients.get("guardduty")
    created = report.safe(
        findings, STEP, "guardduty.create_detector",
        lambda: guardduty.create_detector(
            Enable=True, Tags=spec_module.TAGS(spec, "observability")))
    if created:
        result.set("detector_id", created.get("DetectorId"))
