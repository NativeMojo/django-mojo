import mojo.decorators as md
import mojo.errors as merrors
from mojo.apps.account.models import ApiKey


@md.URL('group/apikey')
@md.URL('group/apikey/<int:pk>')
@md.uses_model_security(ApiKey)
def on_group_apikey(request, pk=None):
    return ApiKey.on_rest_request(request, pk)


@md.GET('group/apikey/me')
@md.requires_auth()
def on_group_apikey_me(request):
    """Whoami for the authenticating API key.

    Returns the API key's own identity, group scope, and granted
    permissions — without the token. Lets a key holder confirm the
    token is valid and inspect what it is allowed to do without needing
    any management permission. Used by `PhoneConfig.test_connection()`
    for the mojo SMS provider's connectivity check.

    Requires API key authentication (`Authorization: apikey <token>`).
    A user/JWT-authenticated request has no API key and gets 401 — those
    callers should use `GET /api/user/me` instead.
    """
    api_key = getattr(request, "api_key", None)
    if api_key is None:
        raise merrors.PermissionDeniedException(
            "This endpoint requires API key authentication", 401, 401)
    # Forced "me" graph — never honors a ?graph= override, so the token
    # extra on the default graph can never be reached here.
    return dict(status=True, data=api_key.to_dict(graph="me"))
