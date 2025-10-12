"""
AWS SES Domain Management Service

This service provides domain onboarding, auditing, and reconciliation functionality
for AWS SES email domains. It encapsulates the complex domain setup process and
provides user-friendly recommendations for configuration issues.

Usage:
    from mojo.apps.aws.services.email_ops import (
        onboard_email_domain,
        audit_email_domain,
        reconcile_email_domain,
        generate_audit_recommendations
    )

    # Onboard a new domain
    result = onboard_email_domain(
        domain_pk=1,
        region="us-east-1",
        receiving_enabled=True,
        s3_bucket="my-emails",
        dns_mode="manual"
    )

    # Audit domain configuration
    audit = audit_email_domain(domain_pk=1, region="us-east-1")
    recommendations = generate_audit_recommendations(audit.report)

    # Fix configuration drift
    reconcile_result = reconcile_email_domain(
        domain_pk=1,
        receiving_enabled=True,
        s3_bucket="my-emails"
    )
"""

from typing import Dict, Any, Optional, List, NamedTuple
from dataclasses import dataclass

from mojo.apps.aws.models import EmailDomain
from mojo.helpers.settings import settings
from mojo.helpers import logit

# Orchestration helpers
from mojo.helpers.aws.ses_domain import (
    onboard_domain,
    audit_domain_config,
    reconcile_domain_config,
    SnsEndpoints,
    DnsRecord,
    apply_dns_records_godaddy,
)

logger = logit.get_logger("email", "email.log")


# Exceptions
class EmailDomainNotFound(Exception):
    pass

class InvalidConfiguration(Exception):
    pass


# Data structures
@dataclass
class OnboardResult:
    domain: str
    region: str
    dns_records: List[Dict[str, Any]]
    dkim_tokens: List[str]
    topic_arns: Dict[str, str]
    receipt_rule: Optional[str]
    rule_set: Optional[str]
    notes: List[str]


@dataclass
class AuditResult:
    domain: str
    region: str
    status: str
    audit_pass: bool
    checks: Dict[str, bool]
    items: List[Any]
    report: Any  # Store original report for recommendations


@dataclass
class ReconcileResult:
    domain: str
    region: str
    topic_arns: Dict[str, str]
    receipt_rule: Optional[str]
    rule_set: Optional[str]
    notes: List[str]


# Helper functions
def _get_domain(domain_pk: int) -> EmailDomain:
    """Get EmailDomain by primary key or raise exception"""
    try:
        return EmailDomain.objects.get(pk=domain_pk)
    except EmailDomain.DoesNotExist:
        raise EmailDomainNotFound(f"EmailDomain not found with pk={domain_pk}")


def _parse_endpoints(payload: Dict[str, Any]) -> SnsEndpoints:
    """Parse SNS endpoints from payload"""
    ep = payload.get("endpoints") or {}
    return SnsEndpoints(
        bounce=ep.get("bounce") or payload.get("bounce") or payload.get("bounce_endpoint"),
        complaint=ep.get("complaint") or payload.get("complaint") or payload.get("complaint_endpoint"),
        delivery=ep.get("delivery") or payload.get("delivery") or payload.get("delivery_endpoint"),
        inbound=ep.get("inbound") or payload.get("inbound") or payload.get("inbound_endpoint"),
    )


def _dns_records_to_dict(records: List[DnsRecord]) -> List[Dict[str, Any]]:
    """Convert DnsRecord objects to dictionaries"""
    return [{"type": r.type, "name": r.name, "value": r.value, "ttl": r.ttl} for r in records]


