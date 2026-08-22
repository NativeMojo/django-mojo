#!/usr/bin/env python3
"""Audit an AWS account for the security and durability gaps django-mojo cares about.

Read-only. Every call is a Describe/Get/List — nothing here mutates the account,
so it is safe to run against production, and safe to hand to an AI session as the
"is this account set up correctly?" oracle.

    python3 -m mojo.deploy.check_setup                       # uses /opt/api/var/django.conf
    python3 -m mojo.deploy.check_setup --config ./var/django.conf
    python3 -m mojo.deploy.check_setup --json                # machine-readable, for CI
    python3 -m mojo.deploy.check_setup --section rds,cache   # one area at a time
    python3 -m mojo.deploy.check_setup --profile prod        # use an ~/.aws profile
    python3 -m mojo.deploy.check_setup --topology reference  # + reference-topology asserts

Credentials come from django.conf (AWS_KEY / AWS_SECRET / AWS_REGION) so the tool
works from a laptop or from the box itself with no extra setup. --profile
overrides that and uses the normal boto3 chain, which is what you want once the
instances carry an IAM role and the static keys are gone.

TWO CLASSES OF FINDING, and the difference matters:

    universal    true for any deployment — IMDSv2, public-access blocks,
                 unencrypted volumes, world-open SSH/Postgres/Redis,
                 AdministratorAccess on the app key, key age. Always checked.
    topology     assertions about ONE deployment shape (the django-mojo
                 reference topology: web node + N API nodes behind a load
                 balancer, Aurora writer+reader Multi-AZ, ElastiCache with
                 automatic failover, alarms into SNS). OFF by default, because
                 a single-node dev account is not misconfigured — it is just a
                 different deployment. Turn them on with `--topology reference`
                 or MOJO_DEPLOY_TOPOLOGY=reference in django.conf.

A finding is one of:

    PASS   no gap found
    WARN   works, but is a known way to lose an afternoon later
    FAIL   a correctness, durability, or security gap that should be scheduled
    INFO   context, no judgement
    BLIND  the audit credential could not see this at all

Exit code is 1 if anything FAILed OR anything came back BLIND, else 0 — so this
can gate a deploy. BLIND counts because a gate that returns green when it cannot
see is worse than no gate: an audit key without iam:List* would otherwise report
a clean IAM section it never actually read.

THREE THINGS IN THIS REPOSITORY LOOK AT AN AWS ACCOUNT. They are not
interchangeable, and picking the wrong one wastes an afternoon:

    this module                         pre-Django, read-only, and it JUDGES.
                                        It scores an account against the
                                        django-mojo reference topology and
                                        universal security expectations, and
                                        exits non-zero so it can gate a deploy.
                                        Answers "is this account set up
                                        correctly?".
    mojo/apps/aws/services/aws_check.py in-Django, reads settings, and CREATES
                                        MISSING integration surfaces — the cron
                                        rule, the S3 file manager, SES, dnsman.
                                        It is about a running deployment's
                                        readiness, not about infrastructure.
                                        Answers "can this deployment talk to
                                        AWS?".
    mojo/deploy/provision/discover.py   pre-Django, read-only, and JUDGES
                                        NOTHING. It returns the raw shape of
                                        the resources the provisioner manages
                                        so its ensure-services can decide what
                                        to create. Answers "what is already
                                        there?".

See docs/django_developer/aws/aws_check.md.
"""

import argparse
import json
import os
import sys

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
INFO = "INFO"
BLIND = "BLIND"

STATUSES = (PASS, WARN, FAIL, INFO, BLIND)

# Ordered so the report reads top-down: identity, then compute, then the data
# tier, then what watches it.
SECTIONS = (
    "account", "ec2", "sg", "lb", "rds", "cache",
    "observability", "backup", "s3", "iam",
)

# The one topology this tool knows how to assert against.
TOPOLOGIES = ("none", "reference")

# Instance families that bill as burstable. Fine for a dev box, a liability for
# a node serving money: when the credit balance empties the box does not fail,
# it silently gets slow, which is the hardest outage class to diagnose.
BURSTABLE_PREFIXES = ("t2.", "t3.", "t3a.", "t4g.")

# t2 specifically is previous-generation: same money as t3 for less machine.
LEGACY_PREFIXES = ("t2.", "m3.", "m4.", "c3.", "c4.", "r3.", "r4.")

# django.conf lives next to the app on every node. The old repo-relative default
# resolved to <site-packages>/var/django.conf once this shipped inside a wheel —
# a path that never exists, producing a confusing "no config at ..." and exit 2.
DEFAULT_CONFIG_PATH = "/opt/api/var/django.conf"

# Error codes that mean "you cannot see this", not "this is fine".
DENIED_CODES = (
    "AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
    "NotAuthorized", "AuthorizationError", "UnauthorizedAccess",
)
THROTTLE_CODES = (
    "Throttling", "ThrottlingException", "RequestLimitExceeded",
    "TooManyRequestsException", "RequestThrottled", "SlowDown",
)
# The credential itself is bad or expired. These are NOT permission errors, so
# they miss DENIED_CODES, but they mean the check did not run just the same --
# and a gate that exits 0 because its key was revoked is the fail-open case this
# module exists to close. AuthFailure in particular is what EC2 returns for a
# deactivated key.
UNUSABLE_CODES = (
    "AuthFailure", "InvalidClientTokenId", "SignatureDoesNotMatch",
    "UnrecognizedClientException", "ExpiredToken", "ExpiredTokenException",
    "InvalidAccessKeyId", "OptInRequired", "SubscriptionRequiredException",
)


