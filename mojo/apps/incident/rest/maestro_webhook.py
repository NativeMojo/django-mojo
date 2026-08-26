import json

from mojo import decorators as md
from mojo.errors import PermissionDeniedException, ValueException
from mojo.helpers.crypto.sign import get_signature_header, verify_signature


MAX_CALLBACK_BYTES = 65536


@md.POST('maestro/webhook')
@md.public_endpoint("Maestro workspace callback — HMAC verified with deployment ApiKey")
@md.strict_rate_limit("maestro_webhook", ip_limit=60)
def on_maestro_webhook(request):
    """Receive a signed callback for the deployment's one Maestro integration."""
    if len(request.body or b"") > MAX_CALLBACK_BYTES:
        raise ValueException("invalid payload", 400)
    try:
        payload = json.loads(request.body)
    except Exception:
        raise ValueException("invalid payload", 400)
    if not isinstance(payload, dict):
        raise ValueException("invalid payload", 400)

    from mojo.apps.incident.services import maestro_sync
    try:
        _api_url, key = maestro_sync.get_config()
    except ValueException:
        raise PermissionDeniedException("invalid webhook target", 401, 401)

    header = get_signature_header()
    signature = request.META.get("HTTP_" + header.replace("-", "_").upper())
    if signature is None and hasattr(request, "headers"):
        signature = request.headers.get(header)
    if not signature or not verify_signature(payload, signature, key):
        raise PermissionDeniedException("invalid signature", 401, 401)

    return maestro_sync.handle_webhook(payload)
