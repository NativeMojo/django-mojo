"""
Provision, check, disable and rotate CDN delivery for an S3 bucket
(maestro #8250).

Same contract as `mojo/deploy/provision`: OBSERVE the account, then CONVERGE
it additively. Nothing here deletes anything, there are no waiters, and there
is no state file — resume is re-observation, so a second run creates nothing
and a run interrupted anywhere is finished by the next one. Every resource
has an identity the next run can find it by:

    signing key      a CdnSigningKey row saved FIRST, whose public_key_name
                     (`mojo-fm-key-<pk>`) is the CloudFront CallerReference
    key group        one per deployment, found by name
    OAC              found by name (a hash of the bucket; names cap at 64)
    distribution     found by OUR tag only. An untagged distribution on the
                     same bucket is never adopted — it is somebody else's.

Findings use the seven statuses of `mojo/deploy/provision/report.py`
(PASS / PENDING / MISSING / DRIFT / MANUAL / BLOCKED / BLIND) and the report
is the `aws-check --json` shape. Every AWS call names its IAM action
explicitly, so a denial tells the operator exactly what to grant.

Credentials: the DEPLOYMENT's identity (AWS_KEY/AWS_SECRET or the instance
role) by default — the same identity `aws-check --apply` uses — never a
tenant manager's upload credentials. Callers may inject `clients`.
"""

import hashlib
import json
import uuid
from datetime import timedelta
import re

from django.utils import timezone

from mojo.deploy.provision import report
from mojo.helpers import logit
from mojo.helpers.aws.provider_call import ProviderCallError, ProviderCaller
from mojo.helpers.settings import settings

from . import cdn


logger = logit.get_logger("fileman", "fileman.log")

REPORT_VERSION = 1
TAG_KEY = "mojo:fileman-bucket"
CACHING_OPTIMIZED_POLICY_ID = "658327ea-f89d-4fab-a63d-7e88639e58f6"
MAX_KEYS_PER_GROUP = 5
MAX_BEHAVIORS = 25          # CloudFront's default cache-behavior quota per distribution
KEY_GROUP_PAGE_LIMIT = 100    # 100 groups a page: far past the default 10-group quota

STEP_PREFLIGHT = "preflight"
STEP_BUCKET = "bucket"
STEP_KEY = "signing_key"
STEP_OAC = "oac"
STEP_DISTRIBUTION = "distribution"
STEP_POLICY = "bucket_policy"
STEP_KMS = "kms"
STEP_PERSIST = "persist"

PENDING_STATUSES = ("InProgress",)


# ----------------------------------------------------------------------
# naming
# ----------------------------------------------------------------------

def deployment_slug():
    """A stable, short identity for this deployment (the key group is one per
    deployment, and CloudFront allows ten per account)."""
    seed = settings.get("BASE_URL", None) or settings.get("PROJECT_NAME", None) or "django-mojo"
    return hashlib.sha1(str(seed).encode()).hexdigest()[:12]


def key_group_name():
    return f"mojo-fm-{deployment_slug()}"


def oac_name(bucket):
    return f"mojo-fm-oac-{hashlib.sha1(bucket.encode()).hexdigest()[:12]}"


def public_key_name(row_pk):
    """Account-wide unique: CloudFront public key names are shared by every
    deployment in the account, so the deployment slug is part of the name."""
    return f"mojo-fm-key-{deployment_slug()}-{int(row_pk)}"


def origin_domain(bucket, region):
    return f"{bucket}.s3.{region}.amazonaws.com"


def bucket_policy_sid(distribution_id):
    return f"AllowCloudFrontServicePrincipalReadOnly_{distribution_id}"


def kms_policy_sid(distribution_id):
    return f"AllowCloudFrontDecrypt_{distribution_id}"


def bucket_policy_statement(bucket, distribution_arn):
    return {
        "Sid": bucket_policy_sid(distribution_arn.rsplit("/", 1)[-1]),
        "Effect": "Allow",
        "Principal": {"Service": "cloudfront.amazonaws.com"},
        "Action": "s3:GetObject",
        "Resource": f"arn:aws:s3:::{bucket}/*",
        "Condition": {"StringEquals": {"AWS:SourceArn": distribution_arn}},
    }


def kms_policy_statement(distribution_arn):
    return {
        "Sid": kms_policy_sid(distribution_arn.rsplit("/", 1)[-1]),
        "Effect": "Allow",
        "Principal": {"Service": "cloudfront.amazonaws.com"},
        "Action": ["kms:Decrypt"],
        "Resource": "*",
        "Condition": {"StringEquals": {"AWS:SourceArn": distribution_arn}},
    }


def merged_policy(current, statement):
    """`current` (a policy dict or None) with `statement` added or replaced by Sid."""
    policy = {"Version": "2012-10-17", "Statement": []}
    if isinstance(current, dict):
        policy = {"Version": current.get("Version", "2012-10-17"),
                  "Statement": list(current.get("Statement") or [])}
        if isinstance(current.get("Statement"), dict):
            policy["Statement"] = [current["Statement"]]
    policy["Statement"] = [s for s in policy["Statement"] if s.get("Sid") != statement["Sid"]]
    policy["Statement"].append(statement)
    return policy


def _statements(policy):
    statements = (policy or {}).get("Statement") if isinstance(policy, dict) else None
    if isinstance(statements, dict):
        statements = [statements]
    return [st for st in (statements or []) if isinstance(st, dict)]


def _has_statement(policy, statement):
    for current in _statements(policy):
        if current.get("Sid") == statement["Sid"]:
            principal = current.get("Principal")
            principal = principal if isinstance(principal, dict) else {}
            condition = current.get("Condition") if isinstance(current.get("Condition"), dict) else {}
            equals = condition.get("StringEquals") if isinstance(condition.get("StringEquals"), dict) else {}
            if (current.get("Effect") == "Allow"
                    and principal.get("Service") == "cloudfront.amazonaws.com"
                    and equals.get("AWS:SourceArn") == statement["Condition"]["StringEquals"]["AWS:SourceArn"]):
                return True
    return False


def _is_anonymous_allow(statement):
    if statement.get("Effect") != "Allow":
        return False
    principal = statement.get("Principal")
    if principal == "*":
        return True
    if isinstance(principal, dict):
        aws = principal.get("AWS")
        return aws == "*" or (isinstance(aws, list) and "*" in aws)
    return False


# ----------------------------------------------------------------------
# distribution config
# ----------------------------------------------------------------------

class KeyGroupMissing(ValueError):
    """A signed behavior was asked for with no key group to trust."""


def _behavior(origin_id, key_group_id, signed, path_pattern=None, response_headers_policy_id=None):
    if signed and not key_group_id:
        # Never write a "signed" behavior that trusts nobody: CloudFront would
        # serve the prefix to anyone.
        raise KeyGroupMissing("a signed cache behavior needs the deployment key group")
    behavior = {
        "TargetOriginId": origin_id,
        "ViewerProtocolPolicy": "redirect-to-https",
        "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"],
                           "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
        "Compress": True,
        "CachePolicyId": CACHING_OPTIMIZED_POLICY_ID,
        "TrustedSigners": {"Enabled": False, "Quantity": 0},
        "TrustedKeyGroups": (
            {"Enabled": True, "Quantity": 1, "Items": [key_group_id]} if signed
            else {"Enabled": False, "Quantity": 0}),
    }
    if response_headers_policy_id:
        behavior["ResponseHeadersPolicyId"] = response_headers_policy_id
    if path_pattern:
        behavior["PathPattern"] = path_pattern
    return behavior


def plan_behaviors(managers, origin_id, key_group_id, response_headers_policy_id=None):
    """The cache behaviors a bucket needs, planned fail-closed.

    Every S3 manager on the bucket counts — active or not, opted out or not:
    its objects are in the bucket either way and the bucket policy grants
    CloudFront all of them. Where two managers share a prefix, signed wins.
    A prefix CloudFront would read as a wildcard (`*`, `?`) or that carries a
    character outside the path-pattern charset gets no behavior at all (it
    falls under the signed default) and is reported. A child prefix with
    the same signedness as its nearest ancestor is covered by the ancestor
    and gets no behavior of its own, which keeps the count under CloudFront's
    quota. A PRIVATE prefix under a PUBLIC ancestor is reported as nested:
    whether CloudFront matches the encoded or the decoded path decides if the
    longer signed pattern can be sidestepped, so it is refused until the live
    probe says otherwise.

    Every behavior carries `response_headers_policy_id` when one is given.

    Returns {"behaviors", "unsafe", "nested", "prefixes"}.
    """
    signed_by_prefix = {}
    unsafe = []
    for manager in managers:
        prefix = (manager.root_path or "").strip("/")
        if not prefix:
            continue
        if not cdn.PREFIX_SAFE_RE.match(prefix):
            unsafe.append((prefix, getattr(manager, "name", "")))
            continue
        signed_by_prefix[prefix] = signed_by_prefix.get(prefix, False) or not bool(manager.is_public)
    ordered = sorted(signed_by_prefix, key=lambda p: (p.count("/"), p))
    nested = []
    kept = {}
    for prefix in ordered:
        ancestor = None
        for candidate in kept:
            if prefix.startswith(candidate + "/") and (ancestor is None or len(candidate) > len(ancestor)):
                ancestor = candidate
        signed = signed_by_prefix[prefix]
        if ancestor is not None and kept[ancestor] == signed:
            continue
        if ancestor is not None and signed and not kept[ancestor]:
            nested.append((prefix, ancestor))
        kept[prefix] = signed
    rows = sorted(kept.items(), key=lambda row: (-len(row[0]), row[0]))
    behaviors = [_behavior(origin_id, key_group_id, signed=signed, path_pattern=f"{prefix}/*",
                           response_headers_policy_id=response_headers_policy_id)
                 for prefix, signed in rows]
    return {"behaviors": behaviors, "unsafe": unsafe, "nested": nested, "prefixes": dict(kept)}


