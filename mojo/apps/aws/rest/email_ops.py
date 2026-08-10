from typing import Dict, Any

from mojo import decorators as md
from mojo import JsonResponse
from mojo.errors import MojoException
from mojo.helpers import logit
from mojo.helpers.aws.provider_call import safe_error_detail

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


def _dnsman_email():
    """
    Return the dnsman email service, or None when the app is not installed.

    dnsman is an optional app; the aws app must keep working without it, so the
    import is guarded rather than made a hard dependency.
    """
    try:
        from mojo.apps.dnsman.services import email as dnsman_email
    except ImportError:
        return None
    return dnsman_email


def _onboard_via_dnsman(pk: int, payload: Dict[str, Any]):
    """
    Onboard through dnsman: SES computes the records, dnsman applies them to
    whichever provider actually hosts the zone, using the domain's linked
    DnsCredential. No provider API secret travels in the request body.
    """
    service = _dnsman_email()
    if service is None:
        return JsonResponse(
            {"error": "use_dnsman requires the dnsman app to be installed"}, status=400)

    result = service.onboard_email_domain(
        pk,
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
            "provider": result.provider,
            "dns_records": result.dns_records,
            "dkim_tokens": result.dkim_tokens,
            "topic_arns": result.topic_arns,
            "receipt_rule": result.receipt_rule,
            "rule_set": result.rule_set,
            "notes": result.notes,
            "applied": result.applied,
        }
    })


@md.URL("email/domain/<int:pk>/onboard")
@md.requires_global_perms("manage_aws", "comms")
def on_email_domain_onboard(request, pk: int):
    """
    Kick off domain onboarding:
      - Request SES domain verification + DKIM tokens
      - Compute required DNS records (manual or automated via GoDaddy if requested)
      - Ensure SNS topics + notification mappings
      - Optionally enable receiving (catch-all → S3 + SNS)
      - Optionally enable MAIL FROM (returns DNS to add)

    Pass `use_dnsman: true` to apply the records through dnsman instead: the
    provider is chosen from the dnsman Domain row and the credential comes from
    the linked DnsCredential. The `godaddy_key` / `godaddy_secret` parameters
    are DEPRECATED (they put a provider API secret in the request body) but
    still work.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    payload = _get_json(request)

    try:
        if payload.get("use_dnsman"):
            return _onboard_via_dnsman(pk, payload)

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
    except MojoException as e:
        # dnsman speaks MojoException — carry its status through instead of
        # flattening "this domain is not managed here" into a 500.
        return JsonResponse({"error": e.reason, "code": e.code}, status=e.status)
    except Exception as e:
        failure = safe_error_detail(e, "ses.onboard")
        logger.error(
            "SES onboarding failed operation=%s provider_code=%s domain_id=%s",
            failure.get("operation"), failure.get("provider_code"), pk)
        return JsonResponse({
            "error": "SES onboarding could not complete safely",
            "failure": failure,
        }, status=500)


@md.URL("email/domain/<int:pk>/audit")
@md.requires_global_perms("manage_aws", "comms")
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
        failure = safe_error_detail(e, "ses.audit")
        logger.error(
            "SES audit failed operation=%s provider_code=%s domain_id=%s",
            failure.get("operation"), failure.get("provider_code"), pk)
        return JsonResponse({
            "error": "SES audit could not complete safely",
            "failure": failure,
        }, status=500)


@md.URL("email/domain/<int:pk>/reconcile")
@md.requires_global_perms("manage_aws", "comms")
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
        failure = safe_error_detail(e, "ses.reconcile")
        logger.error(
            "SES reconcile failed operation=%s provider_code=%s domain_id=%s",
            failure.get("operation"), failure.get("provider_code"), pk)
        return JsonResponse({
            "error": "SES reconciliation could not complete safely",
            "failure": failure,
        }, status=500)
