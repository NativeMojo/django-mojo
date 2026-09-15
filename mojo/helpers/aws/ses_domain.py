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

from mojo.helpers.aws.client import get_client, get_session
from mojo.helpers.aws.ses import EmailSender
from mojo.helpers.aws.sns import SNSTopic, SNSSubscription
from mojo.helpers.aws.s3 import S3Bucket
from mojo.helpers.aws.provider_call import ProviderCallError, ProviderClient, provider_caller, safe_error_detail
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


def _get_ses_client(region: str, access_key: Optional[str], secret_key: Optional[str],
                    client_factory=None):
    factory = client_factory or get_client
    return factory(
        "ses",
        access_key=access_key or settings.AWS_KEY,
        secret_key=secret_key or settings.AWS_SECRET,
        region=region or getattr(settings, "AWS_REGION", "us-east-1"),
    )


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
        if key == "inbound":
            topic.client = ProviderClient(topic.client, "sns")
        if not topic.exists:
            topic.create(display_name=name)
        topic_arns[key] = topic.arn

        # Subscribe HTTPS endpoints if provided
        endpoint = getattr(endpoints, key, None)
        if endpoint:
            sub = SNSSubscription(topic.arn, access_key=access_key, secret_key=secret_key, region=region)
            # idempotent: SNS allows duplicate subscriptions but returns pending conf
            if key == "inbound":
                sub.client = ProviderClient(sub.client, "sns")
            subscribed = sub.subscribe(protocol="https", endpoint=endpoint, return_subscription_arn=False)
            if key == "inbound":
                subscription_arn = subscribed.get("SubscriptionArn") or ""
                if not subscription_arn.startswith("arn:"):
                    raise ReceivingConfigurationError(
                        "Inbound SNS subscription is not confirmed; verify the webhook and reconcile after confirmation")

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
            logger.error("SES call failed operation=ses.set_identity_notification_topic domain=%s type=%s", domain, notif)


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
    except (ClientError, ProviderCallError) as e:
        logger.error("SES call failed operation=ses.set_identity_mail_from_domain domain=%s", domain)


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
    except (ClientError, ProviderCallError) as e:
        logger.error("SES call failed operation=ses.set_identity_dkim_enabled domain=%s", domain)

class ReceivingConfigurationError(ValueError):
    """A safe, actionable receiving configuration conflict for Admin callers."""


def _receiving_rule(domain, bucket, prefix, topic):
    return {
        "Name": f"mojo-{domain}-catchall", "Enabled": True,
        "TlsPolicy": "Optional", "Recipients": [domain], "ScanEnabled": True,
        "Actions": [{"S3Action": {
            "BucketName": bucket, "ObjectKeyPrefix": prefix or "", "TopicArn": topic,
        }}],
    }


def _merge_receiving_statement(document, statement):
    # A malformed policy must never turn into an empty policy during repair.
    if not isinstance(document, dict):
        raise ReceivingConfigurationError("Receiving policy must be a JSON object")
    statements = document.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    if not isinstance(statements, list) or any(not isinstance(s, dict) for s in statements):
        raise ReceivingConfigurationError("Receiving policy has invalid statements; repair it before reconciling")
    result = dict(document)
    result["Statement"] = [s for s in statements if s.get("Sid") != statement["Sid"]]
    result["Statement"].append(statement)
    result.setdefault("Version", "2012-10-17")
    return result