def _get_aws_credentials(domain: EmailDomain,
                        access_key: Optional[str] = None,
                        secret_key: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """Get AWS credentials with domain/settings fallback"""
    return (
        access_key or domain.aws_key or getattr(settings, "AWS_KEY", None),
        secret_key or domain.aws_secret or getattr(settings, "AWS_SECRET", None)
    )


def generate_audit_recommendations(report) -> List[Dict[str, Any]]:
    """Generate user-friendly recommendations based on audit results"""
    recommendations = []

    for item in report.items:
        resource = item.resource
        status = item.status
        current = str(item.current)

        if status != "conflict":
            continue

        recommendation = {"resource": resource, "severity": "high", "action": "", "explanation": ""}

        # Check for credential/permission issues
        if ("AccessDenied" in current or "not authorized" in current or
            "InvalidSignatureException" in current or "SignatureDoesNotMatch" in current):

            if "ses-smtp-user" in current:
                recommendation.update({
                    "severity": "critical",
                    "action": "Replace SMTP credentials with full AWS API credentials",
                    "explanation": "You're using SMTP-only credentials that can't manage SES settings. You need AWS API credentials with SES permissions to configure domains."
                })
            else:
                recommendation.update({
                    "severity": "critical",
                    "action": "Fix AWS credentials or permissions",
                    "explanation": "Your AWS credentials are invalid, expired, or don't have the required SES permissions. Check your AWS access key and secret key."
                })

        # Resource-specific recommendations
        elif resource == "ses.account.production_access":
            if not item.desired.get("ProductionAccessEnabled"):
                recommendation.update({
                    "action": "Request SES production access",
                    "explanation": "Your SES account is in sandbox mode. You can only send to verified email addresses. Request production access through AWS console to send to any email."
                })

        elif resource == "ses.identity.verification":
            recommendation.update({
                "action": "Verify your domain in AWS SES",
                "explanation": "Add the required DNS TXT record to prove you own this domain. Check AWS SES console for the verification record."
            })

        elif resource == "ses.identity.dkim":
            recommendation.update({
                "action": "Set up DKIM for better email delivery",
                "explanation": "Add DKIM DNS records to improve email authenticity and delivery rates. This helps prevent emails from being marked as spam."
            })

        elif resource == "ses.identity.notification_topics":
            recommendation.update({
                "severity": "medium",
                "action": "Configure bounce and complaint handling",
                "explanation": "Set up SNS topics to track bounced and complained emails. This is required for production email sending."
            })

        elif "s3_bucket" in resource:
            recommendation.update({
                "severity": "medium",
                "action": "Create or configure S3 bucket for incoming emails",
                "explanation": "If you want to receive emails, you need an S3 bucket where AWS will store incoming messages."
            })

        elif "receiving_rule" in resource:
            recommendation.update({
                "severity": "low",
                "action": "Configure email receiving rules",
                "explanation": "Set up SES rules to automatically process incoming emails and store them in S3."
            })

        else:
            recommendation.update({
                "action": "Review AWS SES configuration",
                "explanation": "There's a configuration issue that needs attention. Check the AWS SES console for more details."
            })

        recommendations.append(recommendation)

    # Add overall recommendations based on checks
    checks = report.checks
    if not checks.get("ses_verified") and not any(r["resource"] == "ses.identity.verification" for r in recommendations):
        recommendations.insert(0, {
            "resource": "domain_verification",
            "severity": "critical",
            "action": "Verify your domain ownership first",
            "explanation": "Before you can send emails, you must prove you own this domain by adding a DNS record. This is the first step in email setup."
        })

    return recommendations


# Public API
def onboard_email_domain(
    domain_pk: int,
    *,
    region: Optional[str] = None,
    receiving_enabled: Optional[bool] = None,
    s3_bucket: Optional[str] = None,
    s3_prefix: Optional[str] = None,
    ensure_mail_from: bool = False,
    mail_from_subdomain: str = "feedback",
    dns_mode: Optional[str] = None,
    endpoints: Optional[Dict[str, str]] = None,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    godaddy_key: Optional[str] = None,
    godaddy_secret: Optional[str] = None,
) -> OnboardResult:
    """
    Onboard an email domain for AWS SES.

    Args:
        domain_pk: Primary key of the EmailDomain to onboard
        region: AWS region (defaults to domain.region or settings.AWS_REGION)
        receiving_enabled: Enable email receiving (defaults to domain.receiving_enabled)
        s3_bucket: S3 bucket for incoming emails
        s3_prefix: S3 prefix for incoming emails
        ensure_mail_from: Configure MAIL FROM subdomain
        mail_from_subdomain: Subdomain for MAIL FROM
        dns_mode: "manual" or "godaddy" for DNS record application
        endpoints: SNS endpoint configuration
        access_key, secret_key: AWS credentials override
        godaddy_key, godaddy_secret: GoDaddy API credentials for automatic DNS

    Returns:
        OnboardResult with DNS records, DKIM tokens, and configuration details

    Raises:
        EmailDomainNotFound: If domain_pk doesn't exist
        InvalidConfiguration: If receiving_enabled but no s3_bucket
    """
    domain = _get_domain(domain_pk)

    # Resolve configuration with defaults
    region = region or domain.region or getattr(settings, "AWS_REGION", "us-east-1")
    receiving_enabled = receiving_enabled if receiving_enabled is not None else domain.receiving_enabled
    s3_bucket = s3_bucket or domain.s3_inbound_bucket
    s3_prefix = s3_prefix or domain.s3_inbound_prefix or ""
    dns_mode = dns_mode or domain.dns_mode or "manual"

    if receiving_enabled and not s3_bucket:
        raise InvalidConfiguration("s3_bucket is required when receiving_enabled is true")

    # Get AWS credentials
    access_key_final, secret_key_final = _get_aws_credentials(domain, access_key, secret_key)

    # Parse endpoints
    sns_endpoints = _parse_endpoints(endpoints or {})
    # Persist provided SNS endpoints into EmailDomain.metadata for future runs
    provided = {}
    if sns_endpoints.bounce:
        provided["bounce_endpoint"] = sns_endpoints.bounce
    if sns_endpoints.complaint:
        provided["complaint_endpoint"] = sns_endpoints.complaint
    if sns_endpoints.delivery:
        provided["delivery_endpoint"] = sns_endpoints.delivery
    if sns_endpoints.inbound:
        provided["inbound_endpoint"] = sns_endpoints.inbound
    if provided:
        meta = domain.metadata or {}
        changed = False
        for k, v in provided.items():
            if meta.get(k) != v:
                meta[k] = v
                changed = True
        if changed:
            domain.metadata = meta
            domain.save(update_fields=["metadata", "modified"])

    try:
        result = onboard_domain(
            domain=domain.name,
            region=region,
            access_key=access_key_final,
            secret_key=secret_key_final,
            receiving_enabled=receiving_enabled,
            s3_bucket=s3_bucket,
            s3_prefix=s3_prefix,
            dns_mode=dns_mode,
            ensure_mail_from=ensure_mail_from,
            mail_from_subdomain=mail_from_subdomain,
            endpoints=sns_endpoints,
        )

        # Apply DNS via GoDaddy if requested
        if dns_mode == "godaddy" and godaddy_key and godaddy_secret:
            apply_dns_records_godaddy(
                domain=domain.name,
                records=result.dns_records,
                api_key=godaddy_key,
                api_secret=godaddy_secret,
            )
            result.notes.append("Applied DNS via GoDaddy")
        elif dns_mode == "godaddy":
            result.notes.append("DNS mode is GoDaddy but credentials not provided; returning records for manual apply")

        # Update domain configuration
        updates = {}
        if domain.region != region:
            updates["region"] = region
        if domain.receiving_enabled != receiving_enabled:
            updates["receiving_enabled"] = receiving_enabled
        if s3_bucket and domain.s3_inbound_bucket != s3_bucket:
            updates["s3_inbound_bucket"] = s3_bucket
        if (s3_prefix or "") != (domain.s3_inbound_prefix or ""):
            updates["s3_inbound_prefix"] = s3_prefix
        if dns_mode and domain.dns_mode != dns_mode:
            updates["dns_mode"] = dns_mode

        if updates:
            for k, v in updates.items():
                setattr(domain, k, v)
            domain.save(update_fields=list(updates.keys()) + ["modified"])

        return OnboardResult(
            domain=result.domain,
            region=result.region,
            dns_records=_dns_records_to_dict(result.dns_records),
            dkim_tokens=result.dkim_tokens,
            topic_arns=result.topic_arns,
            receipt_rule=result.receipt_rule,
            rule_set=result.rule_set,
            notes=result.notes,
        )

    except Exception as e:
        logger.error(f"onboard error for domain {domain.name}: {e}")
        raise


def audit_email_domain(
    domain_pk: int,
    *,
    region: Optional[str] = None,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    rule_set: Optional[str] = None,
    rule_name: Optional[str] = None,
) -> AuditResult:
    """
    Audit AWS SES configuration for an email domain.

    Args:
        domain_pk: Primary key of the EmailDomain to audit
        region: AWS region override
        access_key, secret_key: AWS credentials override
        rule_set: SES receiving rule set name
        rule_name: SES receiving rule name

    Returns:
        AuditResult with configuration status and drift analysis

    Raises:
        EmailDomainNotFound: If domain_pk doesn't exist
    """
    domain = _get_domain(domain_pk)

    region = region or domain.region or getattr(settings, "AWS_REGION", "us-east-1")
    access_key_final, secret_key_final = _get_aws_credentials(domain, access_key, secret_key)

    # Configure desired receiving if enabled
    desired_receiving = None
    if domain.receiving_enabled and domain.s3_inbound_bucket:
        desired_receiving = {
            "bucket": domain.s3_inbound_bucket,
            "prefix": domain.s3_inbound_prefix or "",
            "rule_set": rule_set or "mojo-default-receiving",
            "rule_name": rule_name or f"mojo-{domain.name}-catchall",
            "inbound_topic_arn": getattr(domain, "sns_topic_inbound_arn", None),
        }

    try:
        report = audit_domain_config(
            domain=domain.name,
            region=region,
            access_key=access_key_final,
            secret_key=secret_key_final,
            desired_receiving=desired_receiving,
            desired_topics={
                "bounce": getattr(domain, "sns_topic_bounce_arn", None),
                "complaint": getattr(domain, "sns_topic_complaint_arn", None),
                "delivery": getattr(domain, "sns_topic_delivery_arn", None),
            },
        )

        # Update domain status based on audit results
        can_send = bool(
            report.checks.get("ses_verified") and
            report.checks.get("dkim_verified") and
            report.checks.get("ses_production_access") and
            report.checks.get("notification_topics_ok")
        )

        can_recv = False
        if domain.receiving_enabled:
            can_recv = bool(
                report.checks.get("s3_bucket_exists") and
                report.checks.get("receiving_rule_s3_ok") and
                report.checks.get("receiving_rule_sns_ok") and
                report.checks.get("sns_topics_exist") and
                report.checks.get("sns_subscriptions_confirmed")
            )

        # Determine status: "verified" if SES domain is verified, "ready" if fully configured, else "missing"
        if report.checks.get("ses_verified"):
            new_status = "verified"
            if report.audit_pass:
                new_status = "ready"
        else:
            new_status = "missing"

        # Track what we're updating for debugging
        updates = {}
        if domain.status != new_status:
            updates["status"] = new_status
        if domain.can_send != can_send:
            updates["can_send"] = can_send
        if domain.can_recv != can_recv:
            updates["can_recv"] = can_recv

        if updates:
            logger.info(f"Updating domain {domain.name} (pk={domain_pk}) with changes: {updates}")
            for k, v in updates.items():
                setattr(domain, k, v)
            domain.save(update_fields=list(updates.keys()) + ["modified"])
            logger.info(f"Successfully updated domain {domain.name} status to '{new_status}'")
        else:
            logger.info(f"Domain {domain.name} status unchanged: {domain.status}")

        return AuditResult(
            domain=report.domain,
            region=report.region,
            status=report.status,
            audit_pass=report.audit_pass,
            checks=report.checks,
            items=report.items,
            report=report  # Store original for recommendations
        )

    except Exception as e:
        logger.error(f"Audit error for domain {domain.name}: {e}")
        raise


def reconcile_email_domain(
    domain_pk: int,
    *,
    region: Optional[str] = None,
    receiving_enabled: Optional[bool] = None,
    s3_bucket: Optional[str] = None,
    s3_prefix: Optional[str] = None,
    ensure_mail_from: bool = False,
    mail_from_subdomain: str = "feedback",
    endpoints: Optional[Dict[str, str]] = None,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> ReconcileResult:
    """
    Reconcile AWS SES/SNS configuration for an email domain.

    Args:
        domain_pk: Primary key of the EmailDomain to reconcile
        region: AWS region override
        receiving_enabled: Enable email receiving
        s3_bucket: S3 bucket for incoming emails
        s3_prefix: S3 prefix for incoming emails
        ensure_mail_from: Configure MAIL FROM subdomain
        mail_from_subdomain: Subdomain for MAIL FROM
        endpoints: SNS endpoint configuration
        access_key, secret_key: AWS credentials override

    Returns:
        ReconcileResult with applied configuration changes

    Raises:
        EmailDomainNotFound: If domain_pk doesn't exist
        InvalidConfiguration: If receiving_enabled but no s3_bucket
    """
    domain = _get_domain(domain_pk)

    region = region or domain.region or getattr(settings, "AWS_REGION", "us-east-1")
    receiving_enabled = receiving_enabled if receiving_enabled is not None else domain.receiving_enabled
    s3_bucket = s3_bucket or domain.s3_inbound_bucket
    s3_prefix = s3_prefix or domain.s3_inbound_prefix or ""

    if receiving_enabled and not s3_bucket:
        raise InvalidConfiguration("s3_bucket is required when receiving_enabled is true")

    access_key_final, secret_key_final = _get_aws_credentials(domain, access_key, secret_key)
    sns_endpoints = _parse_endpoints(endpoints or {})
    # Persist provided SNS endpoints into EmailDomain.metadata for future runs
    provided = {}
    if sns_endpoints.bounce:
        provided["bounce_endpoint"] = sns_endpoints.bounce
    if sns_endpoints.complaint:
        provided["complaint_endpoint"] = sns_endpoints.complaint
    if sns_endpoints.delivery:
        provided["delivery_endpoint"] = sns_endpoints.delivery
    if sns_endpoints.inbound:
        provided["inbound_endpoint"] = sns_endpoints.inbound
    if provided:
        meta = domain.metadata or {}
        changed = False
        for k, v in provided.items():
            if meta.get(k) != v:
                meta[k] = v
                changed = True
        if changed:
            domain.metadata = meta
            domain.save(update_fields=["metadata", "modified"])

    try:
        result = reconcile_domain_config(
            domain=domain.name,
            region=region,
            receiving_enabled=receiving_enabled,
            s3_bucket=s3_bucket,
            s3_prefix=s3_prefix,
            endpoints=sns_endpoints,
            access_key=access_key_final,
            secret_key=secret_key_final,
            ensure_mail_from=ensure_mail_from,
            mail_from_subdomain=mail_from_subdomain,
        )

        # Update domain configuration
        updates = {}
        if domain.region != region:
            updates["region"] = region
        if domain.receiving_enabled != receiving_enabled:
            updates["receiving_enabled"] = receiving_enabled
        if s3_bucket and domain.s3_inbound_bucket != s3_bucket:
            updates["s3_inbound_bucket"] = s3_bucket
        if (s3_prefix or "") != (domain.s3_inbound_prefix or ""):
            updates["s3_inbound_prefix"] = s3_prefix

        if updates:
            for k, v in updates.items():
                setattr(domain, k, v)
            domain.save(update_fields=list(updates.keys()) + ["modified"])

        return ReconcileResult(
            domain=domain.name,
            region=region,
            topic_arns=result.topic_arns,
            receipt_rule=result.receipt_rule,
            rule_set=result.rule_set,
            notes=result.notes,
        )

    except Exception as e:
        logger.error(f"reconcile error for domain {domain.name}: {e}")
        raise
