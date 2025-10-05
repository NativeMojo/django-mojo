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

        # Use getattr to avoid attribute errors if 'text' is missing
        reporter.report_event(getattr(alert, "text", ""), category="ossec", **alert)

    return JsonResponse({"status": True})


@md.POST('ossec/firewall')
@md.public_endpoint()
def on_ossec_firewall(request):
    logit.info("Firewall event received", request.DATA)
    return JsonResponse({"status": True})
