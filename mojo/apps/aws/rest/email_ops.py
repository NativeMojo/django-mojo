from typing import Dict, Any

from mojo import decorators as md
from mojo import JsonResponse
from mojo.helpers import logit

# Use the new email_ops service
from mojo.apps.aws.services.email_ops import (
    onboard_email_domain,
    audit_email_domain,
    reconcile_email_domain,
    generate_audit_recommendations,
    EmailDomainNotFound,
    InvalidConfiguration,
)

logger = logit.get_logger("email", "email.log")


def _get_json(request) -> Dict[str, Any]:
    return getattr(request, "DATA", {}) or {}


@md.URL("email/domain/<int:pk>/onboard")
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
        result = onboard_email_domain(
            domain_pk=pk,
            region=payload.get("region"),
            receiving_enabled=payload.get("receiving_enabled"),
            s3_bucket=payload.get("s3_inbound_bucket"),
            s3_prefix=payload.get("s3_inbound_prefix"),
            ensure_mail_from=bool(payload.get("ensure_mail_from", False)),
            mail_from_subdomain=payload.get("mail_from_subdomain", "feedback"),
            dns_mode=payload.get("dns_mode"),
            endpoints=payload.get("endpoints") or {
                "bounce": payload.get("bounce_endpoint"),
                "complaint": payload.get("complaint_endpoint"),
                "delivery": payload.get("delivery_endpoint"),
                "inbound": payload.get("inbound_endpoint"),
            },
            access_key=payload.get("aws_access_key"),
            secret_key=payload.get("aws_secret_key"),
            godaddy_key=payload.get("godaddy_key"),
            godaddy_secret=payload.get("godaddy_secret"),
        )

        return JsonResponse({
            "status": True,
            "data": {
                "domain": result.domain,
                "region": result.region,
                "dns_records": result.dns_records,
                "dkim_tokens": result.dkim_tokens,
                "topic_arns": result.topic_arns,
                "receipt_rule": result.receipt_rule,
                "rule_set": result.rule_set,
                "notes": result.notes,
            }
        })
    except EmailDomainNotFound:
        return JsonResponse({"error": "EmailDomain not found", "code": 404}, status=404)
    except InvalidConfiguration as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception as e:
        logger.error(f"onboard error for domain pk={pk}: {e}")
        return JsonResponse({"error": str(e)}, status=500)


@md.URL("email/domain/<int:pk>/audit")
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
        result = audit_email_domain(
            domain_pk=pk,
            region=payload.get("region"),
            access_key=payload.get("aws_access_key"),
            secret_key=payload.get("aws_secret_key"),
            rule_set=payload.get("rule_set"),
            rule_name=payload.get("rule_name"),
        )

        return JsonResponse({
            "status": True,
            "data": {
                "domain": result.domain,
                "region": result.region,
                "status": result.status,
                "audit_pass": result.audit_pass,
                "checks": result.checks,
                "items": [
                    {
                        "resource": it.resource,
                        "desired": it.desired,
                        "current": it.current,
                        "status": it.status
                    } for it in result.items
                ],
                "recommendations": generate_audit_recommendations(result.report)
            }
        })
    except EmailDomainNotFound:
        return JsonResponse({"error": "EmailDomain not found", "code": 404}, status=404)
    except Exception as e:
        logger.error(f"audit error for domain pk={pk}: {e}")
        return JsonResponse({"error": str(e)}, status=500)


@md.URL("email/domain/<int:pk>/reconcile")
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
        result = reconcile_email_domain(
            domain_pk=pk,
            region=payload.get("region"),
            receiving_enabled=payload.get("receiving_enabled"),
            s3_bucket=payload.get("s3_inbound_bucket"),
            s3_prefix=payload.get("s3_inbound_prefix"),
            ensure_mail_from=bool(payload.get("ensure_mail_from", False)),
            mail_from_subdomain=payload.get("mail_from_subdomain", "feedback"),
            endpoints=payload.get("endpoints") or {
                "bounce": payload.get("bounce_endpoint"),
                "complaint": payload.get("complaint_endpoint"),
                "delivery": payload.get("delivery_endpoint"),
                "inbound": payload.get("inbound_endpoint"),
            },
            access_key=payload.get("aws_access_key"),
            secret_key=payload.get("aws_secret_key"),
        )

        return JsonResponse({
            "status": True,
            "data": {
                "domain": result.domain,
                "region": result.region,
                "topic_arns": result.topic_arns,
                "receipt_rule": result.receipt_rule,
                "rule_set": result.rule_set,
                "notes": result.notes,
            }
        })
    except EmailDomainNotFound:
        return JsonResponse({"error": "EmailDomain not found", "code": 404}, status=404)
    except InvalidConfiguration as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception as e:
        logger.error(f"reconcile error for domain pk={pk}: {e}")
        return JsonResponse({"error": str(e)}, status=500)