class Report:
    """Collects findings and prints them grouped by section."""

    def __init__(self):
        self.findings = []

    def add(self, section, status, name, detail, fix=None):
        self.findings.append({
            "section": section,
            "status": status,
            "name": name,
            "detail": detail,
            "fix": fix,
        })

    def passed(self, section, name, detail):
        self.add(section, PASS, name, detail)

    def warn(self, section, name, detail, fix=None):
        self.add(section, WARN, name, detail, fix)

    def fail(self, section, name, detail, fix=None):
        self.add(section, FAIL, name, detail, fix)

    def info(self, section, name, detail):
        self.add(section, INFO, name, detail)

    def blind(self, section, name, detail, fix=None):
        """The audit could not see this. Distinct from INFO on purpose: a
        finding nobody could read is not context, it is a hole in the audit,
        and it must make the exit code non-zero."""
        self.add(section, BLIND, name, detail, fix)

    def counts(self):
        totals = {status: 0 for status in STATUSES}
        for row in self.findings:
            totals[row["status"]] += 1
        return totals

    def render(self):
        marks = {PASS: "  ok  ", WARN: " WARN ", FAIL: " FAIL ",
                 INFO: " info ", BLIND: " BLIND"}
        current = None
        for row in self.findings:
            if row["section"] != current:
                current = row["section"]
                print()
                print(f"── {current.upper()} " + "─" * (66 - len(current)))
            print(f"[{marks[row['status']]}] {row['name']}")
            print(f"          {row['detail']}")
            if row["fix"]:
                print(f"          fix: {row['fix']}")
        totals = self.counts()
        print()
        print("─" * 72)
        print(f"  {totals[PASS]} pass · {totals[WARN]} warn · "
              f"{totals[FAIL]} fail · {totals[INFO]} info · "
              f"{totals[BLIND]} blind")


# ---------------------------------------------------------------------------
# config / clients
# ---------------------------------------------------------------------------

def read_config(path):
    """Parse the key=value django.conf. Comments and bad lines are skipped."""
    config = {}
    try:
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip().strip('"').strip("'")
    except OSError as err:
        print(f"cannot read {path}: {err}", file=sys.stderr)
        sys.exit(2)
    return config


def build_session(config, profile):
    import boto3

    region = config.get("AWS_REGION", "us-east-1")
    if profile:
        return boto3.Session(profile_name=profile, region_name=region)
    key = config.get("AWS_KEY")
    secret = config.get("AWS_SECRET")
    if key and secret:
        return boto3.Session(
            aws_access_key_id=key, aws_secret_access_key=secret,
            region_name=region)
    if key or secret:
        # Deliberately NOT the PartialCredentialsError that
        # mojo/helpers/aws/client.py raises: an audit that refuses to run is
        # less useful than one that falls through and says so.
        print("warning: only one of AWS_KEY/AWS_SECRET is set — ignoring it "
              "and using the ambient credential chain", file=sys.stderr)
    # No static keys is the GOOD end state — fall through to the instance role
    # or whatever the ambient chain provides.
    return boto3.Session(region_name=region)


def list_paginated(client, method, result_key, token_field="NextToken", **params):
    """Follow every page. Same shape as
    mojo/apps/aws/services/aws_check.py::_list_paginated, with the token field
    named because AWS is not consistent about it (NextToken, Marker,
    ContinuationToken, and lowercase nextToken in CloudWatch Logs).

    Without this, every listing check here measured only the FIRST PAGE of the
    account, so a PASS meant "nothing bad in the first hundred resources"."""
    rows, token = [], None
    while True:
        request = dict(params)
        if token:
            request[token_field] = token
        response = getattr(client, method)(**request)
        rows.extend(response.get(result_key) or [])
        token = response.get(token_field)
        if not token:
            return rows


def safe(report, section, name, func, default=None):
    """Run an AWS call, turning a permission or throttling failure into a BLIND
    finding instead of a traceback. An audit that dies on the first permission
    gap tells you nothing about the other twenty checks — but one that quietly
    downgrades AccessDenied to a note and still exits 0 is worse, because it
    reports a clean section it never read."""
    from botocore.exceptions import (BotoCoreError, ClientError,
                                     EndpointConnectionError,
                                     NoCredentialsError,
                                     PartialCredentialsError)

    try:
        return func()
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code", "")
        if code in DENIED_CODES:
            report.blind(section, f"{name}: not permitted",
                         f"the audit credential cannot call this ({code}), so "
                         "this check was not performed",
                         "grant the audit credential read access, or run with "
                         "--section to skip areas it is not meant to see")
        elif code in THROTTLE_CODES:
            report.blind(section, f"{name}: throttled",
                         f"AWS throttled this call ({code}), so this check was "
                         "not performed",
                         "re-run the audit")
        elif code in UNUSABLE_CODES:
            report.blind(section, f"{name}: credential unusable",
                         f"the audit credential was rejected ({code}), so this "
                         "check was not performed",
                         "check that the audit credential is active and not "
                         "expired, then re-run")
        else:
            report.info(section, f"{name}: error", str(err))
        return default
    except BotoCoreError as err:
        # Not every transport error means the check could not run -- a timeout
        # is worth retrying, not gating on. But a missing credential or an
        # unreachable endpoint means this section was never read, and reporting
        # that as INFO would exit 0 on an audit that saw nothing.
        if isinstance(err, (NoCredentialsError, PartialCredentialsError,
                            EndpointConnectionError)):
            report.blind(section, f"{name}: credential or endpoint unavailable",
                         f"{type(err).__name__}, so this check was not performed",
                         "confirm the audit credential and network reachability, "
                         "then re-run")
        else:
            report.info(section, f"{name}: error", str(err))
        return default


# ---------------------------------------------------------------------------
# account
# ---------------------------------------------------------------------------

