import logging
from mojo import decorators as md
from mojo.apps.incident.parsers import ossec
from mojo import JsonResponse
from mojo.apps.incident import reporter
from mojo.helpers.settings import settings

logger = logging.getLogger(__name__)

OSSEC_SECRET = settings.get_static("OSSEC_SECRET", None)

_defaults_checked = False

def _ensure_defaults():
    global _defaults_checked
    if not _defaults_checked:
        from mojo.apps.incident.models import RuleSet
        if not RuleSet.objects.filter(category="ossec").exists():
            RuleSet.ensure_default_rules()
        _defaults_checked = True


def _check_ossec_secret(request):
    """
    Validate OSSEC_SECRET if configured. Returns None on success,
    or a JsonResponse to return on failure.
    """
    if OSSEC_SECRET is None:
        return None
    header = request.META.get("HTTP_X_OSSEC_SECRET", "")
    if header != OSSEC_SECRET:
        logger.warning("OSSEC request rejected: invalid secret from %s", request.ip)
        return JsonResponse({"error": "unauthorized"}, status=403)
    return None


@md.POST('ossec/alert')
@md.public_endpoint()
@md.rate_limit("ossec_alert", ip_limit=60)
def on_ossec_alert(request):
    auth_error = _check_ossec_secret(request)
    if auth_error:
        return auth_error
    _ensure_defaults()
    ossec_alert = ossec.parse(request.DATA)

    # Skip if parsing returned None (ignored or malformed alert)
    if not ossec_alert:
        return JsonResponse({"status": True})

    # Add the request IP defensively
    ossec_alert["request_ip"] = request.ip
    ossec_alert["model_name"] = "ossec_rule"
    ossec_alert["model_id"] = ossec_alert.get("rule_id", 1)

    # Use getattr to avoid attribute errors if 'text' is missing
    reporter.report_event(ossec_alert.get("text", ""), category="ossec", scope="ossec", **ossec_alert)
    return JsonResponse({"status": True})


@md.POST('ossec/alert/batch')
@md.public_endpoint()
@md.rate_limit("ossec_alert_batch", ip_limit=100)
def on_ossec_alert_batch(request):
    auth_error = _check_ossec_secret(request)
    if auth_error:
        return auth_error
    _ensure_defaults()
    ossec_alerts = ossec.parse(request.DATA) or []

    for alert in ossec_alerts:
        # Skip None alerts (ignored or malformed)
        if not alert:
            continue

        # Add the request IP defensively
        try:
            alert["request_ip"] = request.ip
        except Exception:
            try:
                setattr(alert, "request_ip", request.ip)
            except Exception:
                pass

        # Ensure model_name/model_id for bundling by OSSEC rule type
        try:
            if "model_name" not in alert:
                alert["model_name"] = "ossec_rule"
            if "model_id" not in alert:
                alert["model_id"] = alert.get("rule_id") if hasattr(alert, "get") else getattr(alert, "rule_id", None)
        except Exception:
            try:
                if not getattr(alert, "model_name", None):
                    setattr(alert, "model_name", "ossec_rule")
                if not getattr(alert, "model_id", None):
                    setattr(alert, "model_id", getattr(alert, "rule_id", None))
            except Exception:
               pass

        # Use getattr to avoid attribute errors if 'text' is missing
        reporter.report_event(getattr(alert, "text", ""), category="ossec", scope="ossec", **alert)

    return JsonResponse({"status": True})