def _ensure_receiving_storage(factory, bucket_name, prefix, region, rule_set, rule_name):
    import hashlib

    identity = factory("sts").get_caller_identity()
    account = identity.get("Account")
    if not isinstance(account, str) or len(account) != 12 or not account.isdigit():
        raise ReceivingConfigurationError("Cannot establish AWS account for receiving permissions")
    partition = (identity.get("Arn") or "arn:aws:").split(":")[1]
    source = f"arn:{partition}:ses:{region}:{account}:receipt-rule-set/{rule_set}:receipt-rule/{rule_name}"
    sid = "MojoSES" + hashlib.sha256(source.encode()).hexdigest()[:24]
    condition = {"StringEquals": {"AWS:SourceAccount": account, "AWS:SourceArn": source}}
    statement = {
        "Sid": sid, "Effect": "Allow", "Principal": {"Service": "ses.amazonaws.com"},
        "Action": "s3:PutObject", "Resource": f"arn:{partition}:s3:::{bucket_name}/{prefix}*",
        "Condition": condition,
    }
    s3 = factory("s3")
    try:
        policy = json.loads(s3.get_bucket_policy(Bucket=bucket_name)["Policy"])
    except (ClientError, ProviderCallError) as error:
        if (getattr(error, "provider_code", None) or getattr(error, "response", {}).get("Error", {}).get("Code")) != "NoSuchBucketPolicy":
            raise
        policy = {"Version": "2012-10-17", "Statement": []}
    desired = _merge_receiving_statement(policy, statement)
    if desired != policy:
        s3.put_bucket_policy(Bucket=bucket_name, Policy=json.dumps(desired))
        actual = json.loads(s3.get_bucket_policy(Bucket=bucket_name)["Policy"])
        if actual != desired:
            raise ReceivingConfigurationError("S3 receiving policy readback differs; reconcile again after resolving concurrent changes")

    # Bucket SSE-KMS is not SES message encryption (S3Action.KmsKeyArn).
    # The latter needs a client-side decryptor which the inbox does not have.
    try:
        encryption = provider_caller.call(
            "s3.get_bucket_encryption",
            lambda: s3.client.get_bucket_encryption(Bucket=bucket_name),
            "s3:GetEncryptionConfiguration")
    except (ClientError, ProviderCallError) as error:
        if (getattr(error, "provider_code", None) or getattr(error, "response", {}).get("Error", {}).get("Code")) == "ServerSideEncryptionConfigurationNotFoundError":
            return
        raise
    for rule in encryption.get("ServerSideEncryptionConfiguration", {}).get("Rules", []):
        default = rule.get("ApplyServerSideEncryptionByDefault", {})
        algorithm = default.get("SSEAlgorithm")
        if algorithm == "AES256":
            continue
        if algorithm not in ("aws:kms", "aws:kms:dsse"):
            raise ReceivingConfigurationError("Unsupported inbound bucket encryption; verify its encryption configuration")
        key_id = default.get("KMSMasterKeyID")
        if not key_id:
            raise ReceivingConfigurationError("Inbound SSE-KMS requires a customer-managed key ARN and SES GenerateDataKey/Decrypt permission")
        kms = factory("kms")
        metadata = kms.describe_key(KeyId=key_id)["KeyMetadata"]
        if metadata.get("KeyManager") != "CUSTOMER" or metadata.get("KeyState") != "Enabled":
            raise ReceivingConfigurationError("Inbound SSE-KMS requires an enabled customer-managed key with SES GenerateDataKey/Decrypt permission")
        key_arn = metadata["Arn"]
        key_policy = json.loads(kms.get_key_policy(KeyId=key_arn, PolicyName="default")["Policy"])
        key_statement = dict(statement, Action=["kms:GenerateDataKey", "kms:Decrypt"], Resource="*")
        desired_key_policy = _merge_receiving_statement(key_policy, key_statement)
        if desired_key_policy != key_policy:
            kms.put_key_policy(KeyId=key_arn, PolicyName="default", Policy=json.dumps(desired_key_policy))
            actual = json.loads(kms.get_key_policy(KeyId=key_arn, PolicyName="default")["Policy"])
            if actual != desired_key_policy:
                raise ReceivingConfigurationError("KMS receiving permission readback differs; verify key policy before reconciling")


