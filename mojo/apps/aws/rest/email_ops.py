from typing import Dict, Any, Optional, List

from mojo import decorators as md
from mojo import JsonResponse

from mojo.apps.aws.models import EmailDomain
from mojo.helpers.settings import settings
from mojo.helpers import logit

# Orchestration helpers (built to leverage existing AWS helpers and prevent duplication)
from mojo.helpers.aws.ses_domain import (
    onboard_domain,
    audit_domain_config,
    reconcile_domain_config,
    SnsEndpoints,
    DnsRecord,
    apply_dns_records_godaddy,
)

logger = logit.get_logger(__name__)


def _get_json(request) -> Dict[str, Any]:
    return getattr(request, "DATA", {}) or {}


def _parse_endpoints(payload: Dict[str, Any]) -> SnsEndpoints:
    """
    Accept either:
      - endpoints: { bounce, complaint, delivery, inbound }
      - or top-level: bounce_endpoint, complaint_endpoint, delivery_endpoint, inbound_endpoint
    """
    ep = payload.get("endpoints") or {}
    return SnsEndpoints(
        bounce=ep.get("bounce") or payload.get("bounce_endpoint"),
        complaint=ep.get("complaint") or payload.get("complaint_endpoint"),
        delivery=ep.get("delivery") or payload.get("delivery_endpoint"),
        inbound=ep.get("inbound") or payload.get("inbound_endpoint"),
    )


def _dns_records_to_dict(records: List[DnsRecord]) -> List[Dict[str, Any]]:
    return [{"type": r.type, "name": r.name, "value": r.value, "ttl": r.ttl} for r in records]