def check_account(report, session, config):
    sts = session.client("sts")
    who = safe(report, "account", "sts.get_caller_identity",
               lambda: sts.get_caller_identity())
    if not who:
        report.fail("account", "credentials",
                    "could not resolve the caller identity — the audit is blind",
                    "check AWS_KEY/AWS_SECRET in django.conf, or pass --profile")
        return None
    arn = who["Arn"]
    report.info("account", "identity",
                f"{arn} in account {who['Account']} "
                f"({session.region_name})")

    if config.get("AWS_KEY") and config.get("AWS_SECRET"):
        report.fail(
            "account", "static credentials in django.conf",
            "AWS_KEY/AWS_SECRET are stored in plaintext in var/django.conf. "
            "Any file-read bug, backup, or stolen laptop hands over whatever "
            "this key can do.",
            "attach an IAM instance profile scoped to what the app needs, "
            "delete AWS_KEY/AWS_SECRET from django.conf, then deactivate the key")
    else:
        report.passed("account", "credentials",
                      "no static keys in django.conf — using a role")
    return who


# ---------------------------------------------------------------------------
# ec2
# ---------------------------------------------------------------------------

def check_ec2(report, session, topology):
    ec2 = session.client("ec2")
    reservations = safe(
        report, "ec2", "describe_instances",
        lambda: list_paginated(
            ec2, "describe_instances", "Reservations",
            Filters=[{"Name": "instance-state-name",
                      "Values": ["running", "stopped"]}]))
    if reservations is None:
        return []

    instances = []
    for reservation in reservations:
        instances.extend(reservation.get("Instances", []))

    if not instances:
        # "No EC2" is only a defect against the reference topology. A dev
        # account, a Lambda-only account, or one mid-setup is legitimately
        # empty, and failing those from framework code is how an audit tool
        # teaches everyone to ignore it.
        if topology:
            report.fail("ec2", "no instances",
                        "the reference topology expects a web node and at "
                        "least one API node, and the account has neither")
        else:
            report.info("ec2", "no instances", "the account has no EC2 instances")
        return []

    running = [i for i in instances if i["State"]["Name"] == "running"]
    azs = {i["Placement"]["AvailabilityZone"] for i in running}

    report.info("ec2", "fleet",
                f"{len(running)} running instance(s) across {len(azs)} AZ(s): "
                f"{', '.join(sorted(azs))}")

    # TOPOLOGY. The reference shape is web + N api nodes across 2+ AZs. A
    # single-node deployment is a legitimate choice, so this is only a finding
    # when the operator has said that is the shape they are aiming for.
    if topology:
        if len(running) == 1:
            report.fail(
                "ec2", "single node",
                "one EC2 instance serves everything — web, API, websockets and "
                "cron. An instance failure, an AZ event, or a bad deploy is a "
                "full outage with no second node to fail over to.",
                "split web from API and run 2+ API nodes behind a load balancer, "
                "one per AZ")
        elif len(azs) < 2:
            report.fail(
                "ec2", "single AZ",
                f"{len(running)} instances but all in {sorted(azs)[0]} — an AZ "
                "event takes the whole fleet",
                "spread the API nodes across at least two AZs")
        else:
            report.passed("ec2", "fleet spread",
                          f"{len(running)} nodes across {len(azs)} AZs")

    for inst in running:
        name = tag_of(inst, "Name") or inst["InstanceId"]
        itype = inst["InstanceType"]

        if not inst.get("IamInstanceProfile"):
            report.fail(
                "ec2", f"{name}: no IAM instance profile",
                "the box has no role, so anything it does in AWS must use a "
                "static key from disk",
                "create a least-privilege role, attach it, then strip the keys")
        else:
            report.passed("ec2", f"{name}: instance profile",
                          inst["IamInstanceProfile"]["Arn"].split("/")[-1])

        tokens = inst.get("MetadataOptions", {}).get("HttpTokens")
        if tokens == "required":
            report.passed("ec2", f"{name}: IMDSv2",
                          "IMDSv2 required — SSRF cannot read instance credentials")
        else:
            report.fail(
                "ec2", f"{name}: IMDSv1 allowed",
                "the metadata service answers unauthenticated requests, so any "
                "SSRF in the app can read instance credentials",
                f"aws ec2 modify-instance-metadata-options --instance-id "
                f"{inst['InstanceId']} --http-tokens required "
                f"--http-endpoint enabled")

        if itype.startswith(LEGACY_PREFIXES):
            report.warn(
                "ec2", f"{name}: previous-generation {itype}",
                "t2/m4-class instances cost the same or more than the current "
                "generation for less CPU and worse network",
                "resize to the t3/m6i equivalent")
        elif itype.startswith(BURSTABLE_PREFIXES):
            report.warn(
                "ec2", f"{name}: burstable {itype}",
                "when CPU credits run out the node gets throttled rather than "
                "failing — a slow, hard-to-diagnose brownout under sustained load",
                "either move to a non-burstable family or set unlimited mode "
                "and alarm on CPUCreditBalance")
        else:
            report.passed("ec2", f"{name}: instance type", itype)

    return running


def tag_of(resource, key):
    for tag in resource.get("Tags", []) or []:
        if tag["Key"] == key:
            return tag["Value"]
    return None


# ---------------------------------------------------------------------------
# security groups
# ---------------------------------------------------------------------------

# Ports that are fine to expose to the world, because that is the point.
PUBLIC_OK = {80, 443}

# Ports that must never be world-reachable. Value is what an exposure means.
SENSITIVE = {
    22: "SSH",
    3306: "MySQL",
    5432: "PostgreSQL",
    6379: "Redis/Valkey",
    27017: "MongoDB",
    9200: "Elasticsearch",
    11211: "memcached",
}


