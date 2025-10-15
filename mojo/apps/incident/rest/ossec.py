from mojo import decorators as md
from mojo.apps.incident.parsers import ossec
from mojo import JsonResponse
from mojo.apps.incident import reporter
from mojo.helpers import logit

@md.POST('ossec/alert')
@md.public_endpoint()
def on_ossec_alert(request):
    ossec_alert = ossec.parse(request.DATA)

    # Skip if parsing returned None (ignored or malformed alert)
    if not ossec_alert:
        return JsonResponse({"status": True})

    # Add the request IP defensively
    try:
        ossec_alert["request_ip"] = request.ip
    except Exception:
        try:
            setattr(ossec_alert, "request_ip", request.ip)
        except Exception:
            pass

    # Ensure model_name/model_id for bundling by OSSEC rule type
    try:
        if "model_name" not in ossec_alert:
            ossec_alert["model_name"] = "ossec_rule"
        if "model_id" not in ossec_alert:
            ossec_alert["model_id"] = ossec_alert.get("rule_id") if hasattr(ossec_alert, "get") else getattr(ossec_alert, "rule_id", None)
    except Exception:
        try:
            if not getattr(ossec_alert, "model_name", None):
                setattr(ossec_alert, "model_name", "ossec_rule")
            if not getattr(ossec_alert, "model_id", None):
                setattr(ossec_alert, "model_id", getattr(ossec_alert, "rule_id", None))
        except Exception:
            pass

    # Use getattr to avoid attribute errors if 'text' is missing
    reporter.report_event(getattr(ossec_alert, "text", ""), category="ossec", **ossec_alert)
    return JsonResponse({"status": True})


@md.POST('ossec/alert/batch')
@md.public_endpoint()
def on_ossec_alert_batch(request):
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
        reporter.report_event(getattr(alert, "text", ""), category="ossec", **alert)

    return JsonResponse({"status": True})


@md.POST('ossec/firewall')
@md.public_endpoint()
def on_ossec_firewall(request):
    logit.info("Firewall event received", request.DATA)
    return JsonResponse({"status": True})