@md.URL("aws/email/domain/<int:pk>/onboard")
@md.requires_perms("manage_aws")
def on_email_domain_onboard(request, pk: int):
    """
    Kick off domain onboarding:
      - Request SES domain verification + DKIM tokens
      - Compute required DNS records (manual or automated via GoDaddy if requested)
      - Ensure SNS topics + notification mappings
      - Optionally enable receiving (catch-all → S3 + SNS)
      - Optionally enable MAIL FROM (returns DNS to add)
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    payload = _get_json(request)
    try:
        domain = EmailDomain.objects.get(pk=pk)
    except EmailDomain.DoesNotExist:
        return JsonResponse({"error": "EmailDomain not found", "code": 404}, status=404)

    # Resolve configuration with overrides from payload
    region = payload.get("region") or domain.region or getattr(settings, "AWS_REGION", "us-east-1")
    receiving_enabled = payload.get("receiving_enabled", domain.receiving_enabled)

    s3_bucket = payload.get("s3_inbound_bucket", domain.s3_inbound_bucket)
    s3_prefix = payload.get("s3_inbound_prefix", domain.s3_inbound_prefix or "")

    ensure_mail_from = bool(payload.get("ensure_mail_from", False))
    mail_from_subdomain = payload.get("mail_from_subdomain", "feedback")

    dns_mode = payload.get("dns_mode", domain.dns_mode or "manual")
    endpoints = _parse_endpoints(payload)

    # Optional AWS creds override (defaults to project settings)
    access_key = payload.get("aws_access_key")
    secret_key = payload.get("aws_secret_key")

    if receiving_enabled and not s3_bucket:
        return JsonResponse({"error": "s3_inbound_bucket is required when receiving_enabled is true"}, status=400)

    try:
        result = onboard_domain(
            domain=domain.name,
            region=region,
            access_key=access_key,
            secret_key=secret_key,
            receiving_enabled=receiving_enabled,
            s3_bucket=s3_bucket,
            s3_prefix=s3_prefix,
            dns_mode=dns_mode,
            ensure_mail_from=ensure_mail_from,
            mail_from_subdomain=mail_from_subdomain,
            endpoints=endpoints,
        )

        # Optionally apply DNS via GoDaddy if requested and credentials provided
        if dns_mode == "godaddy":
            gd_key = payload.get("godaddy_key")
            gd_secret = payload.get("godaddy_secret")
            if gd_key and gd_secret:
                apply_dns_records_godaddy(
                    domain=domain.name,
                    records=result.dns_records,
                    api_key=gd_key,
                    api_secret=gd_secret,
                )
                result.notes.append("Applied DNS via GoDaddy")
            else:
                result.notes.append("DNS mode is GoDaddy but credentials not provided; returning records for manual apply")

        # Persist any effective config changes on the EmailDomain
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

        return JsonResponse({
            "status": True,
            "data": {
                "domain": result.domain,
                "region": result.region,
                "dns_records": _dns_records_to_dict(result.dns_records),
                "dkim_tokens": result.dkim_tokens,
                "topic_arns": result.topic_arns,
                "receipt_rule": result.receipt_rule,
                "rule_set": result.rule_set,
                "notes": result.notes,
            }
        })
    except Exception as e:
        logger.error(f"onboard error for domain {domain.name}: {e}")
        return JsonResponse({"error": str(e)}, status=500)


@md.URL("aws/email/domain/<int:pk>/audit")
@md.requires_perms("manage_aws")
def on_email_domain_audit(request, pk: int):
    """
    Audit SES/SNS/S3 configuration for the domain and return a drift report.
    Uses the model configuration to compute desired receiving.
    """
    if request.method not in ("GET", "POST"):
        return JsonResponse({"error": "Method not allowed"}, status=405)

    payload = _get_json(request) if request.method == "POST" else {}
    try:
        domain = EmailDomain.objects.get(pk=pk)
    except EmailDomain.DoesNotExist:
        return JsonResponse({"error": "EmailDomain not found", "code": 404}, status=404)

    region = payload.get("region") or domain.region or getattr(settings, "AWS_REGION", "us-east-1")
    access_key = payload.get("aws_access_key")
    secret_key = payload.get("aws_secret_key")

    desired_receiving = None
    if domain.receiving_enabled and domain.s3_inbound_bucket:
        desired_receiving = {
            "bucket": domain.s3_inbound_bucket,
            "prefix": domain.s3_inbound_prefix or "",
            "rule_set": payload.get("rule_set") or "mojo-default-receiving",
            "rule_name": payload.get("rule_name") or f"mojo-{domain.name}-catchall",
        }

    try:
        report = audit_domain_config(
            domain=domain.name,
            region=region,
            access_key=access_key,
            secret_key=secret_key,
            desired_receiving=desired_receiving,
            desired_topics=None,  # we treat topics as flexible unless caller provides explicit desired ARNs
        )
        return JsonResponse({
            "status": True,
            "data": {
                "domain": report.domain,
                "region": report.region,
                "status": report.status,
                "items": [
                    {
                        "resource": it.resource,
                        "desired": it.desired,
                        "current": it.current,
                        "status": it.status
                    } for it in report.items
                ]
            }
        })
    except Exception as e:
        logger.error(f"audit error for domain {domain.name}: {e}")
        return JsonResponse({"error": str(e)}, status=500)


@md.URL("aws/email/domain/<int:pk>/reconcile")
@md.requires_perms("manage_aws")
def on_email_domain_reconcile(request, pk: int):
    """
    Attempt to reconcile SES/SNS for the domain:
      - Ensure SNS topics and notification mappings
      - Ensure receiving catch-all rule (if receiving_enabled)
      - Optionally configure MAIL FROM
    Does not modify DNS; use onboarding + DNS mode or apply manually.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    payload = _get_json(request)
    try:
        domain = EmailDomain.objects.get(pk=pk)
    except EmailDomain.DoesNotExist:
        return JsonResponse({"error": "EmailDomain not found", "code": 404}, status=404)

    region = payload.get("region") or domain.region or getattr(settings, "AWS_REGION", "us-east-1")
    receiving_enabled = payload.get("receiving_enabled", domain.receiving_enabled)
    s3_bucket = payload.get("s3_inbound_bucket", domain.s3_inbound_bucket)
    s3_prefix = payload.get("s3_inbound_prefix", domain.s3_inbound_prefix or "")

    ensure_mail_from = bool(payload.get("ensure_mail_from", False))
    mail_from_subdomain = payload.get("mail_from_subdomain", "feedback")

    endpoints = _parse_endpoints(payload)
    access_key = payload.get("aws_access_key")
    secret_key = payload.get("aws_secret_key")

    if receiving_enabled and not s3_bucket:
        return JsonResponse({"error": "s3_inbound_bucket is required when receiving_enabled is true"}, status=400)

    try:
        res = reconcile_domain_config(
            domain=domain.name,
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

        # Persist any effective config changes on the EmailDomain
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

        return JsonResponse({
            "status": True,
            "data": {
                "domain": domain.name,
                "region": region,
                "topic_arns": res.topic_arns,
                "receipt_rule": res.receipt_rule,
                "rule_set": res.rule_set,
                "notes": res.notes,
            }
        })
    except Exception as e:
        logger.error(f"reconcile error for domain {domain.name}: {e}")
        return JsonResponse({"error": str(e)}, status=500)