def desired_behaviors(managers, origin_id, key_group_id):
    return plan_behaviors(managers, origin_id, key_group_id)["behaviors"]


def distribution_config(bucket, region, oac_id, key_group_id, behaviors, caller_reference,
                        enabled=True, current=None, response_headers_policy_id=None):
    """The full DistributionConfig we converge to. With `current`, fields we
    do not own (aliases, certificate, price class, logging...) are kept."""
    origin_id = f"s3-{bucket}"
    config = {
        "CallerReference": caller_reference,
        "Comment": f"django-mojo fileman {bucket}",
        "Enabled": bool(enabled),
        "Origins": {"Quantity": 1, "Items": [{
            "Id": origin_id,
            "DomainName": origin_domain(bucket, region),
            "OriginAccessControlId": oac_id,
            "S3OriginConfig": {"OriginAccessIdentity": ""},
        }]},
        "DefaultCacheBehavior": _behavior(origin_id, key_group_id, signed=True,
                                          response_headers_policy_id=response_headers_policy_id),
        "CacheBehaviors": {"Quantity": 0, "Items": []},
        "PriceClass": "PriceClass_All",
        "HttpVersion": "http2",
        "IsIPV6Enabled": True,
    }
    config["CacheBehaviors"] = {"Quantity": len(behaviors), "Items": list(behaviors)}
    if isinstance(current, dict):
        merged = dict(current)
        for key in ("Enabled", "Origins", "DefaultCacheBehavior", "CacheBehaviors"):
            merged[key] = config[key]
        merged.setdefault("Comment", config["Comment"])
        return merged
    return config


def _behaviors_match(current, desired):
    def shape(items):
        out = []
        for item in items or []:
            groups = item.get("TrustedKeyGroups") or {}
            out.append((item.get("PathPattern"), bool(groups.get("Enabled")),
                        tuple(sorted(groups.get("Items") or [])),
                        item.get("TargetOriginId"),
                        item.get("ResponseHeadersPolicyId") or None))
        return out
    return shape(current) == shape(desired)


# ----------------------------------------------------------------------
# the provisioner
# ----------------------------------------------------------------------

# `CdnProvisioner(response_headers_policy=...)` default: read the setting.
_FROM_SETTING = object()


