from mojo import decorators as md
from mojo.apps.incident.parsers import ossec
from mojo import JsonResponse
from mojo.apps.incident import reporter
from mojo.helpers import logit

@md.POST('ossec/alert')
@md.public_endpoint()
def on_ossec_alert(request):
    ossec_alert = ossec.parse(request.DATA)
    # add the request ip
    ossec_alert["request_ip"] = request.ip
    reporter.report_event(ossec_alert.text, category="ossec", **ossec_alert)
    return JsonResponse({"status": True})


@md.POST('ossec/alert/batch')
@md.public_endpoint()
def on_ossec_alert_batch(request):
    ossec_alerts = ossec.parse(request.DATA)
    for alert in ossec_alerts:
        alert["request_ip"] = request.ip
        reporter.report_event(alert.text, category="ossec", **alert)
    return JsonResponse({"status": True})


@md.POST('ossec/firewall')
@md.public_endpoint()
def on_ossec_firewall(request):
    logit.info("Firewall event received", request.DATA)
    return JsonResponse({"status": True})