def check_security_groups(report, session):
    ec2 = session.client("ec2")
    groups = safe(report, "sg", "describe_security_groups",
                  lambda: list_paginated(ec2, "describe_security_groups",
                                         "SecurityGroups"))
    if groups is None:
        return

    for group in groups:
        label = f"{group['GroupName']} ({group['GroupId']})"
        world_ports = []
        cidr_backed = []

        for rule in group.get("IpPermissions", []):
            open_to_world = any(
                r.get("CidrIp") == "0.0.0.0/0"
                for r in rule.get("IpRanges", []))
            open_to_world = open_to_world or any(
                r.get("CidrIpv6") == "::/0"
                for r in rule.get("Ipv6Ranges", []))
            from_port = rule.get("FromPort")
            to_port = rule.get("ToPort")

            if open_to_world:
                if from_port is None:
                    world_ports.append("ALL")
                else:
                    world_ports.extend(range(from_port, to_port + 1))
            elif rule.get("IpRanges") and not rule.get("UserIdGroupPairs"):
                cidr_backed.append(f"{from_port}-{to_port}")

        exposed_sensitive = [
            (p, SENSITIVE[p]) for p in world_ports
            if isinstance(p, int) and p in SENSITIVE]
        exposed_other = [
            p for p in world_ports
            if p == "ALL" or (isinstance(p, int)
                              and p not in PUBLIC_OK and p not in SENSITIVE)]

        for port, service in exposed_sensitive:
            report.fail(
                "sg", f"{label}: {service} open to the internet",
                f"port {port} accepts 0.0.0.0/0 — every scanner on the "
                f"internet reaches {service} on this tier",
                "restrict to your office/VPN CIDR, or move to SSM Session "
                "Manager and close 22 entirely")

        if exposed_other:
            report.fail(
                "sg", f"{label}: unexpected ports open to the internet",
                f"world-reachable: {exposed_other}",
                "narrow to 80/443 and reach everything else over the VPC")

        if not exposed_sensitive and not exposed_other:
            served = sorted(p for p in world_ports if isinstance(p, int))
            if served:
                report.passed("sg", f"{label}: public surface",
                              f"only {served} open to the internet")
            else:
                report.passed("sg", f"{label}: not internet-facing",
                              "no 0.0.0.0/0 ingress")

        if cidr_backed:
            report.warn(
                "sg", f"{label}: CIDR-based internal rules",
                f"rules {cidr_backed} allow by IP range rather than by source "
                "security group, so they keep working when an instance is "
                "replaced by something else at the same address",
                "reference the source security group id instead of a CIDR")


# ---------------------------------------------------------------------------
# load balancer
# ---------------------------------------------------------------------------

def check_load_balancer(report, session, running_instances):
    elbv2 = session.client("elbv2")
    data = safe(report, "lb", "describe_load_balancers",
                lambda: elbv2.describe_load_balancers())
    balancers = (data or {}).get("LoadBalancers", [])

    if not balancers:
        report.fail(
            "lb", "no load balancer",
            "traffic reaches the instance directly, so there is no health "
            "check, no way to drain a node for deploy, and no path to a "
            "second API node without a DNS change and its TTL",
            "put a network (or application) load balancer in front of the API "
            "tier and point the DNS record at it")
        return

    for balancer in balancers:
        name = balancer["LoadBalancerName"]
        kind = balancer["Type"]
        azs = [z["ZoneName"] for z in balancer.get("AvailabilityZones", [])]
        report.info("lb", f"{name}: {kind}",
                    f"{balancer['Scheme']} across {', '.join(sorted(azs))}")

        if len(azs) < 2:
            report.fail("lb", f"{name}: single AZ",
                        "a load balancer in one AZ cannot survive an AZ event",
                        "enable at least two subnets in different AZs")
        else:
            report.passed("lb", f"{name}: AZ coverage", f"{len(azs)} AZs")

        attrs = safe(report, "lb", f"{name} attributes",
                     lambda: elbv2.describe_load_balancer_attributes(
                         LoadBalancerArn=balancer["LoadBalancerArn"]))
        settings = {a["Key"]: a["Value"] for a in (attrs or {}).get("Attributes", [])}
        if settings.get("deletion_protection.enabled") != "true":
            report.warn("lb", f"{name}: no deletion protection",
                        "a stray API call or terraform destroy removes it",
                        "enable deletion_protection.enabled")
        if settings.get("access_logs.s3.enabled") != "true":
            report.warn("lb", f"{name}: access logs off",
                        "no record of what the LB saw when something breaks",
                        "enable access_logs.s3.enabled to a lifecycle-managed bucket")

    groups = safe(report, "lb", "describe_target_groups",
                  lambda: elbv2.describe_target_groups())
    for group in (groups or {}).get("TargetGroups", []):
        name = group["TargetGroupName"]
        health = safe(report, "lb", f"{name} health",
                      lambda: elbv2.describe_target_health(
                          TargetGroupArn=group["TargetGroupArn"]))
        targets = (health or {}).get("TargetHealthDescriptions", [])
        healthy = [t for t in targets
                   if t["TargetHealth"]["State"] == "healthy"]
        if not targets:
            report.fail("lb", f"{name}: no targets",
                        "the target group is empty — nothing is being served")
        elif len(healthy) < 2:
            report.fail(
                "lb", f"{name}: {len(healthy)}/{len(targets)} healthy",
                "fewer than two healthy targets means no redundancy; losing "
                "one is an outage",
                "register a second node in another AZ")
        else:
            report.passed("lb", f"{name}: targets",
                          f"{len(healthy)}/{len(targets)} healthy")


# ---------------------------------------------------------------------------
# rds
# ---------------------------------------------------------------------------

