import json

from mojo import decorators as md
from mojo.errors import PermissionDeniedException, ValueException
from mojo.helpers.crypto.sign import get_signature_header, verify_signature
from mojo.apps.incident.models import MaestroBoard


@md.POST('maestro/webhook/<str:token>')
@md.public_endpoint("maestro board link webhook — HMAC-verified against the board's link key")
def on_maestro_webhook(request, token=None):
    """Receiver for maestro board webhooks (DM-040).

    Fail-closed: the board is looked up by its unguessable callback token and
    must be active, and the payload must carry a valid X-Mojo-Signature —
    HMAC-SHA256 of the canonical JSON dict keyed by the raw link key (verified
    on the parsed dict, per the link contract, so wire encoding is
    irrelevant). 4xx responses are terminal to maestro (no retry).
    """
    board = MaestroBoard.objects.filter(callback_token=token, is_active=True).first()
    if board is None:
        raise PermissionDeniedException("invalid webhook target", 401, 401)

    try:
        payload = json.loads(request.body)
    except Exception:
        raise ValueException("invalid payload", 400)
    if not isinstance(payload, dict):
        raise ValueException("invalid payload", 400)

    header = get_signature_header()
    signature = request.META.get("HTTP_" + header.replace("-", "_").upper())
    if signature is None and hasattr(request, "headers"):
        signature = request.headers.get(header)
    if not signature or not verify_signature(payload, signature, board.get_secret("link_key")):
        raise PermissionDeniedException("invalid signature", 401, 401)

    from mojo.apps.incident.services import maestro_sync
    return maestro_sync.handle_board_webhook(board, payload)
