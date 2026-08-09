import json

from django.http import HttpResponse

from mojo import decorators as md
from mojo.apps.account.models import ApiKey
from mojo.apps.incident.services import mojosec


def _json_response(payload, status=200):
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return HttpResponse(body, status=status, content_type="application/json")


@md.POST("mojosec/batch")
def on_mojosec_batch(request):
    api_key = getattr(request, "api_key", None)
    if (request.bearer != "apikey" or not isinstance(api_key, ApiKey) or
            not api_key.group.is_effectively_active() or
            not api_key.has_permission("mojosec_ingest")):
        return _json_response({"error": "unauthorized"}, status=403)
    try:
        batch = mojosec.parse_request_batch(request)
        mojosec.sensor_profile(api_key, batch)
    except mojosec.MojoSecIngestError as err:
        return _json_response({"error": err.reason}, status=err.status)
    return _json_response(mojosec.ingest_batch(api_key, batch))