def check_rds(report, session, topology):
    rds = session.client("rds")

    clusters = safe(report, "rds", "describe_db_clusters",
                    lambda: rds.describe_db_clusters())
    for cluster in (clusters or {}).get("DBClusters", []):
        name = cluster["DBClusterIdentifier"]
        members = cluster.get("DBClusterMembers", [])
        report.info("rds", f"{name}: cluster",
                    f"{cluster['Engine']} {cluster.get('EngineVersion','?')}, "
                    f"{len(members)} instance(s)")

        # TOPOLOGY. Plenty of deployments run a deliberately single-instance
        # cluster; only the reference shape promises a reader.
        if topology:
            if len(members) < 2:
                report.fail(
                    "rds", f"{name}: no reader",
                    "a single-instance Aurora cluster has no standby: a failure "
                    "means a full restore, and every read competes with writes",
                    "add a reader instance in a second AZ — Aurora promotes it "
                    "automatically, typically in under a minute")
            else:
                report.passed("rds", f"{name}: instances",
                              f"{len(members)} members")

        if not cluster.get("StorageEncrypted"):
            report.fail(
                "rds", f"{name}: storage not encrypted",
                "the cluster volume, its snapshots, and its backups are all "
                "unencrypted at rest. For anything holding regulated personal "
                "or financial data this fails most compliance baselines "
                "outright.",
                "encryption cannot be turned on in place — snapshot, copy the "
                "snapshot with a KMS key, restore, and cut over")
        else:
            report.passed("rds", f"{name}: encryption",
                          f"encrypted with {cluster.get('KmsKeyId','a KMS key')}")

        if not cluster.get("DeletionProtection"):
            report.fail("rds", f"{name}: no deletion protection",
                        "one API call deletes the production database",
                        f"aws rds modify-db-cluster --db-cluster-identifier "
                        f"{name} --deletion-protection --apply-immediately")
        else:
            report.passed("rds", f"{name}: deletion protection", "enabled")

        # TOPOLOGY for the <7d FAIL only. Below that threshold without the
        # opt-in the finding still lands, as the ordinary "shorter than 14 days"
        # warning — the signal survives, the deploy gate does not fire.
        retention = cluster.get("BackupRetentionPeriod", 0)
        if retention < 7 and topology:
            report.fail("rds", f"{name}: backup retention {retention}d",
                        "too short to survive a problem discovered on a Monday",
                        "set retention to at least 14 days")
        elif retention < 14:
            report.warn(
                "rds", f"{name}: backup retention {retention}d",
                "7 days covers an incident you notice immediately; it does not "
                "cover slow corruption or a bad migration found a week later",
                "raise to 14-35 days — Aurora backup storage is cheap")
        else:
            report.passed("rds", f"{name}: backup retention", f"{retention} days")

        exports = cluster.get("EnabledCloudwatchLogsExports") or []
        if not exports:
            report.warn(
                "rds", f"{name}: no log exports",
                "postgresql logs stay on the instance, so slow queries and "
                "connection errors are invisible once it is replaced",
                "enable the postgresql log export to CloudWatch Logs")
        else:
            report.passed("rds", f"{name}: log exports", ", ".join(exports))

    instances = safe(report, "rds", "describe_db_instances",
                     lambda: rds.describe_db_instances())
    for inst in (instances or {}).get("DBInstances", []):
        name = inst["DBInstanceIdentifier"]
        klass = inst["DBInstanceClass"]

        if inst.get("PubliclyAccessible"):
            report.fail("rds", f"{name}: publicly accessible",
                        "the database has a public endpoint",
                        "set --no-publicly-accessible and reach it over the VPC")
        else:
            report.passed("rds", f"{name}: not public", "VPC-only endpoint")

        if klass.replace("db.", "").startswith(BURSTABLE_PREFIXES):
            report.warn(
                "rds", f"{name}: burstable {klass}",
                "the database throttles instead of failing when credits run "
                "out, which surfaces as slow API responses with no obvious cause",
                "move to db.r6g/db.m6g, or alarm on CPUCreditBalance")
        else:
            report.passed("rds", f"{name}: instance class", klass)

        if not inst.get("PerformanceInsightsEnabled"):
            report.warn("rds", f"{name}: Performance Insights off",
                        "no query-level history when the database gets slow",
                        "enable Performance Insights (7 days is free)")
        else:
            report.passed("rds", f"{name}: Performance Insights", "enabled")


# ---------------------------------------------------------------------------
# elasticache
# ---------------------------------------------------------------------------

def check_cache(report, session):
    cache = session.client("elasticache")
    data = safe(report, "cache", "describe_replication_groups",
                lambda: cache.describe_replication_groups())
    groups = (data or {}).get("ReplicationGroups", [])

    if not groups:
        clusters = safe(report, "cache", "describe_cache_clusters",
                        lambda: cache.describe_cache_clusters())
        standalone = (clusters or {}).get("CacheClusters", [])
        if standalone:
            report.fail(
                "cache", "standalone cache nodes",
                f"{len(standalone)} node(s) with no replication group, so "
                "there is no failover and no reader endpoint",
                "rebuild as a replication group with automatic failover")
        else:
            report.warn("cache", "no cache",
                        "no ElastiCache found — websockets, sessions and the "
                        "job queue need Redis/Valkey in this stack")
        return

    for group in groups:
        name = group["ReplicationGroupId"]
        members = group.get("MemberClusters", [])
        report.info("cache", f"{name}: replication group",
                    f"{len(members)} node(s), engine {group.get('Engine','?')}")

        if group.get("AutomaticFailover") == "enabled":
            report.passed("cache", f"{name}: automatic failover", "enabled")
        else:
            report.fail("cache", f"{name}: no automatic failover",
                        "losing the primary means manual intervention",
                        "enable automatic failover (needs 2+ nodes)")

        if group.get("MultiAZ") == "enabled":
            report.passed("cache", f"{name}: Multi-AZ", "enabled")
        else:
            report.fail("cache", f"{name}: not Multi-AZ",
                        "all nodes share one AZ's fate",
                        "enable Multi-AZ and place replicas in other AZs")

        if group.get("AtRestEncryptionEnabled"):
            report.passed("cache", f"{name}: encryption at rest", "enabled")
        else:
            report.fail("cache", f"{name}: no encryption at rest",
                        "session tokens and cached personal data sit in the clear",
                        "at-rest encryption cannot be enabled in place — "
                        "recreate the group with it on")

        if group.get("TransitEncryptionEnabled"):
            report.passed("cache", f"{name}: encryption in transit",
                          group.get("TransitEncryptionMode", "enabled"))
        else:
            report.fail("cache", f"{name}: no TLS",
                        "cache traffic crosses the VPC unencrypted",
                        "enable transit encryption")

        if group.get("AuthTokenEnabled"):
            report.passed("cache", f"{name}: AUTH", "token required")
        else:
            report.warn(
                "cache", f"{name}: no AUTH token",
                "the security group is the only thing between anything in the "
                "VPC and a full FLUSHALL — there is no second factor",
                "enable a Valkey/Redis AUTH token (or RBAC user groups) so an "
                "SG mistake is not immediately a data-loss event")

        snapshots = group.get("SnapshotRetentionLimit", 0)
        if not snapshots:
            report.warn("cache", f"{name}: no snapshots",
                        "nothing to restore from if the group is lost",
                        "set a snapshot retention of a few days")