class CdnProvisioner:
    """Observe and converge CDN delivery for one manager's bucket.

    `apply=False` is `check`: read-only, every gap reported with its exact
    remedy (the merged policy JSON where one is needed). `apply=True` is
    `enable`. `harden=True` additionally applies Block Public Access and
    Object Ownership, which `check` only reports because flipping them
    bucket-wide breaks every non-CDN public manager on the bucket.
    """

    def __init__(self, manager, apply=False, harden=False, clients=None, region=None,
                 timeout=3, secret_writer=None, key_loader=None, key_pem_factory=None,
                 now=None, session_factory=None, profile=None,
                 response_headers_policy=_FROM_SETTING, caller=None):
        self.manager = manager
        self.bucket = manager.root_location
        self.apply = bool(apply)
        self.harden = bool(harden)
        self.clients = dict(clients or {})
        self.region = region or settings.get("AWS_REGION", "us-east-1")
        self.timeout = timeout
        self.secret_writer = secret_writer or _store_private_key
        self.key_loader = key_loader or _load_private_key
        self.key_pem_factory = key_pem_factory or cdn.generate_private_key_pem
        self.now = now or timezone.now
        self.session_factory = session_factory
        self.profile = profile
        # The policy `enable` writes on a distribution fileman provisioned,
        # read from the setting only when `enable`/`check` first asks for it.
        # The visibility sync never asks: it copies the live default's.
        self._response_headers_policy = response_headers_policy
        self.caller = caller or ProviderCaller(logger)
        self.findings = []
        self.actions = []
        self.observed = {}
        self.blocked = set()

    @property
    def response_headers_policy(self):
        if self._response_headers_policy is _FROM_SETTING:
            self._response_headers_policy = cdn.response_headers_policy_id()
        return self._response_headers_policy or None

    # --- plumbing ------------------------------------------------------

    def client(self, service):
        client = self.clients.get(service)
        if client is None:
            from mojo.helpers.aws.client import get_client, get_session
            factory = self.session_factory or get_session
            session = factory(access_key=settings.get("AWS_KEY", None),
                              secret_key=settings.get("AWS_SECRET", None),
                              region=self.region, profile=self.profile)
            client = get_client(service, session=session, region=self.region,
                                timeout=self.timeout)
            self.clients[service] = client
        return client

    def _add(self, step, status, code, message, remedy=None):
        self.findings.append(report.Finding(step, status, code, message, remedy))

    def _call(self, step, operation, iam_action, func, mutation=False, default=None,
              not_found_codes=()):
        """Run one AWS call. A denial, throttle or outage is a BLIND finding
        that blocks the step; `not_found_codes` turn a missing resource into
        `default` without a finding."""
        try:
            return self.caller.call(operation, func, iam_action=iam_action, mutation=mutation)
        except ProviderCallError as error:
            if error.provider_code in not_found_codes:
                return default
            if error.denied:
                self._add(step, report.BLIND, f"{operation}.denied",
                          f"the deployment identity may not call {operation}",
                          f"grant {iam_action} to the deployment identity, then re-run")
            elif error.retryable:
                self._add(step, report.BLIND, f"{operation}.unavailable",
                          f"{operation} could not be completed ({error.provider_code})",
                          "re-run — the next pass picks up where this one stopped")
            else:
                self._add(step, report.BLIND, f"{operation}.error",
                          f"{operation} failed ({error.provider_code})",
                          "read the AWS error in the provider log, then re-run")
            self.blocked.add(step)
            return default

    def _blocked(self, step, *depends_on):
        for dependency in depends_on:
            if dependency in self.blocked:
                self._add(step, report.BLOCKED, f"{step}.blocked",
                          f"{step} did not run because {dependency} did not converge")
                self.blocked.add(step)
                return True
        return False

    # --- entry ---------------------------------------------------------

    def run(self):
        if self._preflight():
            self._bucket_posture()
            self._signing_key()
            self._oac()
            self._distribution()
            self._bucket_policy()
            self._kms()
            self._persist()
        return self.report()

    def report(self):
        rep = report.Report(self.findings, self.actions) if hasattr(report, "Report") else None
        items = [f.as_dict() for f in self.findings]
        worst = max((f.status for f in self.findings), key=lambda s: report.SEVERITY[s], default=report.PASS)
        return {
            "version": REPORT_VERSION,
            "mode": "enable" if self.apply else "check",
            "bucket": self.bucket,
            "manager": self.manager.pk,
            "harden": self.harden,
            "items": items,
            "actions": [a.as_dict() for a in self.actions],
            "worst": worst,
            "blocking": any(f.status in report.BLOCKING_STATUSES for f in self.findings),
            # Converged means delivery will work: nothing blind, blocked, missing
            # or left to a human. PENDING (still deploying) and DRIFT (posture
            # advice) do not stop enablement.
            "converged": all(f.status in (report.PASS, report.PENDING, report.DRIFT)
                             for f in self.findings),
            "distribution": {k: v for k, v in self.observed.items()
                             if k in ("distribution_id", "distribution_arn", "domain_name",
                                      "status", "oac_id", "key_group_id", "key_id")},
            "counts": rep.counts() if rep is not None else {},
        }

    # --- steps ---------------------------------------------------------

    def _preflight(self):
        if not self.manager.is_s3 or not self.bucket:
            self._add(STEP_PREFLIGHT, report.MANUAL, "manager.not_s3",
                      "CDN delivery applies to S3 file managers only",
                      "choose an S3 file manager")
            return False
        if self.apply and self.secret_writer is _store_private_key and not settings.get("KMS_KEY_ID", None):
            self._add(STEP_PREFLIGHT, report.MANUAL, "kms_key_id.missing",
                      "KMS_KEY_ID is not configured, so the CloudFront signing key cannot be stored",
                      "set KMS_KEY_ID (the deployment's envelope key; `python -m "
                      "mojo.deploy.provision apply` creates one), then re-run")
            return False
        identity = self._call(STEP_PREFLIGHT, "sts.get_caller_identity", "sts:GetCallerIdentity",
                              lambda: self.client("sts").get_caller_identity())
        if identity is None:
            return False
        self.observed["account"] = identity.get("Account")
        self._add(STEP_PREFLIGHT, report.PASS, "identity.ok",
                  f"running as {identity.get('Arn')}")
        location = self._call(STEP_PREFLIGHT, "s3.get_bucket_location", "s3:GetBucketLocation",
                              lambda: self.client("s3").get_bucket_location(Bucket=self.bucket),
                              not_found_codes=("NoSuchBucket",))
        if location is None and STEP_PREFLIGHT in self.blocked:
            return False
        if location is None:
            self._add(STEP_PREFLIGHT, report.MANUAL, "bucket.missing",
                      f"bucket {self.bucket} does not exist in this account",
                      "create the bucket (aws-check --apply does) or fix the manager's backend_url")
            return False
        self.observed["bucket_region"] = location.get("LocationConstraint") or "us-east-1"
        return True

    def _bucket_posture(self):
        s3 = self.client("s3")
        pab = self._call(STEP_BUCKET, "s3.get_public_access_block", "s3:GetBucketPublicAccessBlock",
                         lambda: s3.get_public_access_block(Bucket=self.bucket),
                         not_found_codes=("NoSuchPublicAccessBlockConfiguration",), default={})
        if STEP_BUCKET in self.blocked:
            return
        flags = (pab or {}).get("PublicAccessBlockConfiguration") or {}
        all_on = all(flags.get(name) for name in (
            "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"))
        ownership = self._call(STEP_BUCKET, "s3.get_bucket_ownership_controls",
                               "s3:GetBucketOwnershipControls",
                               lambda: s3.get_bucket_ownership_controls(Bucket=self.bucket),
                               not_found_codes=("OwnershipControlsNotFoundError",), default={})
        rules = ((ownership or {}).get("OwnershipControls") or {}).get("Rules") or []
        enforced = any(rule.get("ObjectOwnership") == "BucketOwnerEnforced" for rule in rules)
        self.observed["public_access_block"] = all_on
        self.observed["ownership_enforced"] = enforced

        affected = self._opted_out_public_managers() + [
            m.name for m in self._root_public_managers()]
        if all_on:
            self._add(STEP_BUCKET, report.PASS, "public_access_block.ok",
                      "Block Public Access is on for every setting")
        else:
            names = ", ".join(sorted(set(affected))) or "none"
            if self.apply and self.harden:
                self.actions.append(report.Action(STEP_BUCKET, "modify", self.bucket,
                                                  "enable Block Public Access"))
                if self._call(STEP_BUCKET, "s3.put_public_access_block",
                              "s3:PutBucketPublicAccessBlock",
                              lambda: s3.put_public_access_block(
                                  Bucket=self.bucket, PublicAccessBlockConfiguration={
                                      "BlockPublicAcls": True, "IgnorePublicAcls": True,
                                      "BlockPublicPolicy": True, "RestrictPublicBuckets": True}),
                              mutation=True, default=False) is not False:
                    self._add(STEP_BUCKET, report.PASS, "public_access_block.enabled",
                              "Block Public Access enabled (--harden)")
            else:
                self._add(STEP_BUCKET, report.DRIFT, "public_access_block.off",
                          "Block Public Access is not fully on, so the bucket may still be made public by policy",
                          f"re-run with --harden to turn it on. That breaks raw-S3 public links of every "
                          f"manager on this bucket that does not deliver through the CDN (opted out, "
                          f"or public at the bucket root): {names}")
        if enforced:
            self._add(STEP_BUCKET, report.PASS, "ownership.ok", "Object Ownership is BucketOwnerEnforced")
        elif self.apply and self.harden:
            self.actions.append(report.Action(STEP_BUCKET, "modify", self.bucket,
                                              "set Object Ownership to BucketOwnerEnforced"))
            if self._call(STEP_BUCKET, "s3.put_bucket_ownership_controls",
                          "s3:PutBucketOwnershipControls",
                          lambda: s3.put_bucket_ownership_controls(
                              Bucket=self.bucket, OwnershipControls={
                                  "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}),
                          mutation=True, default=False) is not False:
                self._add(STEP_BUCKET, report.PASS, "ownership.enforced",
                          "Object Ownership set to BucketOwnerEnforced (--harden)")
        else:
            self._add(STEP_BUCKET, report.DRIFT, "ownership.not_enforced",
                      "Object Ownership is not BucketOwnerEnforced, which Origin Access Control requires",
                      "re-run with --harden, or set Object Ownership to Bucket owner enforced in S3")

        encryption = self._call(STEP_BUCKET, "s3.get_bucket_encryption", "s3:GetEncryptionConfiguration",
                                lambda: s3.get_bucket_encryption(Bucket=self.bucket),
                                not_found_codes=("ServerSideEncryptionConfigurationNotFoundError",),
                                default={})
        rules = ((encryption or {}).get("ServerSideEncryptionConfiguration") or {}).get("Rules") or []
        default = (rules[0].get("ApplyServerSideEncryptionByDefault") or {}) if rules else {}
        self.observed["bucket_sse"] = default.get("SSEAlgorithm")
        self.observed["bucket_kms_key"] = default.get("KMSMasterKeyID")

    def _managers_on_bucket(self):
        """Every S3 manager row on the bucket, active or not: their objects are
        in the bucket regardless, and the bucket policy grants CloudFront all
        of them, so every prefix must be planned."""
        return managers_on_bucket(self.bucket)

    def _opted_out_public_managers(self):
        names = []
        for manager in self._managers_on_bucket():
            if not manager.is_public:
                continue
            try:
                if cdn.validate_switch(manager.get_setting(cdn.CDN_SWITCH_KEY)) == "off":
                    names.append(manager.name)
            except ValueError:
                continue
        return names

    def _root_public_managers(self):
        """Active public managers at the top of the bucket that have not opted
        out: they keep S3 links (`cdn.resolve_for_manager`), because an
        empty prefix gets no unsigned behavior and the signed default would
        refuse their unsigned CDN links."""
        rows = []
        for manager in self._managers_on_bucket():
            if not manager.is_active or not manager.is_public:
                continue
            if (manager.root_path or "").strip("/"):
                continue
            try:
                if cdn.validate_switch(manager.get_setting(cdn.CDN_SWITCH_KEY)) == "off":
                    continue
            except ValueError:
                continue
            rows.append(manager)
        return rows

    def _behavior_managers(self):
        """Every manager on the bucket, with this provisioner's own instance
        replacing its database row so a pre-save sync sees the value about
        to be written. Opt-out does not remove a manager: it only changes the
        links it hands out, not what the edge must protect."""
        rows = []
        for manager in self._managers_on_bucket():
            rows.append(self.manager if manager.pk == self.manager.pk else manager)
        if all(m.pk != self.manager.pk for m in rows):
            rows.append(self.manager)
        return rows

    def _planned_behaviors(self, origin_id, key_group_id, step=STEP_DISTRIBUTION,
                           response_headers_policy_id=None):
        """plan_behaviors plus its findings. Returns the plan, or None when it
        must not be written (no key group, a nested private prefix, over quota)."""
        try:
            plan = plan_behaviors(self._behavior_managers(), origin_id, key_group_id,
                                  response_headers_policy_id=response_headers_policy_id)
        except KeyGroupMissing:
            self._add(step, report.MANUAL, "key_group.missing",
                      "a private prefix needs the deployment signing key group, and this "
                      "distribution has none recorded",
                      "run enable to create or record the key group, then re-run")
            self.blocked.add(step)
            return None
        for prefix, name in plan["unsafe"]:
            self._add(step, report.MANUAL, "prefix.unsafe",
                      f"manager {name!r} has root path {prefix!r}, which CloudFront would read as a "
                      f"wildcard or cannot match; it gets no cache behavior and its links will fail",
                      "change the manager's backend_url to a path of letters, digits, '/', '-', "
                      "'_', '.' or '~'")
        for prefix, ancestor in plan["nested"]:
            self._add(step, report.MANUAL, "prefix.nested_private",
                      f"private prefix {prefix!r} sits under public prefix {ancestor!r}; an "
                      f"encoded request path could reach it through the public behavior",
                      "make the parent manager private, or move the private manager to a prefix "
                      "that is not under a public one")
        if len(plan["behaviors"]) > MAX_BEHAVIORS:
            self._add(step, report.MANUAL, "behaviors.quota",
                      f"{len(plan['behaviors'])} cache behaviors are needed but CloudFront allows "
                      f"{MAX_BEHAVIORS} per distribution by default",
                      "request a quota increase, or give managers with the same visibility a "
                      "common parent prefix")
        if plan["nested"] or len(plan["behaviors"]) > MAX_BEHAVIORS:
            self.blocked.add(step)
            return None
        return plan

    def _signing_key(self):
        from mojo.apps.fileman.models import CdnSigningKey
        if self._blocked(STEP_KEY, STEP_PREFLIGHT):
            return
        cf = self.client("cloudfront")
        key = (CdnSigningKey.objects.filter(is_active=True, retire_after__isnull=True,
                                            key_id__isnull=False).order_by("-pk").first())
        pending = None
        if key is None:
            pending = (CdnSigningKey.objects.filter(is_active=True, key_id__isnull=True)
                       .order_by("-pk").first())
        if key is None and not self.apply:
            self._add(STEP_KEY, report.MISSING, "signing_key.missing",
                      "no active CloudFront signing key exists yet",
                      "enable creates one (RSA-2048, private half KMS-encrypted)")
            self.blocked.add(STEP_KEY)
            return
        if key is None:
            key = self._create_signing_key(cf, pending)
            if key is None:
                self.blocked.add(STEP_KEY)
                return
        self.observed["key_id"] = key.key_id
        self.observed["signing_key_pk"] = key.pk
        self._add(STEP_KEY, report.PASS, "signing_key.ok", f"signing key {key.key_id} is active")

        # The key group, one per deployment.
        name = key_group_name()
        groups = self._call(STEP_KEY, "cloudfront.list_key_groups", "cloudfront:ListKeyGroups",
                            lambda: cf.list_key_groups(MaxItems="100"), default={})
        if STEP_KEY in self.blocked:
            return
        group = None
        for item in ((groups or {}).get("KeyGroupList") or {}).get("Items") or []:
            group_body = item.get("KeyGroup") or item
            if (group_body.get("KeyGroupConfig") or {}).get("Name") == name:
                group = group_body
                break
        active_ids = [row.key_id for row in CdnSigningKey.objects.filter(
            is_active=True, key_id__isnull=False)]
        if group is None:
            if not self.apply:
                self._add(STEP_KEY, report.MISSING, "key_group.missing", f"key group {name} does not exist",
                          "enable creates it with the active signing key")
                self.blocked.add(STEP_KEY)
                return
            self.actions.append(report.Action(STEP_KEY, "create", name))
            created = self._call(STEP_KEY, "cloudfront.create_key_group", "cloudfront:CreateKeyGroup",
                                 lambda: cf.create_key_group(KeyGroupConfig={
                                     "Name": name, "Items": active_ids,
                                     "Comment": "django-mojo fileman signing keys"}), mutation=True)
            if created is None:
                return
            self.observed["key_group_id"] = (created.get("KeyGroup") or {}).get("Id")
            self._add(STEP_KEY, report.PASS, "key_group.created", f"key group {name} created")
            return
        group_id = group.get("Id")
        self.observed["key_group_id"] = group_id
        members = list((group.get("KeyGroupConfig") or {}).get("Items") or [])
        missing = [kid for kid in active_ids if kid not in members]
        if not missing:
            self._add(STEP_KEY, report.PASS, "key_group.ok", f"key group {name} holds the active key")
            return
        if not self.apply:
            self._add(STEP_KEY, report.DRIFT, "key_group.stale",
                      f"key group {name} lacks the active signing key", "enable adds it")
            return
        if len(members) + len(missing) > MAX_KEYS_PER_GROUP:
            self._add(STEP_KEY, report.MANUAL, "key_group.full",
                      f"key group {name} would exceed {MAX_KEYS_PER_GROUP} keys",
                      "revoke or retire an old key (fileman_cdn revoke-key), then re-run")
            self.blocked.add(STEP_KEY)
            return
        self.actions.append(report.Action(STEP_KEY, "modify", name, "add the active signing key"))
        self._update_key_group(cf, group_id, members + missing, STEP_KEY)

    def _update_key_group(self, cf, group_id, items, step):
        current = self._call(step, "cloudfront.get_key_group", "cloudfront:GetKeyGroup",
                             lambda: cf.get_key_group(Id=group_id))
        if current is None:
            return False
        config = dict((current.get("KeyGroup") or {}).get("KeyGroupConfig") or {})
        config["Items"] = list(items)
        updated = self._call(step, "cloudfront.update_key_group", "cloudfront:UpdateKeyGroup",
                             lambda: cf.update_key_group(Id=group_id, IfMatch=current.get("ETag"),
                                                         KeyGroupConfig=config), mutation=True)
        if updated is None:
            return False
        self._add(step, report.PASS, "key_group.updated", "key group membership updated")
        return True

    def _create_signing_key(self, cf, pending=None):
        """Save the row first, then create the public key with a deterministic
        CallerReference, so a crash between the two is resumable."""
        from mojo.apps.fileman.models import CdnSigningKey
        row = pending
        if row is None:
            pem = self.key_pem_factory()
            row = CdnSigningKey(is_active=True)
            row.save()
            row.public_key_name = public_key_name(row.pk)
            try:
                self.secret_writer(row, pem)
            except Exception as error:
                self._add(STEP_KEY, report.BLIND, "signing_key.store_failed",
                          f"the private key could not be stored ({type(error).__name__})",
                          "check KMS_KEY_ID and kms:Encrypt on it, then re-run")
                row.is_active = False
                row.save()
                return None
            row.save()
            self.actions.append(report.Action(STEP_KEY, "create", row.public_key_name))
        name = row.public_key_name or public_key_name(row.pk)
        # Resume: a public key of this name may already exist.
        existing = self._call(STEP_KEY, "cloudfront.list_public_keys", "cloudfront:ListPublicKeys",
                              lambda: cf.list_public_keys(MaxItems="100"), default={})
        if STEP_KEY in self.blocked:
            return None
        key_id = None
        for item in ((existing or {}).get("PublicKeyList") or {}).get("Items") or []:
            if item.get("Name") == name:
                key_id = item.get("Id")
                break
        try:
            private_key = self.key_loader(row)
        except Exception as error:
            self._add(STEP_KEY, report.BLIND, "signing_key.unreadable",
                      f"the stored private key could not be read ({type(error).__name__})",
                      "check KMS_KEY_ID and kms:Decrypt on it, then re-run")
            return None
        if key_id is not None:
            # Resume by name — but only if that public key is really ours.
            found = self._call(STEP_KEY, "cloudfront.get_public_key", "cloudfront:GetPublicKey",
                               lambda: cf.get_public_key(Id=key_id))
            if found is None:
                return None
            encoded = ((found.get("PublicKey") or {}).get("PublicKeyConfig") or {}).get("EncodedKey") or ""
            if encoded.strip() != cdn.public_key_pem(private_key).strip():
                self._add(STEP_KEY, report.MANUAL, "signing_key.name_collision",
                          f"CloudFront public key {name} exists but does not match the stored private key",
                          "another deployment in this account uses the same name; delete or rename "
                          "that key, then re-run")
                return None
        if key_id is None:
            created = self._call(STEP_KEY, "cloudfront.create_public_key", "cloudfront:CreatePublicKey",
                                 lambda: cf.create_public_key(PublicKeyConfig={
                                     "CallerReference": name, "Name": name,
                                     "EncodedKey": cdn.public_key_pem(private_key),
                                     "Comment": "django-mojo fileman signing key"}), mutation=True)
            if created is None:
                return None
            key_id = (created.get("PublicKey") or {}).get("Id")
        row.key_id = key_id
        row.public_key_name = name
        row.save()
        self._add(STEP_KEY, report.PASS, "signing_key.created", f"signing key {key_id} created")
        return row

    def _oac(self):
        if self._blocked(STEP_OAC, STEP_PREFLIGHT):
            return
        cf = self.client("cloudfront")
        name = oac_name(self.bucket)
        listing = self._call(STEP_OAC, "cloudfront.list_origin_access_controls",
                             "cloudfront:ListOriginAccessControls",
                             lambda: cf.list_origin_access_controls(MaxItems="100"), default={})
        if STEP_OAC in self.blocked:
            return
        for item in ((listing or {}).get("OriginAccessControlList") or {}).get("Items") or []:
            if item.get("Name") == name:
                self.observed["oac_id"] = item.get("Id")
                ok = (item.get("SigningBehavior") == "always" and item.get("SigningProtocol") == "sigv4"
                      and item.get("OriginAccessControlOriginType") == "s3")
                self._add(STEP_OAC, report.PASS if ok else report.MANUAL, "oac.ok" if ok else "oac.misconfigured",
                          f"origin access control {name} exists",
                          None if ok else "set signing to always / sigv4 for an S3 origin in CloudFront")
                return
        if not self.apply:
            self._add(STEP_OAC, report.MISSING, "oac.missing", f"origin access control {name} does not exist",
                      "enable creates it (s3, sigv4, always)")
            self.blocked.add(STEP_OAC)
            return
        self.actions.append(report.Action(STEP_OAC, "create", name))
        created = self._call(STEP_OAC, "cloudfront.create_origin_access_control",
                             "cloudfront:CreateOriginAccessControl",
                             lambda: cf.create_origin_access_control(OriginAccessControlConfig={
                                 "Name": name, "Description": f"django-mojo fileman {self.bucket}",
                                 "SigningProtocol": "sigv4", "SigningBehavior": "always",
                                 "OriginAccessControlOriginType": "s3"}), mutation=True)
        if created is None:
            return
        self.observed["oac_id"] = (created.get("OriginAccessControl") or {}).get("Id")
        self._add(STEP_OAC, report.PASS, "oac.created", f"origin access control {name} created")

    def _find_distribution(self, cf):
        """Our tagged distribution for the bucket, an untagged one on the same
        origin (never adopted), or nothing. BYO rows are looked up by id."""
        from mojo.apps.fileman.models import CdnDistribution
        row = CdnDistribution.objects.filter(bucket_name=self.bucket).first()
        if row is not None and row.distribution_id:
            found = self._call(STEP_DISTRIBUTION, "cloudfront.get_distribution", "cloudfront:GetDistribution",
                               lambda: cf.get_distribution(Id=row.distribution_id),
                               not_found_codes=("NoSuchDistribution",))
            if found is not None:
                return found.get("Distribution"), row.source == "byo", None
        listing = self._call(STEP_DISTRIBUTION, "cloudfront.list_distributions",
                             "cloudfront:ListDistributions",
                             lambda: cf.list_distributions(MaxItems="200"), default={})
        if STEP_DISTRIBUTION in self.blocked:
            return None, False, None
        origin = origin_domain(self.bucket, self.observed.get("bucket_region", self.region))
        foreign = None
        for item in ((listing or {}).get("DistributionList") or {}).get("Items") or []:
            origins = ((item.get("Origins") or {}).get("Items") or [])
            if not any(o.get("DomainName") == origin for o in origins):
                continue
            tags = self._call(STEP_DISTRIBUTION, "cloudfront.list_tags_for_resource",
                              "cloudfront:ListTagsForResource",
                              lambda: cf.list_tags_for_resource(Resource=item.get("ARN")), default={})
            items = ((tags or {}).get("Tags") or {}).get("Items") or []
            if any(t.get("Key") == TAG_KEY and t.get("Value") == self.bucket for t in items):
                full = self._call(STEP_DISTRIBUTION, "cloudfront.get_distribution",
                                  "cloudfront:GetDistribution",
                                  lambda: cf.get_distribution(Id=item.get("Id")))
                return (full or {}).get("Distribution") or item, False, None
            foreign = item.get("Id")
        return None, False, foreign

    def _distribution(self):
        # check mode examines whatever distribution exists even when our own
        # key or OAC is missing (a BYO distribution brings its own); enable
        # needs both before it can create or converge one.
        depends = (STEP_PREFLIGHT, STEP_KEY, STEP_OAC) if self.apply else (STEP_PREFLIGHT,)
        if self._blocked(STEP_DISTRIBUTION, *depends):
            return
        for manager in self._root_public_managers():
            self._add(STEP_DISTRIBUTION, report.DRIFT, "prefix.root_public",
                      f"public manager {manager.name!r} sits at the top of {self.bucket}, so it "
                      f"keeps S3 links instead of CDN links",
                      f"give it a path of its own (s3://{self.bucket}/<prefix>) to deliver it "
                      f"through the CDN")
        cf = self.client("cloudfront")
        distribution, byo, foreign = self._find_distribution(cf)
        if STEP_DISTRIBUTION in self.blocked:
            return
        if distribution is None:
            if foreign:
                self._add(STEP_DISTRIBUTION, report.MANUAL, "distribution.foreign",
                          f"distribution {foreign} already serves {self.bucket} but was not created by "
                          f"django-mojo",
                          f"to let django-mojo manage it, tag it {TAG_KEY}={self.bucket}; to keep it "
                          f"yours, register it with `fileman_cdn byo --distribution {foreign}` and "
                          f"run check")
                self.blocked.add(STEP_DISTRIBUTION)
                return
            if not self.apply:
                self._add(STEP_DISTRIBUTION, report.MISSING, "distribution.missing",
                          f"no django-mojo distribution serves {self.bucket}",
                          "enable creates it (OAC origin, HTTPS, GET/HEAD, signed by default)")
                self.blocked.add(STEP_DISTRIBUTION)
                return
            self._create_distribution(cf)
            return
        self._record_distribution(distribution)
        config = distribution.get("DistributionConfig") or {}
        if not config:
            got = self._call(STEP_DISTRIBUTION, "cloudfront.get_distribution_config",
                             "cloudfront:GetDistributionConfig",
                             lambda: cf.get_distribution_config(Id=distribution.get("Id")))
            if got is None:
                return
            config = got.get("DistributionConfig") or {}
            etag = got.get("ETag")
        else:
            etag = None
        self._check_distribution_config(cf, distribution, config, byo, etag)

    def _record_distribution(self, distribution):
        self.observed["distribution_id"] = distribution.get("Id")
        self.observed["distribution_arn"] = distribution.get("ARN")
        self.observed["domain_name"] = distribution.get("DomainName")
        status = distribution.get("Status")
        self.observed["status"] = "deployed" if status == "Deployed" else "pending"
        if status in PENDING_STATUSES:
            self._add(STEP_DISTRIBUTION, report.PENDING, "distribution.deploying",
                      f"distribution {distribution.get('Id')} is still deploying to the edge",
                      "links are issued now and start working when CloudFront reports Deployed")

    def _create_distribution(self, cf):
        region = self.observed.get("bucket_region", self.region)
        plan = self._planned_behaviors(f"s3-{self.bucket}", self.observed.get("key_group_id"),
                                       response_headers_policy_id=self.response_headers_policy)
        if plan is None:
            return
        config = distribution_config(
            self.bucket, region, self.observed.get("oac_id"), self.observed.get("key_group_id"),
            plan["behaviors"], caller_reference=f"mojo-fm-{self.bucket}-{uuid.uuid4().hex[:8]}",
            response_headers_policy_id=self.response_headers_policy)
        self.actions.append(report.Action(STEP_DISTRIBUTION, "create", self.bucket))
        created = self._call(STEP_DISTRIBUTION, "cloudfront.create_distribution_with_tags",
                             "cloudfront:CreateDistribution",
                             lambda: cf.create_distribution_with_tags(DistributionConfigWithTags={
                                 "DistributionConfig": config,
                                 "Tags": {"Items": [{"Key": TAG_KEY, "Value": self.bucket}]}}),
                             mutation=True)
        if created is None:
            self._add(STEP_DISTRIBUTION, report.MANUAL, "distribution.tagging_note",
                      "creating a tagged distribution also needs cloudfront:TagResource",
                      "grant cloudfront:CreateDistribution and cloudfront:TagResource, then re-run")
            return
        distribution = created.get("Distribution") or {}
        self._record_distribution(distribution)
        self._add(STEP_DISTRIBUTION, report.PASS, "distribution.created",
                  f"distribution {distribution.get('Id')} created for {self.bucket}")

    def _check_distribution_config(self, cf, distribution, config, byo, etag):
        """Compare the live config with what we converge to; modify on drift."""
        dist_id = distribution.get("Id")
        origins = ((config.get("Origins") or {}).get("Items") or [])
        origin = origins[0] if origins else {}
        problems = []
        expected_origin = origin_domain(self.bucket, self.observed.get("bucket_region", self.region))
        if origin.get("DomainName") != expected_origin:
            problems.append(("origin.mismatch", f"origin is {origin.get('DomainName')}, expected "
                             f"{expected_origin} (a website endpoint cannot use OAC)"))
        if not origin.get("OriginAccessControlId"):
            problems.append(("oac.unset", "the origin has no Origin Access Control"))
        if ((origin.get("S3OriginConfig") or {}).get("OriginAccessIdentity") or "") != "":
            problems.append(("oai.in_use", "the origin uses a legacy Origin Access Identity; OAC needs it empty"))
        if byo:
            self.observed.setdefault("oac_id", origin.get("OriginAccessControlId"))
        default = config.get("DefaultCacheBehavior") or {}
        groups = default.get("TrustedKeyGroups") or {}
        key_group_id = self.observed.get("key_group_id")
        if not key_group_id and byo and groups.get("Enabled") and groups.get("Items"):
            # A BYO distribution trusts its own key group; record it so the
            # plan can sign with it.
            key_group_id = (groups.get("Items") or [None])[0]
            self.observed["key_group_id"] = key_group_id
        if not groups.get("Enabled") or (key_group_id and key_group_id not in (groups.get("Items") or [])):
            problems.append(("default_behavior.unsigned",
                             "the default cache behavior does not require the signing key group, so "
                             "unlisted prefixes would be served to anyone"))
        if default.get("ViewerProtocolPolicy") == "allow-all":
            problems.append(("viewer.http", "the default behavior allows plain HTTP"))
        methods = (default.get("AllowedMethods") or {}).get("Items") or []
        if any(m not in ("GET", "HEAD") for m in methods):
            problems.append(("methods.too_wide", "allowed methods must be GET and HEAD only "
                             "(CloudFront does not check signatures on OPTIONS)"))
        if default.get("CachePolicyId") not in (CACHING_OPTIMIZED_POLICY_ID, None) and not byo:
            problems.append(("cache_policy.unexpected", "the default cache policy is not CachingOptimized"))
        if default.get("ForwardedValues", {}).get("QueryString") is True:
            problems.append(("cache_key.query_strings",
                             "query strings are in the cache key, so every signed URL is cached separately"))
        # A BYO distribution keeps the policy on its own default behavior; a
        # provisioned one carries the deployment's (FILEMAN_CDN_RESPONSE_HEADERS_POLICY).
        live_policy = default.get("ResponseHeadersPolicyId") or None
        policy = live_policy if byo else self.response_headers_policy
        if not byo and live_policy != policy:
            problems.append(("default_behavior.response_headers",
                             f"the default cache behavior's response headers policy is "
                             f"{live_policy or 'none'}, expected {policy or 'none'} "
                             f"({cdn.RESPONSE_HEADERS_POLICY_KEY})"))
        origin_id = origin.get("Id") or f"s3-{self.bucket}"
        plan = self._planned_behaviors(origin_id, key_group_id, response_headers_policy_id=policy)
        if plan is None:
            return
        desired = plan["behaviors"]
        current = (config.get("CacheBehaviors") or {}).get("Items") or []
        if not _behaviors_match(current, desired):
            problems.append(("behaviors.drift", "the per-manager cache behaviors differ from what the "
                             "managers on this bucket need (prefix, signed/unsigned, order)"))
        if not config.get("Enabled"):
            problems.append(("distribution.disabled", "the distribution is disabled"))

        structural = [p for p in problems if p[0] in ("origin.mismatch", "oai.in_use")]
        if structural:
            for code, message in structural:
                self._add(STEP_DISTRIBUTION, report.MANUAL, f"distribution.{code}", message,
                          "fix this in CloudFront; django-mojo changes behaviors and enablement only")
            self.blocked.add(STEP_DISTRIBUTION)
            return
        if not problems:
            self._add(STEP_DISTRIBUTION, report.PASS, "distribution.ok",
                      f"distribution {dist_id} serves {self.bucket} through OAC with the expected behaviors")
            return
        for code, message in problems:
            self._add(STEP_DISTRIBUTION, report.DRIFT if self.apply else report.DRIFT,
                      f"distribution.{code}", message,
                      None if self.apply else ("enable converges the behaviors and enablement; "
                                               "BYO distributions are changed only on explicit enable"))
        if not self.apply:
            return
        if etag is None:
            got = self._call(STEP_DISTRIBUTION, "cloudfront.get_distribution_config",
                             "cloudfront:GetDistributionConfig",
                             lambda: cf.get_distribution_config(Id=dist_id))
            if got is None:
                return
            config, etag = got.get("DistributionConfig") or config, got.get("ETag")
        region = self.observed.get("bucket_region", self.region)
        new_config = distribution_config(
            self.bucket, region, origin.get("OriginAccessControlId") or self.observed.get("oac_id"),
            key_group_id, desired, config.get("CallerReference"), enabled=True,
            current=config, response_headers_policy_id=policy)
        self.actions.append(report.Action(STEP_DISTRIBUTION, "modify", dist_id,
                                          "converge behaviors and enablement"))
        updated = self._call(STEP_DISTRIBUTION, "cloudfront.update_distribution",
                             "cloudfront:UpdateDistribution",
                             lambda: cf.update_distribution(Id=dist_id, IfMatch=etag,
                                                            DistributionConfig=new_config),
                             mutation=True)
        if updated is None:
            return
        self._record_distribution((updated.get("Distribution") or {}) or distribution)
        self._add(STEP_DISTRIBUTION, report.PASS, "distribution.updated",
                  f"distribution {dist_id} updated")

    def sync_behaviors_only(self, row):
        """Rewrite ONLY the per-prefix cache behaviors of the bucket's distribution.

        Used by visibility flips. Nothing else in the config is touched — not
        `Enabled`, not the origin, not a BYO distribution's own settings —
        and the default behavior is left as it is (reported if unsigned).
        The per-prefix behaviors copy the live default's response headers
        policy; the setting is never read here, so a bad setting cannot make
        CloudFront refuse the update and with it a manager's save.
        """
        cf = self.client("cloudfront")
        got = self._call(STEP_DISTRIBUTION, "cloudfront.get_distribution_config",
                         "cloudfront:GetDistributionConfig",
                         lambda: cf.get_distribution_config(Id=row.distribution_id),
                         not_found_codes=("NoSuchDistribution",))
        if got is None:
            if STEP_DISTRIBUTION not in self.blocked:
                self._add(STEP_DISTRIBUTION, report.MANUAL, "distribution.gone",
                          f"distribution {row.distribution_id} no longer exists",
                          "run check to re-record, or disable the CDN for this bucket")
                self.blocked.add(STEP_DISTRIBUTION)
            return
        config = got.get("DistributionConfig") or {}
        origins = ((config.get("Origins") or {}).get("Items") or [])
        origin_id = (origins[0].get("Id") if origins else None) or f"s3-{self.bucket}"
        default_behavior = config.get("DefaultCacheBehavior") or {}
        default_groups = default_behavior.get("TrustedKeyGroups") or {}
        key_group_id = row.key_group_id or ((default_groups.get("Items") or [None])[0]
                                            if default_groups.get("Enabled") else None)
        if not default_groups.get("Enabled"):
            self._add(STEP_DISTRIBUTION, report.DRIFT, "distribution.default_behavior.unsigned",
                      "the default cache behavior does not require a signature",
                      "run enable to converge it")
        plan = self._planned_behaviors(
            origin_id, key_group_id,
            response_headers_policy_id=default_behavior.get("ResponseHeadersPolicyId") or None)
        if plan is None:
            return
        current = (config.get("CacheBehaviors") or {}).get("Items") or []
        if _behaviors_match(current, plan["behaviors"]):
            self._add(STEP_DISTRIBUTION, report.PASS, "behaviors.ok", "cache behaviors already match")
            return
        if not self._signed_behaviors_writable(cf, plan["behaviors"], default_groups):
            return
        new_config = dict(config)
        new_config["CacheBehaviors"] = {"Quantity": len(plan["behaviors"]), "Items": plan["behaviors"]}
        self.actions.append(report.Action(STEP_DISTRIBUTION, "modify", row.distribution_id,
                                          "converge cache behaviors"))
        updated = self._call(STEP_DISTRIBUTION, "cloudfront.update_distribution",
                             "cloudfront:UpdateDistribution",
                             lambda: cf.update_distribution(Id=row.distribution_id, IfMatch=got.get("ETag"),
                                                            DistributionConfig=new_config),
                             mutation=True)
        if updated is None:
            return
        self._add(STEP_DISTRIBUTION, report.PASS, "behaviors.updated",
                  f"cache behaviors of {row.distribution_id} updated")

    def _signed_behaviors_writable(self, cf, behaviors, default_groups):
        """Refuse, as a finding, a behavior set CloudFront would answer with
        an opaque InvalidArgument (maestro #8346): a duplicate path pattern,
        a signed behavior that trusts no key group, or a key group that is
        neither the live default's nor one this account has. The recorded
        key group is preferred over the live default, so the two can drift
        apart. Returns True when the update may be sent."""
        problems = []
        patterns = [b.get("PathPattern") for b in behaviors]
        if len(set(patterns)) != len(patterns):
            problems.append("two cache behaviors share a path pattern")
        cited = set()
        for behavior in behaviors:
            groups = behavior.get("TrustedKeyGroups") or {}
            if not groups.get("Enabled"):
                continue
            items = groups.get("Items") or []
            if not items or groups.get("Quantity") != len(items):
                problems.append(f"signed behavior {behavior.get('PathPattern')!r} trusts no key group")
            cited.update(items)
        if problems:
            self._add(STEP_DISTRIBUTION, report.MANUAL, "behaviors.invalid", "; ".join(problems),
                      "run check_cdn and report the finding; the update was not sent")
            self.blocked.add(STEP_DISTRIBUTION)
            return False
        live = set(default_groups.get("Items") or []) if default_groups.get("Enabled") else set()
        unknown = cited - live
        if not unknown:
            return True
        known = set()
        marker = None
        seen = set()
        complete = False
        for _ in range(KEY_GROUP_PAGE_LIMIT):
            kwargs = {"MaxItems": "100", **({"Marker": marker} if marker else {})}
            page = self._call(STEP_DISTRIBUTION, "cloudfront.list_key_groups", "cloudfront:ListKeyGroups",
                              lambda: cf.list_key_groups(**kwargs))
            if page is None:
                return False
            listing = page.get("KeyGroupList") or {}
            for item in listing.get("Items") or []:
                known.add((item.get("KeyGroup") or item).get("Id"))
            if unknown <= known:
                return True
            marker = listing.get("NextMarker")
            if not marker:
                complete = True
                break
            if marker in seen:
                break
            seen.add(marker)
        if not complete:
            # A listing cut short (page limit, or a marker that repeats) has
            # not shown the group is absent: send the update and let
            # CloudFront judge. Nothing is saved before it answers, and a
            # refusal logs AWS's message.
            return True
        missing = sorted(unknown - known)
        if missing:
            self._add(STEP_DISTRIBUTION, report.MANUAL, "key_group.mismatch",
                      f"the recorded signing key group {', '.join(missing)} is not the distribution's "
                      f"default key group and does not exist in this account",
                      "run enable to converge the key group, then save the manager again")
            self.blocked.add(STEP_DISTRIBUTION)
            return False
        return True

    def _bucket_policy(self):
        if self._blocked(STEP_POLICY, STEP_DISTRIBUTION):
            return
        arn = self.observed.get("distribution_arn")
        if not arn:
            self._add(STEP_POLICY, report.BLOCKED, "bucket_policy.blocked",
                      "no distribution ARN to grant")
            return
        s3 = self.client("s3")
        raw = self._call(STEP_POLICY, "s3.get_bucket_policy", "s3:GetBucketPolicy",
                         lambda: s3.get_bucket_policy(Bucket=self.bucket),
                         not_found_codes=("NoSuchBucketPolicy",), default={"Policy": None})
        if STEP_POLICY in self.blocked:
            return
        current = None
        if raw and raw.get("Policy"):
            try:
                current = json.loads(raw["Policy"])
            except ValueError:
                current = None
        statement = bucket_policy_statement(self.bucket, arn)
        for existing in _statements(current):
            if _is_anonymous_allow(existing):
                self._add(STEP_POLICY, report.DRIFT, "bucket_policy.public_statement",
                          f"the bucket policy grants anonymous access (Sid {existing.get('Sid')})",
                          "remove it once every manager on the bucket delivers through the CDN; "
                          "django-mojo never removes policy statements")
        if _has_statement(current, statement):
            self._add(STEP_POLICY, report.PASS, "bucket_policy.ok",
                      "the bucket policy lets this distribution read objects")
            return
        merged = merged_policy(current, statement)
        if not self.apply:
            self._add(STEP_POLICY, report.MISSING, "bucket_policy.missing",
                      "the bucket policy does not let the distribution read objects",
                      "enable applies this policy, or apply it yourself:\n" + json.dumps(merged, indent=2))
            return
        self.actions.append(report.Action(STEP_POLICY, "write", self.bucket, statement["Sid"]))
        done = self._call(STEP_POLICY, "s3.put_bucket_policy", "s3:PutBucketPolicy",
                          lambda: s3.put_bucket_policy(Bucket=self.bucket, Policy=json.dumps(merged)),
                          mutation=True, default=False)
        if done is not False:
            self._add(STEP_POLICY, report.PASS, "bucket_policy.written",
                      "the bucket policy now lets this distribution read objects")

    def _kms(self):
        if self._blocked(STEP_KMS, STEP_DISTRIBUTION):
            return
        arn = self.observed.get("distribution_arn")
        key_ref = self.manager.get_setting("kms_key_id") or self.observed.get("bucket_kms_key")
        sse = self.observed.get("bucket_sse")
        if not key_ref and sse != "aws:kms":
            self._add(STEP_KMS, report.PASS, "kms.not_used",
                      "objects are not encrypted with KMS; no key policy is needed")
            return
        kms = self.client("kms")
        described = self._call(STEP_KMS, "kms.describe_key", "kms:DescribeKey",
                               lambda: kms.describe_key(KeyId=key_ref or "alias/aws/s3"),
                               not_found_codes=("NotFoundException",))
        if STEP_KMS in self.blocked:
            return
        metadata = (described or {}).get("KeyMetadata") or {}
        if not metadata:
            self._add(STEP_KMS, report.MANUAL, "kms.key_missing", f"KMS key {key_ref} was not found",
                      "fix the manager's kms_key_id or the bucket's default encryption key")
            return
        key_id = metadata.get("KeyId")
        envelope = settings.get("KMS_KEY_ID", None)
        if envelope and envelope in (key_id, metadata.get("Arn"), key_ref):
            self._add(STEP_KMS, report.MANUAL, "kms.envelope_key",
                      "the media key is the deployment's KMS_KEY_ID, which wraps every secret this "
                      "deployment holds; CloudFront will not be granted it",
                      "use a customer-managed key dedicated to media as the manager's kms_key_id")
            self.blocked.add(STEP_KMS)
            return
        if metadata.get("KeyManager") == "AWS":
            self._add(STEP_KMS, report.MANUAL, "kms.aws_managed",
                      "objects are encrypted with the AWS-managed aws/s3 key, whose policy cannot be "
                      "changed, so CloudFront can never decrypt them",
                      "create a customer-managed KMS key dedicated to media (not KMS_KEY_ID, which wraps "
                      "every secret this deployment holds), set it as the manager's kms_key_id and the "
                      "bucket's default encryption, re-encrypt existing objects (copy in place), then "
                      "re-run")
            self.blocked.add(STEP_KMS)
            return
        policy_raw = self._call(STEP_KMS, "kms.get_key_policy", "kms:GetKeyPolicy",
                                lambda: kms.get_key_policy(KeyId=key_id, PolicyName="default"))
        if policy_raw is None:
            return
        try:
            current = json.loads(policy_raw.get("Policy") or "{}")
        except ValueError:
            current = None
        statement = kms_policy_statement(arn)
        if _has_statement(current, statement):
            self._add(STEP_KMS, report.PASS, "kms.ok", "the key policy lets this distribution decrypt")
            return
        merged = merged_policy(current, statement)
        if not self.apply:
            self._add(STEP_KMS, report.MISSING, "kms.missing",
                      "the KMS key policy does not let the distribution decrypt objects",
                      "enable applies this policy (read-merge-write), or apply it yourself:\n"
                      + json.dumps(merged, indent=2))
            return
        self.actions.append(report.Action(STEP_KMS, "write", key_id, statement["Sid"]))
        done = self._call(STEP_KMS, "kms.put_key_policy", "kms:PutKeyPolicy",
                          lambda: kms.put_key_policy(KeyId=key_id, PolicyName="default",
                                                     Policy=json.dumps(merged)),
                          mutation=True, default=False)
        if done is not False:
            self._add(STEP_KMS, report.PASS, "kms.written", "the key policy now lets this distribution decrypt")

    def _persist(self):
        """Write the CdnDistribution row once a distribution exists; findings
        land on it either way so the portal can show the last check."""
        from mojo.apps.fileman.models import CdnDistribution, CdnSigningKey
        row = CdnDistribution.objects.filter(bucket_name=self.bucket).first()
        rep = self.report()
        if not self.apply or not self.observed.get("distribution_id"):
            # check mode records its findings and changes nothing else.
            if row is not None:
                row.last_check = rep
                row.save(update_fields=["last_check", "modified"])
            return
        if row is None:
            if not self.apply:
                return
            # Enabled only once the run converges; the model's default is for
            # rows written by hand.
            row = CdnDistribution(bucket_name=self.bucket, source="provisioned", is_enabled=False)
        row.distribution_id = self.observed.get("distribution_id") or row.distribution_id
        row.distribution_arn = self.observed.get("distribution_arn") or row.distribution_arn
        row.domain_name = self.observed.get("domain_name") or row.domain_name
        row.oac_id = self.observed.get("oac_id") or row.oac_id
        row.key_group_id = self.observed.get("key_group_id") or row.key_group_id
        row.status = self.observed.get("status") or row.status
        if self.observed.get("signing_key_pk"):
            row.signing_key = CdnSigningKey.objects.filter(pk=self.observed["signing_key_pk"]).first()
        if self.apply and rep["converged"]:
            row.is_enabled = True
        row.last_check = rep
        row.save()
        self._add(STEP_PERSIST, report.PASS, "cdn.recorded",
                  f"CDN delivery for {self.bucket} recorded"
                  + ("" if row.is_enabled else " (not enabled: the run did not converge)"))
        self.manager._cdn_distribution_cache = None  # re-resolve on next read


def _store_private_key(row, pem):
    row.set_private_key_pem(pem)
    row.save()


def _load_private_key(row):
    return row.load_private_key()


# ----------------------------------------------------------------------
# operations
# ----------------------------------------------------------------------

def check(manager, **kwargs):
    return CdnProvisioner(manager, apply=False, **kwargs).run()


def enable(manager, harden=False, **kwargs):
    return CdnProvisioner(manager, apply=True, harden=harden, **kwargs).run()


def register_byo(manager, distribution_id, alias=""):
    """Record an operator's own distribution for a bucket; `check` validates it."""
    from mojo.apps.fileman.models import CdnDistribution
    row, _ = CdnDistribution.objects.get_or_create(
        bucket_name=manager.root_location,
        defaults={"source": "byo", "is_enabled": False})
    row.source = "byo"
    row.distribution_id = distribution_id
    row.alias = (alias or "").strip().rstrip("/")
    row.save()
    return row


def disable(manager, retire=False, clients=None, **kwargs):
    """Every manager on the bucket reverts to S3 links on the next call. AWS
    is left as is; `retire` sets the distribution Enabled=false (no delete)."""
    from mojo.apps.fileman.models import CdnDistribution
    row = CdnDistribution.objects.filter(bucket_name=manager.root_location).first()
    if row is None:
        return {"status": True, "disabled": False, "message": "no CDN is configured for this bucket"}
    row.is_enabled = False
    row.save(update_fields=["is_enabled", "modified"])
    manager._cdn_distribution_cache = None
    result = {"status": True, "disabled": True, "distribution_id": row.distribution_id, "retired": False}
    if retire and row.distribution_id:
        provisioner = CdnProvisioner(manager, apply=True, clients=clients, **kwargs)
        cf = provisioner.client("cloudfront")
        got = provisioner._call(STEP_DISTRIBUTION, "cloudfront.get_distribution_config",
                                "cloudfront:GetDistributionConfig",
                                lambda: cf.get_distribution_config(Id=row.distribution_id))
        if got is not None:
            config = dict(got.get("DistributionConfig") or {})
            config["Enabled"] = False
            done = provisioner._call(STEP_DISTRIBUTION, "cloudfront.update_distribution",
                                     "cloudfront:UpdateDistribution",
                                     lambda: cf.update_distribution(
                                         Id=row.distribution_id, IfMatch=got.get("ETag"),
                                         DistributionConfig=config), mutation=True)
            result["retired"] = done is not None
        result["items"] = [f.as_dict() for f in provisioner.findings]
    return result


def sync_manager_behavior(manager, clients=None, **kwargs):
    """Converge the bucket's cache behaviors after a manager's visibility or
    prefix changed. Behaviors only, under a row lock so two concurrent flips
    cannot interleave their reads and writes. Returns the report; raises
    nothing — the caller decides what a failure means."""
    from django.db import transaction
    from mojo.apps.fileman.models import CdnDistribution
    provisioner = CdnProvisioner(manager, apply=True, clients=clients, **kwargs)
    with transaction.atomic():
        row = (CdnDistribution.objects.select_for_update()
               .filter(bucket_name=manager.root_location).exclude(distribution_id="").first())
        if row is None:
            return provisioner.report()
        provisioner.observed["key_group_id"] = row.key_group_id
        provisioner.sync_behaviors_only(row)
        rep = provisioner.report()
        row.last_check = rep
        row.save(update_fields=["last_check", "modified"])
    return rep


def managers_on_bucket(bucket):
    """Every S3 manager whose bucket is exactly `bucket`."""
    from mojo.apps.fileman.models import FileManager
    return [m for m in FileManager.objects.filter(backend_type="s3", backend_url__contains=bucket)
            if m.root_location == bucket]


def distribution_row_for(manager):
    """The bucket's CdnDistribution with a real distribution, enabled or not.

    Visibility flips must converge the edge whenever a distribution exists:
    a disabled row or an opted-out manager still has its objects served by
    that distribution, so "disabled" never means "unprotected".
    """
    from mojo.apps.fileman.models import CdnDistribution
    if not getattr(manager, "is_s3", False):
        return None
    return (CdnDistribution.objects.filter(bucket_name=manager.root_location)
            .exclude(distribution_id="").first())


def _anchor(manager=None):
    """The (row, manager) a key operation runs against: the manager's bucket
    when given, else the first enabled distribution."""
    from mojo.apps.fileman.models import CdnDistribution
    if manager is not None:
        row = CdnDistribution.objects.filter(bucket_name=manager.root_location).first()
        return row, manager
    row = CdnDistribution.objects.filter(is_enabled=True, key_group_id__gt="").order_by("pk").first()
    if row is None:
        return None, None
    rows = managers_on_bucket(row.bucket_name)
    return row, (rows[0] if rows else None)


def rotate_key(manager=None, clients=None, **kwargs):
    """A new signing key becomes the active signer on every distribution; the
    old one keeps signing links already issued until they expire, then the
    cron removes it from the key group."""
    from mojo.apps.fileman.models import CdnDistribution, CdnSigningKey
    anchor, manager = _anchor(manager)
    if anchor is None or not anchor.key_group_id:
        return {"status": False, "error": "no enabled CDN distribution with a key group to rotate for"}
    if manager is None:
        return {"status": False, "error": "no S3 manager on the CDN bucket"}
    provisioner = CdnProvisioner(manager, apply=True, clients=clients, **kwargs)
    cf = provisioner.client("cloudfront")
    old = CdnSigningKey.objects.filter(is_active=True, retire_after__isnull=True,
                                       key_id__isnull=False).order_by("-pk").first()
    group_id = anchor.key_group_id
    current = provisioner._call(STEP_KEY, "cloudfront.get_key_group", "cloudfront:GetKeyGroup",
                                lambda: cf.get_key_group(Id=group_id))
    if current is None:
        return provisioner.report()
    members = list(((current.get("KeyGroup") or {}).get("KeyGroupConfig") or {}).get("Items") or [])
    if len(members) >= MAX_KEYS_PER_GROUP:
        provisioner._add(STEP_KEY, report.MANUAL, "key_group.full",
                         f"the key group already holds {len(members)} keys",
                         "revoke or retire an old key first (fileman_cdn revoke-key)")
        return provisioner.report()
    new = provisioner._create_signing_key(cf)
    if new is None:
        return provisioner.report()
    if not provisioner._update_key_group(cf, group_id, members + [new.key_id], STEP_KEY):
        return provisioner.report()
    CdnDistribution.objects.filter(is_enabled=True).update(signing_key=new)
    if old is not None and old.pk != new.pk:
        # Links round up to the hour past the cap, so the window does too.
        old.retire_after = provisioner.now() + timedelta(seconds=cdn.max_expires_in() + 3600)
        old.save(update_fields=["retire_after", "modified"])
    provisioner._add(STEP_KEY, report.PASS, "key.rotated",
                     f"{new.key_id} now signs; {old.key_id if old else 'no previous key'} retires after "
                     f"its last link expires")
    return provisioner.report()


def revoke_key(key_id, manager=None, clients=None, **kwargs):
    """Remove a key from the key group now and deactivate it. Links it signed
    fail once the change reaches the edge (minutes). The key is deactivated
    only once CloudFront has confirmed it no longer trusts it — a revocation
    the edge did not see must not read as done."""
    from mojo.apps.fileman.models import CdnDistribution, CdnSigningKey
    key = CdnSigningKey.objects.filter(key_id=key_id).first()
    if key is None:
        return {"status": False, "error": f"no signing key {key_id}"}
    anchor, manager = _anchor(manager)
    if anchor is None or manager is None or not anchor.key_group_id:
        return {"status": False, "error": "no CDN distribution with a key group to revoke from",
                "items": []}
    provisioner = CdnProvisioner(manager, apply=True, clients=clients, **kwargs)
    cf = provisioner.client("cloudfront")
    current = provisioner._call(STEP_KEY, "cloudfront.get_key_group", "cloudfront:GetKeyGroup",
                                lambda: cf.get_key_group(Id=anchor.key_group_id))
    findings = lambda: [f.as_dict() for f in provisioner.findings]
    if current is None:
        return {"status": False, "error": "the key group could not be read", "items": findings()}
    members = [m for m in ((current.get("KeyGroup") or {}).get("KeyGroupConfig") or {}).get("Items") or []]
    if key_id in members:
        if not provisioner._update_key_group(cf, anchor.key_group_id, [m for m in members if m != key_id], STEP_KEY):
            return {"status": False, "error": "CloudFront still trusts the key; the key group update failed",
                    "items": findings()}
    key.is_active = False
    key.retire_after = None
    key.save(update_fields=["is_active", "retire_after", "modified"])
    cdn._signer_cache.clear()
    for row in CdnDistribution.objects.filter(signing_key=key):
        replacement = CdnSigningKey.objects.filter(is_active=True, key_id__isnull=False).exclude(pk=key.pk).order_by("-pk").first()
        row.signing_key = replacement
        row.save(update_fields=["signing_key", "modified"])
    return {"status": True, "revoked": key_id, "items": findings()}


def retire_due_keys(manager=None, now=None, clients=None, **kwargs):
    """Cron: keys whose retire window has passed leave the key group."""
    from mojo.apps.fileman.models import CdnSigningKey
    now = now or timezone.now()
    retired = []
    for key in CdnSigningKey.objects.filter(is_active=True, retire_after__lte=now, key_id__isnull=False):
        result = revoke_key(key.key_id, manager=manager, clients=clients, **kwargs)
        if result.get("status"):
            retired.append(key.key_id)
    return retired


def invalidate_paths(distribution, paths, clients=None, **kwargs):
    """Best-effort CloudFront invalidation for keys that changed in place."""
    if distribution is None or not distribution.distribution_id or not paths:
        return None
    rows = managers_on_bucket(distribution.bucket_name)
    if not rows:
        return None
    provisioner = CdnProvisioner(rows[0], apply=True, clients=clients, **kwargs)
    cf = provisioner.client("cloudfront")
    items = sorted({"/" + str(p).lstrip("/") for p in paths})
    return provisioner._call(
        STEP_DISTRIBUTION, "cloudfront.create_invalidation", "cloudfront:CreateInvalidation",
        lambda: cf.create_invalidation(
            DistributionId=distribution.distribution_id,
            InvalidationBatch={"Paths": {"Quantity": len(items), "Items": items},
                               "CallerReference": uuid.uuid4().hex}), mutation=True)