def ensure_receiving_catch_all(
    domain, s3_bucket, s3_prefix, inbound_topic_arn, region,
    access_key, secret_key, rule_set_name=DEFAULT_RULE_SET_NAME,
):
    """Ensure and verify an active receiving rule; provider failures reach the caller."""
    if not inbound_topic_arn:
        raise ReceivingConfigurationError("An inbound SNS topic is required for receiving; repair topic creation first")
    if any(char in (s3_prefix or "") for char in "*?"):
        raise ReceivingConfigurationError("Inbound S3 prefix cannot contain IAM wildcard characters")
    access_key = access_key or settings.AWS_KEY
    secret_key = secret_key or settings.AWS_SECRET

    def factory(service, **kwargs):
        return ProviderClient(
            get_client(service, access_key=access_key, secret_key=secret_key,
                       region=kwargs.get("region") or region), service)

    ses = factory("ses")
    active = ses.describe_active_receipt_rule_set().get("Metadata", {}).get("Name")
    if active and active != rule_set_name:
        raise ReceivingConfigurationError("Another SES receipt rule set is active; resolve the active set before reconciling")
    try:
        current_set = ses.describe_receipt_rule_set(RuleSetName=rule_set_name)
    except (ClientError, ProviderCallError) as error:
        if (getattr(error, "provider_code", None) or getattr(error, "response", {}).get("Error", {}).get("Code")) != "RuleSetDoesNotExist":
            raise
        try:
            ses.create_receipt_rule_set(RuleSetName=rule_set_name)
        except (ClientError, ProviderCallError) as create_error:
            if (getattr(create_error, "provider_code", None) or getattr(create_error, "response", {}).get("Error", {}).get("Code")) != "AlreadyExists":
                raise
        current_set = ses.describe_receipt_rule_set(RuleSetName=rule_set_name)

    desired = _receiving_rule(domain, s3_bucket, s3_prefix, inbound_topic_arn)
    current = next((r for r in current_set.get("Rules", []) if r.get("Name") == desired["Name"]), None)
    if current:
        for action in current.get("Actions", []):
            old_s3 = action.get("S3Action", {})
            if old_s3.get("KmsKeyArn"):
                raise ReceivingConfigurationError("SES message encryption needs a client-side decryptor; use bucket SSE-KMS for inbox ingestion")
            if old_s3.get("IamRoleArn"):
                raise ReceivingConfigurationError("Receiving rule uses an explicit IAM role; reconcile its permissions and actions manually")
    # S3Bucket owns its provider boundary, including missing-bucket/region discovery.
    bucket = S3Bucket(s3_bucket, client_factory=lambda service, **kw: factory(service, **kw).client,
                      region=region)
    if not bucket._check_exists() and not bucket.create(region=region):
        raise ReceivingConfigurationError("Inbound S3 bucket could not be created")
    _ensure_receiving_storage(factory, s3_bucket, s3_prefix or "", region,
                              rule_set_name, desired["Name"])
    if current != desired:
        if current:
            ses.update_receipt_rule(RuleSetName=rule_set_name, Rule=desired)
        else:
            try:
                ses.create_receipt_rule(RuleSetName=rule_set_name, Rule=desired)
            except (ClientError, ProviderCallError) as error:
                if (getattr(error, "provider_code", None) or getattr(error, "response", {}).get("Error", {}).get("Code")) != "AlreadyExists":
                    raise
                # A racing creator is acceptable only if readback matches.
    if not active:
        # Recheck immediately before activation; never intentionally replace another set.
        active = ses.describe_active_receipt_rule_set().get("Metadata", {}).get("Name")
        if active and active != rule_set_name:
            raise ReceivingConfigurationError("SES active rule set changed during receiving setup")
        if not active:
            ses.set_active_receipt_rule_set(RuleSetName=rule_set_name)
    stored = ses.describe_receipt_rule_set(RuleSetName=rule_set_name)
    actual = next((r for r in stored.get("Rules", []) if r.get("Name") == desired["Name"]), None)
    active = ses.describe_active_receipt_rule_set().get("Metadata", {}).get("Name")
    if actual != desired or active != rule_set_name:
        raise ReceivingConfigurationError("SES receiving rule is not active with the requested configuration after reconciliation")
    return rule_set_name, desired["Name"]