# ---------------------------------------------------------------------------
# observability
#
# DELIBERATELY A DELTA. mojo.apps.aws.services.aws_check.check_monitoring
# already owns SNS topic existence and the alarm inventory, discovers them
# account-wide, and can create what is missing. Duplicating that here would
# produce two tools disagreeing about the same account. What is left is what
# aws_check does NOT look at: subscriptions that were never confirmed, alarms
# that never receive their metric, and whether application logs leave the box.
# ---------------------------------------------------------------------------

def check_observability(report, session, topology):
    cloudwatch = session.client("cloudwatch")
    sns = session.client("sns")
    logs = session.client("logs")

    topics = safe(report, "observability", "list_topics",
                  lambda: list_paginated(sns, "list_topics", "Topics"))
    for topic in topics or []:
        arn = topic["TopicArn"]
        short = arn.split(":")[-1]
        entries = safe(report, "observability", f"{short}: list_subscriptions",
                       lambda a=arn: list_paginated(
                           sns, "list_subscriptions_by_topic", "Subscriptions",
                           TopicArn=a))
        pending = [s for s in entries or []
                   if s.get("SubscriptionArn") == "PendingConfirmation"]
        if pending:
            report.fail("observability", f"{short}: unconfirmed subscription",
                        f"{len(pending)} subscription(s) never confirmed — "
                        "those endpoints receive nothing",
                        "confirm the subscription; the mojo endpoint does "
                        "this automatically when it is reachable")

    alarms = safe(report, "observability", "describe_alarms",
                  lambda: list_paginated(cloudwatch, "describe_alarms",
                                         "MetricAlarms"))
    insufficient = [a["AlarmName"] for a in alarms or []
                    if a.get("StateValue") == "INSUFFICIENT_DATA"]
    if insufficient:
        report.warn(
            "observability", "alarms with no data",
            f"{len(insufficient)} alarm(s) never receive their metric, so "
            f"they will never fire: {', '.join(insufficient[:5])}",
            "check the metric namespace and dimensions")

    groups = safe(report, "observability", "describe_log_groups",
                  lambda: list_paginated(logs, "describe_log_groups",
                                         "logGroups", token_field="nextToken"))
    log_groups = groups or []
    app_groups = [g for g in log_groups
                  if not g["logGroupName"].startswith(("RDSOSMetrics", "/aws/rds"))]

    # TOPOLOGY. Shipping application logs off the box is part of the reference
    # shape; a project that centralises logs some other way is not broken.
    if not app_groups:
        if topology:
            report.fail(
                "observability", "no application logs in CloudWatch",
                "nginx and Django logs live only on the instance's local disk. "
                "Replace or lose the box and the evidence goes with it — which "
                "is exactly when you need it.",
                "install the CloudWatch agent and ship /var/log/nginx/*.log and "
                "/opt/api/var/logs/*.log")
    else:
        report.passed("observability", "application logs",
                      f"{len(app_groups)} log group(s) shipping")
        forever = [g["logGroupName"] for g in app_groups
                   if not g.get("retentionInDays")]
        if forever:
            report.warn(
                "observability", "log groups never expire",
                f"{len(forever)} group(s) retain forever and bill forever",
                "set a retention period (30-90 days is usually right)")


# ---------------------------------------------------------------------------
# backup / audit trail
# ---------------------------------------------------------------------------

