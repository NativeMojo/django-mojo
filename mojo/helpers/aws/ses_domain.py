"""
SES Domain Orchestration Helper

Purpose:
- Provide high-level, idempotent operations to onboard and manage an AWS SES domain
  for sending and (optionally) receiving.
- Leverage existing helpers to avoid duplication:
  - Sending and identity ops via mojo.helpers.aws.ses.EmailSender
  - SNS topics and subscriptions via mojo.helpers.aws.sns.SNSTopic / SNSSubscription
  - S3 bucket helpers via mojo.helpers.aws.s3.S3Bucket (for basic existence checks)

Key features (skeleton):
- Request SES domain verification + DKIM, and compute required DNS records
- Optionally enable MAIL FROM (DNS records emitted; optional to apply)
- Create SNS topics for bounce/complaint/delivery/inbound and map identity notifications
- Enable domain-level catch-all receiving (SES Receipt Rule Set) to S3 + SNS
- Audit and reconcile routines to detect drift and attempt safe fixes

Note:
- This is a skeleton. Some AWS operations are best-effort; real-world usage needs robust error handling,
  retries, permissions policies, and region/quotas caveats handled at call sites.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Literal, Any, Tuple

import boto3
import json
from botocore.exceptions import ClientError

from mojo.helpers.aws.client import get_session
from mojo.helpers.aws.ses import EmailSender
from mojo.helpers.aws.sns import SNSTopic, SNSSubscription
from mojo.helpers.aws.s3 import S3Bucket
from mojo.helpers.settings import settings
from mojo.helpers import logit


logger = logit.get_logger(__name__)

NotificationType = Literal["Bounce", "Complaint", "Delivery"]
DnsMode = Literal["manual", "route53", "godaddy"]

DEFAULT_RULE_SET_NAME = "mojo-default-receiving"
DEFAULT_TTL = 600


@dataclass
class DnsRecord:
    type: Literal["TXT", "CNAME", "MX"]
    name: str
    value: str
    ttl: int = DEFAULT_TTL


@dataclass
class SnsEndpoints:
    bounce: Optional[str] = None
    complaint: Optional[str] = None
    delivery: Optional[str] = None
    inbound: Optional[str] = None


@dataclass
class OnboardResult:
    domain: str
    region: str
    verification_token: Optional[str] = None
    dkim_tokens: List[str] = field(default_factory=list)
    dns_records: List[DnsRecord] = field(default_factory=list)
    topic_arns: Dict[str, str] = field(default_factory=dict)
    receipt_rule: Optional[str] = None
    rule_set: Optional[str] = None
    notes: List[str] = field(default_factory=list)


@dataclass
class AuditItem:
    resource: str
    desired: Any
    current: Any
    status: Literal["ok", "drifted", "missing", "conflict"]


@dataclass
class AuditReport:
    domain: str
    region: str
    status: Literal["ok", "drifted", "conflict"]
    items: List[AuditItem] = field(default_factory=list)
    checks: Dict[str, bool] = field(default_factory=dict)
    audit_pass: bool = False


def _get_ses_client(region: str, access_key: Optional[str], secret_key: Optional[str]):
    session = get_session(
        access_key or settings.AWS_KEY,
        secret_key or settings.AWS_SECRET,
        region or getattr(settings, "AWS_REGION", "us-east-1"),
    )
    return session.client("ses")


def _request_ses_verification_and_dkim(
    domain: str,
    region: str,
    access_key: Optional[str],
    secret_key: Optional[str],
) -> Tuple[str, List[str]]:
    """
    Request domain verification (returns TXT token) and DKIM tokens.
    Uses EmailSender for identity verification; DKIM via SES client.
    """
    sender = EmailSender(
        access_key=access_key or settings.AWS_KEY,
        secret_key=secret_key or settings.AWS_SECRET,
        region=region or getattr(settings, "AWS_REGION", "us-east-1"),
    )
    ses = _get_ses_client(region, access_key, secret_key)

    # Domain verification token
    vr = sender.verify_domain_identity(domain)
    token = vr.get("VerificationToken")

    # DKIM tokens (3 tokens typical)
    dk = ses.verify_domain_dkim(Domain=domain)
    dkim_tokens = dk.get("DkimTokens", [])

    return token, dkim_tokens


def build_required_dns_records(
    domain: str,
    region: str,
    verification_token: str,
    dkim_tokens: List[str],
    enable_mail_from: bool = False,
    mail_from_subdomain: str = "feedback",
    ttl: int = DEFAULT_TTL,
) -> List[DnsRecord]:
    """
    Build the set of DNS records that must be present for SES domain verification, DKIM,
    and (optionally) MAIL FROM domain.
    """
    records: List[DnsRecord] = []

    # Domain verification TXT
    records.append(
        DnsRecord(
            type="TXT",
            name=f"_amazonses.{domain}",
            value=verification_token,
            ttl=ttl,
        )
    )

    # DKIM CNAMEs
    for token in dkim_tokens:
        records.append(
            DnsRecord(
                type="CNAME",
                name=f"{token}._domainkey.{domain}",
                value=f"{token}.dkim.amazonses.com",
                ttl=ttl,
            )
        )

    if enable_mail_from:
        # MAIL FROM MX + SPF
        mfq = mail_from_subdomain.strip(".")
        records.append(
            DnsRecord(
                type="MX",
                name=f"{mfq}.{domain}",
                value=f"10 feedback-smtp.{region}.amazonses.com",
                ttl=ttl,
            )
        )
        records.append(
            DnsRecord(
                type="TXT",
                name=f"{mfq}.{domain}",
                value="v=spf1 include:amazonses.com ~all",
                ttl=ttl,
            )
        )

    return records


def ensure_sns_topics_and_subscriptions(
    domain: str,
    endpoints: SnsEndpoints,
    region: str,
    access_key: Optional[str],
    secret_key: Optional[str],
) -> Dict[str, str]:
    """
    Ensure SNS topics for bounce/complaint/delivery/inbound.
    If HTTPS endpoints are provided, ensure subscriptions exist.
    Returns topic ARNs by key: bounce, complaint, delivery, inbound.
    """
    # Derive endpoints from EmailDomain.metadata if none provided
    if not any([getattr(endpoints, "bounce", None), getattr(endpoints, "complaint", None), getattr(endpoints, "delivery", None), getattr(endpoints, "inbound", None)]):
        try:
            from mojo.apps.aws.models import EmailDomain as _EmailDomain
            _ed = _EmailDomain.objects.filter(name=domain).first()
            if _ed and isinstance(getattr(_ed, "metadata", None), dict):
                meta = _ed.metadata or {}
                endpoints = SnsEndpoints(
                    bounce=meta.get("bounce_endpoint") or meta.get("sns_bounce_endpoint"),
                    complaint=meta.get("complaint_endpoint") or meta.get("sns_complaint_endpoint"),
                    delivery=meta.get("delivery_endpoint") or meta.get("sns_delivery_endpoint"),
                    inbound=meta.get("inbound_endpoint") or meta.get("sns_inbound_endpoint"),
                )
        except Exception:
            pass
    topic_arns: Dict[str, str] = {}
    safe_domain = domain.replace(".", "-")
    topics = {
        "bounce": f"ses-{safe_domain}-bounce",
        "complaint": f"ses-{safe_domain}-complaint",
        "delivery": f"ses-{safe_domain}-delivery",
        "inbound": f"ses-{safe_domain}-inbound",
    }

    for key, name in topics.items():
        topic = SNSTopic(name, access_key=access_key, secret_key=secret_key, region=region)
        if not topic.exists:
            topic.create(display_name=name)
        topic_arns[key] = topic.arn

        # Subscribe HTTPS endpoints if provided
        endpoint = getattr(endpoints, key, None)
        if endpoint:
            sub = SNSSubscription(topic.arn, access_key=access_key, secret_key=secret_key, region=region)
            # idempotent: SNS allows duplicate subscriptions but returns pending conf
            sub.subscribe(protocol="https", endpoint=endpoint, return_subscription_arn=False)

    return topic_arns


def map_identity_notification_topics(
    domain: str,
    topic_arns: Dict[str, str],
    region: str,
    access_key: Optional[str],
    secret_key: Optional[str],
):
    """
    Map SES identity notifications (bounce/complaint/delivery) to SNS topics.
    """
    ses = _get_ses_client(region, access_key, secret_key)
    for notif, key in [("Bounce", "bounce"), ("Complaint", "complaint"), ("Delivery", "delivery")]:
        arn = topic_arns.get(key)
        if not arn:
            continue
        try:
            ses.set_identity_notification_topic(
                Identity=domain,
                NotificationType=notif,
                SnsTopic=arn,
            )
        except ClientError as e:
            logger.error(f"Failed to map {notif} topic for {domain}: {e}")


def set_mail_from_domain(
    domain: str,
    region: str,
    mail_from_subdomain: str = "feedback",
    behavior_on_mx_failure: Literal["UseDefaultValue", "RejectMessage"] = "UseDefaultValue",
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
):
    """
    Optionally enable/modify MAIL FROM domain on SES identity.
    """
    ses = _get_ses_client(region, access_key, secret_key)
    try:
        ses.set_identity_mail_from_domain(
            Identity=domain,
            MailFromDomain=f"{mail_from_subdomain.strip('.')}.{domain}",
            BehaviorOnMXFailure=behavior_on_mx_failure,
        )
        logger.info(f"MAIL FROM enabled for {domain}")
    except ClientError as e:
        logger.error(f"Failed to configure MAIL FROM for {domain}: {e}")


def ensure_dkim_enabled(
    domain: str,
    region: str,
    access_key: Optional[str],
    secret_key: Optional[str],
):
    """
    Ensure Easy DKIM signing is enabled once DKIM verification has succeeded.
    """
    ses = _get_ses_client(region, access_key, secret_key)
    try:
        resp = ses.get_identity_dkim_attributes(Identities=[domain])
        attrs = (resp.get("DkimAttributes", {}) or {}).get(domain, {}) or {}
        enabled = attrs.get("DkimEnabled")
        vstatus = attrs.get("DkimVerificationStatus")
        if vstatus == "Success" and not enabled:
            ses.set_identity_dkim_enabled(Identity=domain, DkimEnabled=True)
            logger.info(f"Enabled DKIM signing for {domain}")
    except ClientError as e:
        logger.error(f"Failed to ensure DKIM enabled for {domain}: {e}")

def ensure_receiving_catch_all(
    domain: str,
    s3_bucket: str,
    s3_prefix: str,
    inbound_topic_arn: str,
    region: str,
    access_key: Optional[str],
    secret_key: Optional[str],
    rule_set_name: str = DEFAULT_RULE_SET_NAME,
) -> Tuple[str, str]:
    """
    Ensure a domain-level catch-all SES receipt rule that stores raw emails to S3 and
    publishes to the inbound SNS topic.

    Returns (rule_set_name, rule_name).
    """
    # Sanity: inbound bucket should exist
    bucket = S3Bucket(s3_bucket)
    if not bucket._check_exists():
        # Auto-create inbound bucket for SES receiving
        created = bucket.create(region=region)
        if not created:
            raise ValueError(f"Inbound S3 bucket '{s3_bucket}' does not exist and could not be created")

    ses = _get_ses_client(region, access_key, secret_key)

    # Rule set: create if not present; ensure active if none active.
    existing_sets = ses.list_receipt_rule_sets().get("RuleSets", [])
    set_names = {rs.get("Name") for rs in existing_sets}
    active_set = ses.describe_active_receipt_rule_set().get("Metadata", {}).get("Name")

    if rule_set_name not in set_names:
        try:
            ses.create_receipt_rule_set(RuleSetName=rule_set_name)
            logger.info(f"Created SES receipt rule set: {rule_set_name}")
        except ClientError as e:
            # Might already exist due to race; re-fetch
            logger.warning(f"Create rule set warning: {e}")

    # If there is no active set, set ours active
    if not active_set:
        try:
            ses.set_active_receipt_rule_set(RuleSetName=rule_set_name)
            active_set = rule_set_name
        except ClientError as e:
            logger.error(f"Failed to set active rule set: {e}")
    # If active set differs, we still can place rules in our set; SES uses only active one.
    # In production, you might want to switch or merge rules; we report via audit.

    # Ensure domain-level catch-all rule exists (Recipients can include the domain)
    rule_name = f"mojo-{domain}-catchall"

    # See if rule exists in our set
    try:
        rs = ses.describe_receipt_rule_set(RuleSetName=rule_set_name)
        existing = [r for r in rs.get("Rules", []) if r.get("Name") == rule_name]
    except ClientError as e:
        logger.error(f"Failed to describe rule set {rule_set_name}: {e}")
        existing = []

    actions = [
        {
            "S3Action": {
                "BucketName": s3_bucket,
                "ObjectKeyPrefix": s3_prefix or "",
            }
        }
    ]
    # Only include SNSAction when we have an inbound topic ARN
    if inbound_topic_arn:
        actions.append({
            "SNSAction": {
                "TopicArn": inbound_topic_arn,
                "Encoding": "UTF-8",
            }
        })

    rule_def = {
        "Name": rule_name,
        "Enabled": True,
        "TlsPolicy": "Optional",
        "Recipients": [domain],  # domain-level catch-all
        "ScanEnabled": True,
        "Actions": actions,
    }

    if not existing:
        try:
            ses.create_receipt_rule(
                RuleSetName=rule_set_name,
                Rule=rule_def,
            )
            logger.info(f"Created SES receipt rule {rule_name} in set {rule_set_name}")
        except ClientError as e:
            # Attempt to auto-fix InvalidS3Configuration by applying SES PutObject bucket policy, then retry
            err_code = getattr(e, "response", {}).get("Error", {}).get("Code")
            if err_code == "InvalidS3Configuration":
                try:
                    # Discover account ID for aws:Referer condition
                    sts = boto3.client(
                        "sts",
                        aws_access_key_id=access_key or settings.AWS_KEY,
                        aws_secret_access_key=secret_key or settings.AWS_SECRET,
                        region_name=region,
                    )
                    account_id = sts.get_caller_identity().get("Account")

                    # Try to set a minimal bucket policy if none exists
                    s3c = boto3.client(
                        "s3",
                        aws_access_key_id=access_key or settings.AWS_KEY,
                        aws_secret_access_key=secret_key or settings.AWS_SECRET,
                        region_name=region,
                    )
                    try:
                        s3c.get_bucket_policy(Bucket=s3_bucket)
                        has_policy = True
                    except s3c.exceptions.from_code("NoSuchBucketPolicy"):
                        has_policy = False

                    if not has_policy:
                        policy_str = (
                            "{"
                            '"Version":"2012-10-17","Statement":[{'
                            '"Sid":"AllowSESPuts","Effect":"Allow",'
                            '"Principal":{"Service":"ses.amazonaws.com"},'
                            '"Action":"s3:PutObject",'
                            f'"Resource":"arn:aws:s3:::{s3_bucket}/*",'
                            '"Condition":{"StringEquals":{"aws:Referer":"'
                            f'{account_id}'
                            '"}}'
                            "}]}"
                        )
                        s3c.put_bucket_policy(Bucket=s3_bucket, Policy=policy_str)
                        logger.info(f"Applied SES PutObject policy to bucket {s3_bucket}; retrying rule creation")

                        # Retry rule creation
                        ses.create_receipt_rule(
                            RuleSetName=rule_set_name,
                            Rule=rule_def,
                        )
                        logger.info(f"Created SES receipt rule {rule_name} in set {rule_set_name}")
                    else:
                        try:
                            pol = json.loads(s3c.get_bucket_policy(Bucket=s3_bucket)["Policy"])
                            stmts = pol.get("Statement", [])
                        except Exception:
                            pol = {"Version": "2012-10-17", "Statement": []}
                            stmts = []
                        # Replace existing AllowSESPuts or append a new one
                        new_stmts = [s for s in stmts if s.get("Sid") != "AllowSESPuts"]
                        new_stmts.append({
                            "Sid": "AllowSESPuts",
                            "Effect": "Allow",
                            "Principal": {"Service": "ses.amazonaws.com"},
                            "Action": "s3:PutObject",
                            "Resource": f"arn:aws:s3:::{s3_bucket}/*",
                            "Condition": {"StringEquals": {"aws:Referer": account_id}},
                        })
                        pol["Statement"] = new_stmts
                        s3c.put_bucket_policy(Bucket=s3_bucket, Policy=json.dumps(pol))
                        logger.info(f"Updated bucket policy for {s3_bucket}; retrying rule creation")
                        ses.create_receipt_rule(
                            RuleSetName=rule_set_name,
                            Rule=rule_def,
                        )
                        logger.info(f"Created SES receipt rule {rule_name} in set {rule_set_name}")
                except Exception as pe:
                    logger.error(f"Failed to auto-apply SES S3 policy and create rule {rule_name}: {pe}")
            else:
                logger.error(f"Failed to create receipt rule {rule_name}: {e}")
    else:
        # Update to desired shape (best effort)
        try:
            ses.update_receipt_rule(
                RuleSetName=rule_set_name,
                Rule=rule_def,
            )
            logger.info(f"Updated SES receipt rule {rule_name} in set {rule_set_name}")
        except ClientError as e:
            logger.error(f"Failed to update receipt rule {rule_name}: {e}")

    return rule_set_name, rule_name


def audit_domain_config(
    domain: str,
    region: Optional[str] = None,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    desired_receiving: Optional[Dict[str, Any]] = None,
    desired_topics: Optional[Dict[str, str]] = None,
) -> AuditReport:
    """
    Inspect SES identity verification/DKIM/notifications and receiving rules,
    and produce a boolean checks summary plus detailed items.

    - desired_receiving: {"bucket": str, "prefix": str, "rule_set": str, "rule_name": str}
    - desired_topics: {"bounce": arn, "complaint": arn, "delivery": arn}
      If not provided, will be derived from the EmailDomain model fields.
    """
    region = region or getattr(settings, "AWS_REGION", "us-east-1")
    ses = _get_ses_client(region, access_key, secret_key)

    items: List[AuditItem] = []
    checks: Dict[str, bool] = {}

    # 0) SES account sandbox/production access (region-specific)
    try:
        sesv2 = boto3.client(
            "sesv2",
            aws_access_key_id=access_key or settings.AWS_KEY,
            aws_secret_access_key=secret_key or settings.AWS_SECRET,
            region_name=region,
        )
        acct = sesv2.get_account()
        prod = bool(acct.get("ProductionAccessEnabled", False))
        checks["ses_production_access"] = prod
        items.append(
            AuditItem(
                resource="ses.account.production_access",
                desired={"ProductionAccessEnabled": True},
                current={"ProductionAccessEnabled": prod},
                status="ok" if prod else "drifted",
            )
        )
    except Exception as e:
        checks["ses_production_access"] = False
        items.append(
            AuditItem(
                resource="ses.account.production_access",
                desired={"ProductionAccessEnabled": True},
                current=f"error: {e}",
                status="conflict",
            )
        )

    # Load configured expectations from EmailDomain when available
    try:
        from mojo.apps.aws.models import EmailDomain as _EmailDomain
        _ed = _EmailDomain.objects.filter(name=domain).first()
    except Exception:
        _ed = None

    # Derive desired topics from model if not provided
    if desired_topics is None:
        desired_topics = {}
        if _ed:
            desired_topics = {
                "bounce": getattr(_ed, "sns_topic_bounce_arn", None),
                "complaint": getattr(_ed, "sns_topic_complaint_arn", None),
                "delivery": getattr(_ed, "sns_topic_delivery_arn", None),
            }

    # Derive desired_receiving from model if not provided
    if desired_receiving is None and _ed and getattr(_ed, "receiving_enabled", False) and getattr(_ed, "s3_inbound_bucket", None):
        desired_receiving = {
            "bucket": _ed.s3_inbound_bucket,
            "prefix": _ed.s3_inbound_prefix or "",
            "rule_set": DEFAULT_RULE_SET_NAME,
            "rule_name": f"mojo-{domain}-catchall",
            "inbound_topic_arn": getattr(_ed, "sns_topic_inbound_arn", None),
        }

    # 1) Identity verification
    try:
        ver = ses.get_identity_verification_attributes(Identities=[domain])
        vstatus = (ver.get("VerificationAttributes", {}).get(domain, {}) or {}).get("VerificationStatus")
        item_status = "ok" if vstatus == "Success" else "drifted"
        items.append(
            AuditItem(
                resource="ses.identity.verification",
                desired="Success",
                current=vstatus,
                status=item_status,
            )
        )
        checks["ses_verified"] = (vstatus == "Success")
    except ClientError as e:
        items.append(
            AuditItem(
                resource="ses.identity.verification",
                desired="Success",
                current=f"error: {e}",
                status="conflict",
            )
        )
        checks["ses_verified"] = False

    # 2) DKIM attributes
    try:
        dk = ses.get_identity_dkim_attributes(Identities=[domain])
        dkattrs = (dk.get("DkimAttributes", {}) or {}).get(domain, {}) or {}
        current_dkim = {
            "Enabled": dkattrs.get("DkimEnabled"),
            "VerificationStatus": dkattrs.get("DkimVerificationStatus"),
        }
        desired_dkim = {"Enabled": True, "VerificationStatus": "Success"}
        item_status = "ok" if current_dkim == desired_dkim else "drifted"
        items.append(
            AuditItem(
                resource="ses.identity.dkim",
                desired=desired_dkim,
                current=current_dkim,
                status=item_status,
            )
        )
        checks["dkim_verified"] = (current_dkim.get("Enabled") is True and current_dkim.get("VerificationStatus") == "Success")
    except ClientError as e:
        items.append(
            AuditItem(
                resource="ses.identity.dkim",
                desired={"Enabled": True, "VerificationStatus": "Success"},
                current=f"error: {e}",
                status="conflict",
            )
        )
        checks["dkim_verified"] = False

    # 3) Notification topics mapping (SES identity)
    try:
        na = ses.get_identity_notification_attributes(Identities=[domain])
        cur = (na.get("NotificationAttributes", {}) or {}).get(domain, {}) or {}
        current = {
            "BounceTopic": cur.get("BounceTopic"),
            "ComplaintTopic": cur.get("ComplaintTopic"),
            "DeliveryTopic": cur.get("DeliveryTopic"),
        }
        desired = {
            "BounceTopic": desired_topics.get("bounce"),
            "ComplaintTopic": desired_topics.get("complaint"),
            "DeliveryTopic": desired_topics.get("delivery"),
        }
        mapping_ok = True
        for k in ("BounceTopic", "ComplaintTopic", "DeliveryTopic"):
            # ok only if both are equal (including both None)
            if desired.get(k) != current.get(k):
                mapping_ok = False
                break
        item_status = "ok" if mapping_ok else "drifted"
        items.append(
            AuditItem(
                resource="ses.identity.notification_topics",
                desired=desired,
                current=current,
                status=item_status,
            )
        )
        checks["notification_topics_ok"] = mapping_ok
    except ClientError as e:
        items.append(
            AuditItem(
                resource="ses.identity.notification_topics",
                desired=desired_topics or {},
                current=f"error: {e}",
                status="conflict",
            )
        )
        checks["notification_topics_ok"] = False

    # 4) Receipt rule (S3 and SNS actions) and S3 bucket existence
    checks["receiving_rule_s3_ok"] = False
    checks["receiving_rule_sns_ok"] = False
    checks["s3_bucket_exists"] = False
    if desired_receiving:
        rs_name = desired_receiving.get("rule_set") or DEFAULT_RULE_SET_NAME
        rule_name = desired_receiving.get("rule_name") or f"mojo-{domain}-catchall"
        want_bucket = desired_receiving.get("bucket")
        want_prefix = desired_receiving.get("prefix") or ""
        want_inbound_arn = desired_receiving.get("inbound_topic_arn")

        # S3 bucket head check (read-only)
        try:
            s3 = boto3.client(
                "s3",
                aws_access_key_id=access_key or settings.AWS_KEY,
                aws_secret_access_key=secret_key or settings.AWS_SECRET,
                region_name=region,
            )
            s3.head_bucket(Bucket=want_bucket)
            checks["s3_bucket_exists"] = True
            items.append(
                AuditItem(
                    resource=f"s3.bucket.exists.{want_bucket}",
                    desired={"Exists": True},
                    current={"Exists": True},
                    status="ok",
                )
            )
        except Exception as e:
            items.append(
                AuditItem(
                    resource=f"s3.bucket.exists.{want_bucket}",
                    desired={"Exists": True},
                    current=f"error: {e}",
                    status="missing",
                )
            )
            checks["s3_bucket_exists"] = False

        try:
            rs = ses.describe_receipt_rule_set(RuleSetName=rs_name)
            rules = {r.get("Name"): r for r in rs.get("Rules", [])}
            current_rule = rules.get(rule_name)
            if current_rule:
                # Pull S3Action and SNSAction
                s3_action = next((a.get("S3Action") for a in current_rule.get("Actions", []) if "S3Action" in a), {}) or {}
                sns_action = next((a.get("SNSAction") for a in current_rule.get("Actions", []) if "SNSAction" in a), {}) or {}
                recipients = current_rule.get("Recipients", []) or []

                s3_ok = (want_bucket == s3_action.get("BucketName")) and ((want_prefix or "") == (s3_action.get("ObjectKeyPrefix") or ""))
                sns_ok = (want_inbound_arn is None) or (want_inbound_arn == sns_action.get("TopicArn"))
                rec_ok = (domain in recipients)

                current_view = {
                    "Recipients": recipients,
                    "BucketName": s3_action.get("BucketName"),
                    "ObjectKeyPrefix": s3_action.get("ObjectKeyPrefix"),
                    "SnsTopicArn": sns_action.get("TopicArn"),
                }
                desired_view = {
                    "Recipients": [domain],
                    "BucketName": want_bucket,
                    "ObjectKeyPrefix": want_prefix,
                    "SnsTopicArn": want_inbound_arn,
                }

                # S3 comparison item
                items.append(
                    AuditItem(
                        resource=f"ses.receipt_rule.s3.{rs_name}.{rule_name}",
                        desired={"Recipients": [domain], "BucketName": want_bucket, "ObjectKeyPrefix": want_prefix},
                        current={"Recipients": recipients, "BucketName": s3_action.get("BucketName"), "ObjectKeyPrefix": s3_action.get("ObjectKeyPrefix")},
                        status="ok" if (s3_ok and rec_ok) else "drifted",
                    )
                )
                # SNS comparison item
                items.append(
                    AuditItem(
                        resource=f"ses.receipt_rule.sns.{rs_name}.{rule_name}",
                        desired={"SnsTopicArn": want_inbound_arn},
                        current={"SnsTopicArn": sns_action.get("TopicArn")},
                        status="ok" if sns_ok else "drifted",
                    )
                )

                checks["receiving_rule_s3_ok"] = bool(s3_ok and rec_ok)
                checks["receiving_rule_sns_ok"] = bool(sns_ok)
            else:
                items.append(
                    AuditItem(
                        resource=f"ses.receipt_rule.{rs_name}.{rule_name}",
                        desired={"Recipients": [domain], "BucketName": want_bucket, "ObjectKeyPrefix": want_prefix, "SnsTopicArn": want_inbound_arn},
                        current=None,
                        status="missing",
                    )
                )
                checks["receiving_rule_s3_ok"] = False
                checks["receiving_rule_sns_ok"] = False
        except ClientError as e:
            items.append(
                AuditItem(
                    resource=f"ses.receipt_rule.{rs_name}",
                    desired=desired_receiving,
                    current=f"error: {e}",
                    status="conflict",
                )
            )
            checks["receiving_rule_s3_ok"] = False
            checks["receiving_rule_sns_ok"] = False

    # 5) SNS topics existence and subscription status for configured ARNs
    # Initialize as None so we can detect "no expectations provided"
    checks["sns_topics_exist"] = None
    checks["sns_subscriptions_confirmed"] = None
    try:
        sns = boto3.client(
            "sns",
            aws_access_key_id=access_key or settings.AWS_KEY,
            aws_secret_access_key=secret_key or settings.AWS_SECRET,
            region_name=region,
        )
        # Include bounce/complaint/delivery + inbound (from desired_receiving) if present
        topic_map: Dict[str, Optional[str]] = {
            "bounce": desired_topics.get("bounce"),
            "complaint": desired_topics.get("complaint"),
            "delivery": desired_topics.get("delivery"),
        }
        if desired_receiving and desired_receiving.get("inbound_topic_arn"):
            topic_map["inbound"] = desired_receiving.get("inbound_topic_arn")

        for key, arn in topic_map.items():
            if not arn:
                # If we expect no ARN, treat as OK only if SES mapping is also None (handled above).
                continue
            exists_ok = False
            subs_ok = False
            try:
                sns.get_topic_attributes(TopicArn=arn)
                exists_ok = True
            except Exception as e:
                items.append(
                    AuditItem(
                        resource=f"sns.topic.exists.{key}",
                        desired={"TopicArn": arn},
                        current=f"error: {e}",
                        status="missing",
                    )
                )
                exists_ok = False

            if exists_ok:
                # Check subscriptions
                try:
                    subs = sns.list_subscriptions_by_topic(TopicArn=arn).get("Subscriptions", []) or []
                    # Confirm at least one confirmed HTTPS subscription
                    confirmed = False
                    for s in subs:
                        proto = (s.get("Protocol") or "").lower()
                        pending = s.get("PendingConfirmation")
                        # PendingConfirmation may be 'true'/'false' or boolean
                        is_pending = (str(pending).lower() == "true")
                        if proto == "https" and not is_pending:
                            confirmed = True
                            break
                    subs_ok = confirmed
                    items.append(
                        AuditItem(
                            resource=f"sns.topic.subscriptions.{key}",
                            desired={"ConfirmedHttpsSubscription": True},
                            current={"ConfirmedHttpsSubscription": confirmed},
                            status="ok" if confirmed else "drifted",
                        )
                    )
                except Exception as e:
                    items.append(
                        AuditItem(
                            resource=f"sns.topic.subscriptions.{key}",
                            desired={"ConfirmedHttpsSubscription": True},
                            current=f"error: {e}",
                            status="conflict",
                        )
                    )
                    subs_ok = False

            checks["sns_topics_exist"] = (exists_ok if checks["sns_topics_exist"] is None else (checks["sns_topics_exist"] and exists_ok))
            checks["sns_subscriptions_confirmed"] = (subs_ok if checks["sns_subscriptions_confirmed"] is None else (checks["sns_subscriptions_confirmed"] and subs_ok))

        # Finalize: if no SNS topics were expected (no ARNs provided), set to False instead of defaulting to True
        if checks["sns_topics_exist"] is None:
            checks["sns_topics_exist"] = False
        if checks["sns_subscriptions_confirmed"] is None:
            checks["sns_subscriptions_confirmed"] = False
    except Exception:
        # If SNS client init fails, mark as unknown/false
        checks["sns_topics_exist"] = False
        checks["sns_subscriptions_confirmed"] = False

    # Overall status
    overall = "ok"
    if any(it.status == "conflict" for it in items):
        overall = "conflict"
    elif any(it.status in ("drifted", "missing") for it in items):
        overall = "drifted"

    return AuditReport(
        domain=domain,
        region=region,
        status=overall,
        items=items,
        checks=checks,
        audit_pass=(overall == "ok"),
    )


def reconcile_domain_config(
    domain: str,
    region: str,
    receiving_enabled: bool,
    s3_bucket: Optional[str],
    s3_prefix: Optional[str],
    endpoints: Optional[SnsEndpoints] = None,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    ensure_mail_from: bool = False,
    mail_from_subdomain: str = "feedback",
) -> OnboardResult:
    """
    Attempt to bring the SES identity into alignment:
    - Ensure SNS topics and notification mappings
    - Ensure domain-level receipt rule (catch-all) if receiving_enabled
    - Optionally enable MAIL FROM
    This does NOT modify DNS. Use build_required_dns_records and your DNS manager (GoDaddy or Route 53) for that.
    """
    # Derive endpoints from EmailDomain.metadata if not provided
    endpoints = endpoints or SnsEndpoints()
    if not any([endpoints.bounce, endpoints.complaint, endpoints.delivery, endpoints.inbound]):
        try:
            from mojo.apps.aws.models import EmailDomain as _EmailDomain
            _ed = _EmailDomain.objects.filter(name=domain).first()
            if _ed and isinstance(getattr(_ed, "metadata", None), dict):
                meta = _ed.metadata or {}
                endpoints = SnsEndpoints(
                    bounce=meta.get("bounce_endpoint") or meta.get("sns_bounce_endpoint"),
                    complaint=meta.get("complaint_endpoint") or meta.get("sns_complaint_endpoint"),
                    delivery=meta.get("delivery_endpoint") or meta.get("sns_delivery_endpoint"),
                    inbound=meta.get("inbound_endpoint") or meta.get("sns_inbound_endpoint"),
                )
        except Exception:
            pass
    result = OnboardResult(domain=domain, region=region)

    # Ensure SNS topics (and subscriptions if endpoints provided)
    topic_arns = ensure_sns_topics_and_subscriptions(
        domain=domain,
        endpoints=endpoints,
        region=region,
        access_key=access_key,
        secret_key=secret_key,
    )
    result.topic_arns = topic_arns
    # Persist topic ARNs on EmailDomain model if available
    try:
        from mojo.apps.aws.models import EmailDomain as _EmailDomain
        _ed = _EmailDomain.objects.filter(name=domain).first()
        if _ed:
            _updates = {}
            if topic_arns.get("bounce") and getattr(_ed, "sns_topic_bounce_arn", None) != topic_arns["bounce"]:
                _updates["sns_topic_bounce_arn"] = topic_arns["bounce"]
            if topic_arns.get("complaint") and getattr(_ed, "sns_topic_complaint_arn", None) != topic_arns["complaint"]:
                _updates["sns_topic_complaint_arn"] = topic_arns["complaint"]
            if topic_arns.get("delivery") and getattr(_ed, "sns_topic_delivery_arn", None) != topic_arns["delivery"]:
                _updates["sns_topic_delivery_arn"] = topic_arns["delivery"]
            if topic_arns.get("inbound") and getattr(_ed, "sns_topic_inbound_arn", None) != topic_arns["inbound"]:
                _updates["sns_topic_inbound_arn"] = topic_arns["inbound"]
            if _updates:
                for _k, _v in _updates.items():
                    setattr(_ed, _k, _v)
                _ed.save(update_fields=list(_updates.keys()) + ["modified"])
    except Exception as _e:
        logger.warning(f"Failed to persist topic ARNs for domain {domain}: {_e}")

    # Map notifications (bounce/complaint/delivery)
    map_identity_notification_topics(
        domain=domain,
        topic_arns=topic_arns,
        region=region,
        access_key=access_key,
        secret_key=secret_key,
    )

    # Ensure DKIM signing is enabled once verification is successful
    ensure_dkim_enabled(
        domain=domain,
        region=region,
        access_key=access_key,
        secret_key=secret_key,
    )

    # MAIL FROM (optional)
    if ensure_mail_from:
        set_mail_from_domain(
            domain=domain,
            region=region,
            mail_from_subdomain=mail_from_subdomain,
            access_key=access_key,
            secret_key=secret_key,
        )
        result.notes.append("MAIL FROM configured")

    # Receiving (optional)
    if receiving_enabled:
        if not s3_bucket:
            raise ValueError("receiving_enabled is True, but s3_bucket is not provided")
        rs_name, rule_name = ensure_receiving_catch_all(
            domain=domain,
            s3_bucket=s3_bucket,
            s3_prefix=s3_prefix or "",
            inbound_topic_arn=topic_arns.get("inbound"),
            region=region,
            access_key=access_key,
            secret_key=secret_key,
        )
        result.rule_set = rs_name
        result.receipt_rule = rule_name
        result.notes.append("Receiving catch-all rule ensured")

    return result


def onboard_domain(
    domain: str,
    region: Optional[str] = None,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    receiving_enabled: bool = False,
    s3_bucket: Optional[str] = None,
    s3_prefix: str = "",
    dns_mode: DnsMode = "manual",
    ensure_mail_from: bool = False,
    mail_from_subdomain: str = "feedback",
    endpoints: Optional[SnsEndpoints] = None,
    ttl: int = DEFAULT_TTL,
) -> OnboardResult:
    """
    High-level "one-step" onboarding orchestrator:
    - Request SES domain verification + DKIM tokens
    - Compute required DNS records (caller applies manually or via GoDaddy/Route 53)
    - Ensure SNS topics and notification mappings
    - Optionally configure MAIL FROM
    - Optionally enable receiving (catch-all → S3 + SNS)

    Note: This helper does NOT apply DNS to any provider. It returns `dns_records`.
    """
    region = region or getattr(settings, "AWS_REGION", "us-east-1")
    endpoints = endpoints or SnsEndpoints()

    # Request verification + DKIM
    verification_token, dkim_tokens = _request_ses_verification_and_dkim(
        domain=domain, region=region, access_key=access_key, secret_key=secret_key
    )

    dns_records = build_required_dns_records(
        domain=domain,
        region=region,
        verification_token=verification_token,
        dkim_tokens=dkim_tokens,
        enable_mail_from=ensure_mail_from,
        mail_from_subdomain=mail_from_subdomain,
        ttl=ttl,
    )

    # Ensure AWS-side resources (SNS, notifications, receiving)
    recon = reconcile_domain_config(
        domain=domain,
        region=region,
        receiving_enabled=receiving_enabled,
        s3_bucket=s3_bucket,
        s3_prefix=s3_prefix,
        endpoints=endpoints,
        access_key=access_key,
        secret_key=secret_key,
        ensure_mail_from=ensure_mail_from,
        mail_from_subdomain=mail_from_subdomain,
    )

    return OnboardResult(
        domain=domain,
        region=region,
        verification_token=verification_token,
        dkim_tokens=dkim_tokens,
        dns_records=dns_records,
        topic_arns=recon.topic_arns,
        receipt_rule=recon.receipt_rule,
        rule_set=recon.rule_set,
        notes=recon.notes,
    )


# Optional DNS application helpers (skeletons)
def apply_dns_records_godaddy(
    domain: str,
    records: List[DnsRecord],
    api_key: str,
    api_secret: str,
):
    """
    Apply DNS records using the existing GoDaddy DNSManager helper.
    Caller should pass credentials that map to the domain's registrar account.
    """
    try:
        from mojo.helpers.dns.godaddy import DNSManager  # local helper exists
    except Exception as e:
        raise ImportError("GoDaddy DNSManager not available") from e

    dns = DNSManager(api_key, api_secret)
    if not dns.is_domain_active(domain):
        raise ValueError(f"Domain {domain} is not active in GoDaddy account")

    for r in records:
        # For GoDaddy, record names are relative to the domain
        # e.g., "_amazonses" for "_amazonses.example.com"
        name = r.name.replace(f".{domain}", "")
        # Some providers want quoted TXT data; GoDaddy accepts raw token for SES
        dns.add_record(
            domain=domain,
            record_type=r.type,
            name=name,
            data=r.value,
            ttl=r.ttl,
        )
    return True
