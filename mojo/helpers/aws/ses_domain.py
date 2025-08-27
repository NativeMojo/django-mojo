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
    topic_arns: Dict[str, str] = {}
    topics = {
        "bounce": f"ses-{domain}-bounce",
        "complaint": f"ses-{domain}-complaint",
        "delivery": f"ses-{domain}-delivery",
        "inbound": f"ses-{domain}-inbound",
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
        raise ValueError(f"Inbound S3 bucket '{s3_bucket}' does not exist")

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
                # "KmsKeyArn": "optional-kms-arn",
                # "TopicArn": inbound_topic_arn,  # S3Action TopicArn is optional; we use a separate SNSAction
            }
        },
        {
            "SNSAction": {
                "TopicArn": inbound_topic_arn,
                "Encoding": "UTF-8",
            }
        },
    ]

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
    Inspect SES identity verification/DKIM/notifications and receiving rules.
    Return a drift/conflict report with current vs desired.
    - desired_receiving: {"bucket": str, "prefix": str, "rule_set": str, "rule_name": str}
    - desired_topics: {"bounce": arn, "complaint": arn, "delivery": arn, "inbound": arn}
    """
    region = region or getattr(settings, "AWS_REGION", "us-east-1")
    ses = _get_ses_client(region, access_key, secret_key)

    items: List[AuditItem] = []

    # Identity verification
    try:
        ver = ses.get_identity_verification_attributes(Identities=[domain])
        vstatus = ver.get("VerificationAttributes", {}).get(domain, {}).get("VerificationStatus")
        items.append(
            AuditItem(
                resource="ses.identity.verification",
                desired="Success",
                current=vstatus,
                status="ok" if vstatus == "Success" else "drifted",
            )
        )
    except ClientError as e:
        items.append(
            AuditItem(
                resource="ses.identity.verification",
                desired="Success",
                current=f"error: {e}",
                status="conflict",
            )
        )

    # DKIM attributes
    try:
        dk = ses.get_identity_dkim_attributes(Identities=[domain])
        dkattrs = dk.get("DkimAttributes", {}).get(domain, {})
        current_dkim = {
            "Enabled": dkattrs.get("DkimEnabled"),
            "VerificationStatus": dkattrs.get("DkimVerificationStatus"),
        }
        desired_dkim = {"Enabled": True, "VerificationStatus": "Success"}
        status = "ok" if current_dkim == desired_dkim else "drifted"
        items.append(
            AuditItem(
                resource="ses.identity.dkim",
                desired=desired_dkim,
                current=current_dkim,
                status=status,
            )
        )
    except ClientError as e:
        items.append(
            AuditItem(
                resource="ses.identity.dkim",
                desired={"Enabled": True, "VerificationStatus": "Success"},
                current=f"error: {e}",
                status="conflict",
            )
        )

    # Notification topics
    try:
        na = ses.get_identity_notification_attributes(Identities=[domain])
        cur = na.get("NotificationAttributes", {}).get(domain, {})
        desired = {
            "BounceTopic": desired_topics.get("bounce") if desired_topics else None,
            "ComplaintTopic": desired_topics.get("complaint") if desired_topics else None,
            "DeliveryTopic": desired_topics.get("delivery") if desired_topics else None,
        }
        current = {
            "BounceTopic": cur.get("BounceTopic"),
            "ComplaintTopic": cur.get("ComplaintTopic"),
            "DeliveryTopic": cur.get("DeliveryTopic"),
        }
        status = "ok" if all(
            (desired[k] is None) or (desired[k] == current[k]) for k in desired
        ) else "drifted"
        items.append(
            AuditItem(
                resource="ses.identity.notification_topics",
                desired=desired,
                current=current,
                status=status,
            )
        )
    except ClientError as e:
        items.append(
            AuditItem(
                resource="ses.identity.notification_topics",
                desired=desired_topics or {},
                current=f"error: {e}",
                status="conflict",
            )
        )

    # Receipt rules (optional)
    if desired_receiving:
        try:
            rs_name = desired_receiving.get("rule_set") or DEFAULT_RULE_SET_NAME
            rule_name = desired_receiving.get("rule_name") or f"mojo-{domain}-catchall"
            rs = ses.describe_receipt_rule_set(RuleSetName=rs_name)
            rules = {r.get("Name"): r for r in rs.get("Rules", [])}
            current_rule = rules.get(rule_name)
            desired_rule = {
                "Recipients": [domain],
                "BucketName": desired_receiving.get("bucket"),
                "ObjectKeyPrefix": desired_receiving.get("prefix"),
            }
            if current_rule:
                # Extract a shallow view for comparison
                s3_action = next((a.get("S3Action") for a in current_rule.get("Actions", []) if "S3Action" in a), {})
                recipients = current_rule.get("Recipients", [])
                current_view = {
                    "Recipients": recipients,
                    "BucketName": s3_action.get("BucketName"),
                    "ObjectKeyPrefix": s3_action.get("ObjectKeyPrefix"),
                }
                status = "ok" if current_view == desired_rule else "drifted"
            else:
                current_view = None
                status = "missing"
            items.append(
                AuditItem(
                    resource=f"ses.receipt_rule.{rs_name}.{rule_name}",
                    desired=desired_rule,
                    current=current_view,
                    status=status,
                )
            )
        except ClientError as e:
            items.append(
                AuditItem(
                    resource="ses.receipt_rule",
                    desired=desired_receiving,
                    current=f"error: {e}",
                    status="conflict",
                )
            )

    # Overall status
    overall = "ok"
    if any(it.status == "conflict" for it in items):
        overall = "conflict"
    elif any(it.status in ("drifted", "missing") for it in items):
        overall = "drifted"

    return AuditReport(domain=domain, region=region, status=overall, items=items)


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
    endpoints = endpoints or SnsEndpoints()
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