def check_backup(report, session, topology):
    ec2 = session.client("ec2")
    backup = session.client("backup")
    trail = session.client("cloudtrail")
    guard = session.client("guardduty")

    vols = safe(report, "backup", "describe_volumes",
                lambda: list_paginated(ec2, "describe_volumes", "Volumes"))
    vols = vols or []
    unencrypted = [v["VolumeId"] for v in vols if not v.get("Encrypted")]
    if unencrypted:
        report.fail(
            "backup", "unencrypted EBS volumes",
            f"{len(unencrypted)} volume(s) unencrypted: "
            f"{', '.join(unencrypted[:5])}. These hold the app, its config "
            "with every downstream secret, and the TLS private keys.",
            "snapshot, copy the snapshot with encryption enabled, restore, "
            "swap the volume — and turn on EBS encryption by default so new "
            "volumes are never created unencrypted")
    elif vols:
        report.passed("backup", "EBS encryption",
                      f"all {len(vols)} volume(s) encrypted")

    default_enc = safe(report, "backup", "get_ebs_encryption_by_default",
                       lambda: ec2.get_ebs_encryption_by_default())
    if default_enc is not None:
        if default_enc.get("EbsEncryptionByDefault"):
            report.passed("backup", "EBS encryption by default", "enabled")
        else:
            report.fail(
                "backup", "EBS encryption by default is off",
                "every volume created from now on is unencrypted unless "
                "someone remembers to tick the box",
                "aws ec2 enable-ebs-encryption-by-default")

    # TOPOLOGY. A node that is rebuilt entirely from an AMI + config_sync does
    # not need snapshots of its root volume; the reference shape does.
    if topology:
        snaps = safe(report, "backup", "describe_snapshots",
                     lambda: list_paginated(ec2, "describe_snapshots",
                                            "Snapshots", OwnerIds=["self"]))
        if snaps is not None and not snaps:
            report.fail(
                "backup", "no EBS snapshots",
                "the root volume has never been snapshotted. Nginx configs, "
                "the TLS lineage, cron and django.conf exist in exactly one "
                "place — rebuilding is from memory.",
                "add an AWS Backup plan, or a DLM policy taking a daily "
                "snapshot with 7-14 day retention")

    plans = safe(report, "backup", "list_backup_plans",
                 lambda: backup.list_backup_plans())
    if not (plans or {}).get("BackupPlansList"):
        report.warn(
            "backup", "no AWS Backup plan",
            "backups depend on each service's own settings rather than one "
            "policy you can point at during an audit",
            "create a backup plan covering EBS and RDS with a retention rule")
    else:
        report.passed("backup", "AWS Backup",
                      f"{len(plans['BackupPlansList'])} plan(s)")

    trails = safe(report, "backup", "describe_trails",
                  lambda: trail.describe_trails())
    if not (trails or {}).get("trailList"):
        report.fail(
            "backup", "CloudTrail not enabled",
            "there is no record of who did what in this account. After an "
            "incident you cannot answer what was touched or when, and for a "
            "regulated operator that gap is itself a finding.",
            "create a multi-region trail to an S3 bucket with object lock")
    else:
        multi = [t for t in trails["trailList"] if t.get("IsMultiRegionTrail")]
        if multi:
            report.passed("backup", "CloudTrail",
                          f"{len(multi)} multi-region trail(s)")
        else:
            report.warn("backup", "CloudTrail is single-region",
                        "activity in other regions is unrecorded",
                        "make the trail multi-region")

    detectors = safe(report, "backup", "list_detectors",
                     lambda: guard.list_detectors())
    if not (detectors or {}).get("DetectorIds"):
        report.warn(
            "backup", "GuardDuty not enabled",
            "no managed detection for credential misuse, crypto mining or "
            "traffic to known-bad hosts — the failure modes a stolen key "
            "produces first",
            "enable GuardDuty; it is a few dollars a month at this size")
    else:
        report.passed("backup", "GuardDuty", "enabled")


# ---------------------------------------------------------------------------
# s3
#
# The public-access-block check here is account-wide, across every bucket, and
# a FAIL. aws_check looks at exactly one bucket — the one FileManager is
# configured with — and warns. The account-wide sweep is the differentiator, so
# it stays.
# ---------------------------------------------------------------------------

