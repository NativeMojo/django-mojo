"""The MCP door's own acceptance checks — everything the chokepoint cannot do.

A bad, expired, revoked or wrong-audience token never reaches a view: the auth
middleware refuses it, and ``oauth_server``'s ``validate_access`` stamps the RFC
9728 challenge on that 401. What the middleware CANNOT know is what this module
decides:

* no credential was presented at all;
* a credential was presented but it is not an MCP grant — a browser session
  JWT, a ``user_api_key``, an ``ApiKey``, a group token. The MCP door accepts
  MCP tokens only, so every one of those is a 401 with the challenge that tells
  a spec client where to go;
* the grant lacks the ``mcp`` scope — a 403 ``insufficient_scope``, still with a
  challenge, because a re-authorization with the right scope would fix it;
* the operator holds neither ``view_admin`` nor ``assistant`` — a 403 with NO
  challenge, because re-authenticating cannot fix a permission.

The permission predicate is deliberately the SAME one the chat endpoint applies
(``requires_global_perms('view_admin', 'assistant')``, ANY-of): an operator who
can use the chat can use the door, and nobody else can.
"""
import ujson
from django.http import HttpResponse

from mojo.helpers import request as request_helpers
from mojo.helpers.settings import settings
from mojo.apps.account.services import oauth_server


def resource_path():
    """The absolute request path this resource is routed and registered at.

    ``get_static`` (deployment file only) so the route, the OAuth resource
    registration and the sensitive-body label all derive from one value that no
    database row can move underneath a running process.
    """
    return "/" + settings.get_static(
        "ASSISTANT_MCP_PATH", "api/assistant/mcp").strip("/")


def is_enabled():
    """Read on EVERY request — never cached, so the switch is immediate."""
    return settings.get("ASSISTANT_MCP_ENABLED", False, kind="bool")


def raw_response(payload, status, www_authenticate=None):
    """One raw JSON response, no framework envelope.

    ``mojo.helpers.response.JsonResponse`` injects ``code``/``server``, which a
    spec client would have to ignore — the same reasoning as the OAuth server's
    own ``_raw``. No ``Cache-Control`` is set here: ``MojoMiddleware`` stamps
    ``no-store, no-cache, must-revalidate, max-age=0`` on every response, and a
    value set here would simply be overwritten.
    """
    response = HttpResponse(
        ujson.dumps(payload), content_type="application/json", status=status)
    if www_authenticate:
        response["WWW-Authenticate"] = www_authenticate
    return response


def refusal(request):
    """``None`` when the request may proceed, else the response to return."""
    path = resource_path()
    grant = getattr(request, "oauth_grant", None)

    # `is_key_backed_session` / `is_request_user` are checked although an mcp
    # grant can never be key-backed: a custom AUTH_BEARER_HANDLERS identity that
    # happened to carry an `oauth_grant` attribute must not open this door.
    if (grant is None
            or request_helpers.is_key_backed_session(request)
            or not request_helpers.is_request_user(request)):
        return raw_response(
            {"error": "invalid_token"}, 401,
            oauth_server.www_authenticate(path))

    if "mcp" not in (grant.scopes or []):
        return raw_response(
            {"error": "insufficient_scope"}, 403,
            oauth_server.www_authenticate(
                path, error="insufficient_scope", scope="mcp"))

    if not request.user.has_permission(["view_admin", "assistant"]):
        return raw_response({"error": "permission_denied"}, 403)

    return None