def audit_domain_config(
    domain: str,
    region: Optional[str] = None,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    desired_receiving: Optional[Dict[str, Any]] = None,
    desired_topics: Optional[Dict[str, str]] = None,
    client_factory=None,
) -> AuditReport:
    """
    Inspect SES identity verification/DKIM/notifications and receiving rules,
    and produce a boolean checks summary plus detailed items.

    - desired_receiving: {"bucket": str, "prefix": str, "rule_set": str, "rule_name": str}
    - desired_topics: {"bounce": arn, "complaint": arn, "delivery": arn}
      If not provided, will be derived from the EmailDomain model fields.
    """
    region = region or getattr(settings, "AWS_REGION", "us-east-1")
    ses = _get_ses_client(region, access_key, secret_key, client_factory=client_factory)
    factory = client_factory or get_client

    items: List[AuditItem] = []
    checks: Dict[str, bool] = {}

    # 0) SES account sandbox/production access (region-specific)
    try:
        sesv2 = factory(
            "sesv2",
            access_key=access_key or settings.AWS_KEY,
            secret_key=secret_key or settings.AWS_SECRET,
            region=region,
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
                current=safe_error_detail(e, "sesv2.get_account", "ses:GetAccount"),
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
    except (ClientError, ProviderCallError) as e:
        items.append(
            AuditItem(
                resource="ses.identity.verification",
                desired="Success",
                current=safe_error_detail(
                    e, "ses.get_identity_verification_attributes",
                    "ses:GetIdentityVerificationAttributes"),
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
    except (ClientError, ProviderCallError) as e:
        items.append(
            AuditItem(
                resource="ses.identity.dkim",
                desired={"Enabled": True, "VerificationStatus": "Success"},
                current=safe_error_detail(
                    e, "ses.get_identity_dkim_attributes",
                    "ses:GetIdentityDkimAttributes"),
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
    except (ClientError, ProviderCallError) as e:
        items.append(
            AuditItem(
                resource="ses.identity.notification_topics",
                desired=desired_topics or {},
                current=safe_error_detail(
                    e, "ses.get_identity_notification_attributes",
                    "ses:GetIdentityNotificationAttributes"),
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
            s3 = factory(
                "s3",
                access_key=access_key or settings.AWS_KEY,
                secret_key=secret_key or settings.AWS_SECRET,
                region=region,
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
                    current=safe_error_detail(e, "s3.head_bucket", "s3:ListBucket"),
                    status="missing",
                )
            )
            checks["s3_bucket_exists"] = False

        try:
            rs = ses.describe_receipt_rule_set(RuleSetName=rs_name)
            rules = {r.get("Name"): r for r in rs.get("Rules", [])}
            current_rule = rules.get(rule_name)
            if current_rule:
                # The S3 action notification carries the object location required by ingestion.
                s3_action = next((a.get("S3Action") for a in current_rule.get("Actions", []) if "S3Action" in a), {}) or {}
                sns_action = s3_action
                recipients = current_rule.get("Recipients", []) or []

                desired_rule = _receiving_rule(domain, want_bucket, want_prefix, want_inbound_arn)
                desired_s3 = desired_rule["Actions"][0]["S3Action"]
                s3_ok = (desired_s3["BucketName"] == s3_action.get("BucketName")
                         and desired_s3["ObjectKeyPrefix"] == (s3_action.get("ObjectKeyPrefix") or "")
                         and not s3_action.get("KmsKeyArn"))
                sns_ok = bool(want_inbound_arn) and (want_inbound_arn == s3_action.get("TopicArn"))
                active_name = ses.describe_active_receipt_rule_set().get("Metadata", {}).get("Name")
                rec_ok = (recipients == desired_rule["Recipients"]
                          and current_rule.get("Enabled") == desired_rule["Enabled"]
                          and active_name == rs_name)

                current_view = {
                    "Recipients": recipients,
                    "Enabled": current_rule.get("Enabled"),
                    "ActiveRuleSet": active_name,
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
                        current={"Recipients": recipients, "Enabled": current_rule.get("Enabled"), "ActiveRuleSet": active_name, "BucketName": s3_action.get("BucketName"), "ObjectKeyPrefix": s3_action.get("ObjectKeyPrefix")},
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
        except (ClientError, ProviderCallError) as e:
            items.append(
                AuditItem(
                    resource=f"ses.receipt_rule.{rs_name}",
                    desired=desired_receiving,
                    current=safe_error_detail(
                        e, "ses.describe_receipt_rule_set",
                        "ses:DescribeReceiptRuleSet"),
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
        sns = factory(
            "sns",
            access_key=access_key or settings.AWS_KEY,
            secret_key=secret_key or settings.AWS_SECRET,
            region=region,
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
                        current=safe_error_detail(
                            e, "sns.get_topic_attributes",
                            "sns:GetTopicAttributes"),
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
                            current=safe_error_detail(
                                e, "sns.list_subscriptions_by_topic",
                                "sns:ListSubscriptionsByTopic"),
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
        logger.warning("SES setup failed operation=db.persist_topic_arns domain=%s", domain)

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
    if receiving_enabled:
        dns_records.append(DnsRecord(
            type="MX", name=domain,
            value=f"10 inbound-smtp.{region}.amazonaws.com", ttl=ttl))

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

    # Each PUT replaces a complete RRset. Group first so repeated names keep
    # every requested value, and validate MX values before any DNS write.
    zone_name = domain.rstrip(".").lower()
    grouped = {}
    for record in records:
        name = record.name.rstrip(".")
        if name.lower() == zone_name or name == "@":
            name = "@"
        elif name.lower().endswith(f".{zone_name}"):
            name = name[:-(len(zone_name) + 1)]
        else:
            raise ValueError(f"DNS record '{record.name}' is outside '{domain}'")
        record_type = record.type.upper()
        entry = {"data": record.value, "ttl": record.ttl}
        if record_type == "MX":
            parts = record.value.split()
            if len(parts) != 2 or not parts[0].isdigit() or not 0 <= int(parts[0]) <= 65535:
                raise ValueError(f"MX record '{record.name}' requires a priority and mail server")
            entry["priority"] = int(parts[0])
            entry["data"] = parts[1]
        entries = grouped.setdefault((record_type, name), [])
        if entry not in entries:
            entries.append(entry)

    dns = DNSManager(api_key, api_secret, raise_on_error=True)
    if not dns.is_domain_active(domain):
        raise ValueError(f"Domain {domain} is not active in GoDaddy account")

    for (record_type, name), entries in grouped.items():
        dns.put_records(
            domain=domain,
            record_type=record_type,
            name=name,
            entries=entries,
        )
    return True


def apply_dns_records_route53(
    domain: str,
    records: List[DnsRecord],
    zone_id: Optional[str] = None,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
):
    """
    Apply DNS records to a Route 53 hosted zone — the twin of
    `apply_dns_records_godaddy`.

    Differences from the GoDaddy path that matter:
      - Route 53 speaks FQDNs, so record names are used exactly as
        `build_required_dns_records` produced them (no relative-label rewrite).
      - TXT values MUST be quoted (and 255-chunked). An unquoted SES
        verification token or SPF string is silently invalid, so values are run
        through `route53.format_txt_value` here. `route53.upsert_record` applies
        the same formatting, and it passes already-quoted values through
        untouched, so doing it explicitly is idempotent.
      - MX values carry their priority in the value string
        ("10 feedback-smtp.<region>.amazonses.com"); Route 53 stores the whole
        string, so it is passed through verbatim.
      - An UPSERT REPLACES the whole record set, so records that share a
        (type, name) pair are collapsed into a single change carrying every
        value. Writing them one at a time would leave only the last value.

    Returns True. Raises when no hosted zone can be found for the domain.
    """
    from mojo.helpers.aws import route53

    zone_id = zone_id or route53.find_zone_id(
        domain, access_key=access_key, secret_key=secret_key)
    if not zone_id:
        raise ValueError(f"No Route 53 hosted zone found for {domain}")

    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in records:
        key = ((r.type or "").upper(), r.name)
        entry = grouped.get(key)
        value = route53.format_txt_value(r.value) if key[0] in ("TXT", "SPF") else r.value
        if entry is None:
            grouped[key] = {"ttl": r.ttl, "record_values": [value]}
        elif value not in entry["record_values"]:
            entry["record_values"].append(value)

    for (rtype, name), entry in grouped.items():
        route53.upsert_record(
            zone_id,
            rtype,
            name,
            entry["record_values"],
            ttl=entry["ttl"],
            zone_name=domain,
            access_key=access_key,
            secret_key=secret_key,
        )
    return True