def check_s3(report, session, config):
    s3 = session.client("s3")
    listing = safe(report, "s3", "list_buckets",
                   lambda: list_paginated(s3, "list_buckets", "Buckets",
                                          token_field="ContinuationToken"))
    if listing is None:
        return
    buckets = [b["Name"] for b in listing]
    cert_bucket = config.get("AWS_CERT_BUCKET")

    for name in buckets:
        block = safe(report, "s3", f"{name} public access block",
                     lambda n=name: s3.get_public_access_block(Bucket=n))
        settings = (block or {}).get("PublicAccessBlockConfiguration", {})
        if all(settings.get(k) for k in ("BlockPublicAcls", "IgnorePublicAcls",
                                         "BlockPublicPolicy",
                                         "RestrictPublicBuckets")):
            report.passed("s3", f"{name}: public access block", "fully enabled")
        else:
            off = [k for k in ("BlockPublicAcls", "IgnorePublicAcls",
                               "BlockPublicPolicy", "RestrictPublicBuckets")
                   if not settings.get(k)]
            report.fail(
                "s3", f"{name}: public access block incomplete",
                f"not blocked: {', '.join(off)}. Nothing stops a future policy "
                "or ACL from exposing the bucket, and nothing warns you when "
                "one does.",
                "enable all four, then carve out public objects via CloudFront "
                "rather than by leaving the bucket open")

        policy = safe(report, "s3", f"{name} policy",
                      lambda n=name: s3.get_bucket_policy(Bucket=n))
        if policy:
            document = json.loads(policy["Policy"])
            public = [s for s in document.get("Statement", [])
                      if s.get("Effect") == "Allow"
                      and s.get("Principal") in ("*", {"AWS": "*"})]
            for statement in public:
                report.warn(
                    "s3", f"{name}: public-read policy",
                    f"anonymous s3:GetObject on {statement.get('Resource')} — "
                    "confirm no private material can ever land under that "
                    "prefix, since a wildcard matches more than it looks like",
                    "serve public assets through CloudFront with OAC and keep "
                    "the bucket itself private")

        versioning = safe(report, "s3", f"{name} versioning",
                          lambda n=name: s3.get_bucket_versioning(Bucket=n))
        if (versioning or {}).get("Status") == "Enabled":
            report.passed("s3", f"{name}: versioning", "enabled")
        else:
            report.warn(
                "s3", f"{name}: versioning off",
                "an overwrite or delete is unrecoverable"
                + (" — and this bucket holds the shared TLS lineage"
                   if name == cert_bucket else ""),
                "enable versioning with a lifecycle rule to expire old versions")

        encryption = safe(report, "s3", f"{name} encryption",
                          lambda n=name: s3.get_bucket_encryption(Bucket=n))
        if encryption:
            rules = encryption["ServerSideEncryptionConfiguration"]["Rules"]
            algo = rules[0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
            report.passed("s3", f"{name}: encryption", f"default {algo}")
        else:
            report.fail("s3", f"{name}: no default encryption",
                        "objects land unencrypted unless the client asks",
                        "set default SSE-S3 or SSE-KMS")

    if cert_bucket and cert_bucket not in buckets:
        report.fail("s3", "cert bucket missing",
                    f"AWS_CERT_BUCKET={cert_bucket} does not exist in this "
                    "account — the shared TLS lineage has nowhere to live",
                    "create the bucket or correct AWS_CERT_BUCKET")


# ---------------------------------------------------------------------------
# iam
# ---------------------------------------------------------------------------

def check_iam(report, session, identity):
    iam = session.client("iam")

    profiles = safe(report, "iam", "list_instance_profiles",
                    lambda: list_paginated(iam, "list_instance_profiles",
                                           "InstanceProfiles",
                                           token_field="Marker"))
    if profiles is not None:
        if not profiles:
            report.fail(
                "iam", "no instance profiles",
                "no role exists for an EC2 box to assume, which is why the "
                "instances fall back to static keys on disk",
                "create a role with a least-privilege policy and an "
                "ec2.amazonaws.com trust policy")
        else:
            report.passed("iam", "instance profiles",
                          f"{len(profiles)} available")

    if not identity or ":user/" not in identity.get("Arn", ""):
        # Not blind — genuinely not applicable. Say so rather than returning
        # silently, which used to make the whole per-user half of this section
        # vanish without a trace on exactly the role-based deployments the
        # reference topology recommends.
        report.info("iam", "per-user checks skipped",
                    "the audit identity is a role, not an IAM user, so "
                    "attached-policy, access-key-age and MFA checks do not "
                    "apply to it")
        return
    user = identity["Arn"].split("/")[-1]

    attached = safe(report, "iam", "list_attached_user_policies",
                    lambda: iam.list_attached_user_policies(UserName=user))
    for policy in (attached or {}).get("AttachedPolicies", []):
        if policy["PolicyName"] in ("AdministratorAccess", "PowerUserAccess"):
            report.fail(
                "iam", f"{user}: {policy['PolicyName']}",
                "the key the application uses can do anything in the account — "
                "delete the database, create users, disable logging. Whatever "
                "reads that config file inherits the whole account.",
                "replace with a policy granting only what the app needs "
                "(its S3 prefix, ses:SendEmail, cloudwatch:PutMetricData)")
        else:
            report.info("iam", f"{user}: attached policy", policy["PolicyName"])

    keys = safe(report, "iam", "list_access_keys",
                lambda: iam.list_access_keys(UserName=user))
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    for key in (keys or {}).get("AccessKeyMetadata", []):
        if key["Status"] != "Active":
            continue
        age = (now - key["CreateDate"]).days
        if age > 365:
            report.fail("iam", f"{user}: key {age} days old",
                        f"{key['AccessKeyId'][:8]}… has never been rotated",
                        "rotate, or remove it once the instance role is in place")
        elif age > 90:
            report.warn("iam", f"{user}: key {age} days old",
                        f"{key['AccessKeyId'][:8]}… is past a 90-day rotation window",
                        "rotate the key")
        else:
            report.passed("iam", f"{user}: key age", f"{age} days")

    mfa = safe(report, "iam", "list_mfa_devices",
               lambda: iam.list_mfa_devices(UserName=user))
    if mfa is not None and not mfa.get("MFADevices"):
        report.warn("iam", f"{user}: no MFA",
                    "if this user also has console access, a password is the "
                    "only thing protecting the account",
                    "attach an MFA device, or confirm the user is "
                    "programmatic-only with no login profile")


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def topology_enabled(requested, config):
    """--topology wins; MOJO_DEPLOY_TOPOLOGY in django.conf is the persistent
    way to say the same thing. Default off."""
    value = requested or config.get("MOJO_DEPLOY_TOPOLOGY") or "none"
    return str(value).strip().lower() == "reference"


def main(argv, *, session_factory=None):
    # session_factory is a test seam: it must build (or fake) the boto3
    # session from (config, profile). None keeps the real build_session.
    if session_factory is None:
        session_factory = build_session
    parser = argparse.ArgumentParser(
        prog="python3 -m mojo.deploy.check_setup",
        description="Audit an AWS account for django-mojo deployment gaps")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help=f"path to django.conf (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--profile",
                        help="use this ~/.aws profile instead of django.conf keys")
    parser.add_argument("--section", default="all",
                        help=f"comma-separated subset of: {', '.join(SECTIONS)}")
    parser.add_argument("--topology", choices=TOPOLOGIES, default=None,
                        help="also assert the named deployment topology "
                             "(default: off, or MOJO_DEPLOY_TOPOLOGY)")
    parser.add_argument("--json", action="store_true",
                        help="emit findings as JSON")
    args = parser.parse_args(argv)

    # Section validation happens before anything touches AWS, so a typo costs
    # a usage message rather than a round of API calls.
    if args.section == "all":
        wanted = set(SECTIONS)
    else:
        wanted = {s.strip() for s in args.section.split(",")}
        unknown = wanted - set(SECTIONS)
        if unknown:
            parser.error(f"unknown section(s): {', '.join(sorted(unknown))}")

    config = read_config(args.config) if os.path.exists(args.config) else {}
    if not config and not args.profile:
        print(f"no config at {args.config} and no --profile given",
              file=sys.stderr)
        return 2

    topology = topology_enabled(args.topology, config)

    try:
        session = session_factory(config, args.profile)
    except ImportError:
        print("boto3 is not installed", file=sys.stderr)
        return 2

    report = Report()
    identity = check_account(report, session, config) if "account" in wanted else None

    instances = []
    if "ec2" in wanted:
        instances = check_ec2(report, session, topology)
    if "sg" in wanted:
        check_security_groups(report, session)
    if "lb" in wanted:
        check_load_balancer(report, session, instances)
    if "rds" in wanted:
        check_rds(report, session, topology)
    if "cache" in wanted:
        check_cache(report, session)
    if "observability" in wanted:
        check_observability(report, session, topology)
    if "backup" in wanted:
        check_backup(report, session, topology)
    if "s3" in wanted:
        check_s3(report, session, config)
    if "iam" in wanted:
        check_iam(report, session, identity)

    counts = report.counts()
    if args.json:
        print(json.dumps({
            "findings": report.findings,
            "counts": counts,
            "topology": "reference" if topology else "none",
        }, indent=2))
    else:
        report.render()

    # BLIND is non-zero on purpose. See the module docstring.
    return 1 if (counts[FAIL] or counts[BLIND]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
