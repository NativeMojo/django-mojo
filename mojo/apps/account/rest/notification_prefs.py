"""
Notification preferences REST endpoints.

GET  /api/account/notification/preferences  — return current preferences
POST /api/account/notification/preferences  — partial-update preferences
"""
from mojo import decorators as md
from mojo import errors as merrors
from mojo.apps.account.services.notification_prefs import get_preferences, set_preferences
from mojo.helpers.response import JsonResponse


VALID_CHANNELS = {"in_app", "email", "push"}
MAX_KIND_LENGTH = 64


def _validate_preferences(prefs):
    """
    Validate the incoming preferences payload.
    Raises ValueException on bad input.
    """
    if not isinstance(prefs, dict):
        raise merrors.ValueException("preferences must be a dict")
    for kind, channels in prefs.items():
        if not isinstance(kind, str) or len(kind) > MAX_KIND_LENGTH:
            raise merrors.ValueException(f"Invalid notification kind: {kind}")
        if not isinstance(channels, dict):
            raise merrors.ValueException(f"Value for '{kind}' must be a dict of channel booleans")
        for channel, value in channels.items():
            if channel not in VALID_CHANNELS:
                # Unknown channels are silently ignored (forward-compatible)
                continue
            if not isinstance(value, bool):
                raise merrors.ValueException(f"Channel '{channel}' value must be a boolean")


@md.GET("account/notification/preferences")
@md.requires_auth()
def on_notification_preferences_get(request):
    """Return the user's current notification preferences."""
    prefs = get_preferences(request.user)
    return JsonResponse({"status": True, "data": {"preferences": prefs}})


@md.POST("account/notification/preferences")
@md.requires_auth()
def on_notification_preferences_post(request):
    """Partial-update notification preferences."""
    incoming = request.DATA.get("preferences")
    if incoming is None:
        raise merrors.ValueException("preferences is required")

    _validate_preferences(incoming)

    # Strip unknown channels before saving — only store valid ones
    cleaned = {}
    for kind, channels in incoming.items():
        kind_clean = {}
        for channel, value in channels.items():
            if channel in VALID_CHANNELS and isinstance(value, bool):
                kind_clean[channel] = value
        if kind_clean:
            cleaned[kind] = kind_clean

    updated = set_preferences(request.user, cleaned)
    return JsonResponse({"status": True, "data": {"preferences": updated}})