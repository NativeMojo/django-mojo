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

from mojo.helpers import logit
from mojo.helpers import request as request_helpers
from mojo.helpers.settings import settings
from mojo.apps.account.services import oauth_server

DEFAULT_PATH = "api/assistant/mcp"


def configured_path(raw=None, append_slash=None):
    """The ONE absolute request path this resource is routed and known by.

    Three places must agree byte for byte or the door breaks in a way nothing
    reports: the ``@md.URL`` route, the OAuth resource registration (whose entry
    ``validate_access`` looks up by the token audience's path, compared against
    ``request.path`` EXACTLY), and the challenge's ``resource_metadata``. So all
    three call this.

    * ``get_static`` (deployment file only), so no database row can move the
      path underneath a running process.
    * ``MOJO_APPEND_SLASH`` deployments serve ``/api/assistant/mcp/``, so the
      slash is appended HERE — and, because the resulting pattern already ends
      in one, ``_register_route``'s own append step leaves it alone.
    * An empty or ``/``-only setting falls back to the default. Honouring it
      would mount this endpoint at the site root, where it would shadow every
      other route and label every root POST as MCP traffic.

    ``raw`` and ``append_slash`` are test seams — a test passes values instead
    of mutating the shared settings singleton.
    """
    if raw is None:
        raw = settings.get_static("ASSISTANT_MCP_PATH", DEFAULT_PATH)
    cleaned = str(raw or "").strip().strip("/")
    if not cleaned:
        logit.error(
            "assistant.mcp",
            f"ASSISTANT_MCP_PATH is empty or '/'-only ({raw!r}); refusing to "
            f"mount the MCP resource server at the site root — falling back to "
            f"{DEFAULT_PATH!r}")
        cleaned = DEFAULT_PATH
    if append_slash is None:
        append_slash = settings.get_static("MOJO_APPEND_SLASH", False, kind="bool")
    return "/" + cleaned + ("/" if append_slash else "")


def resource_path():
    """The absolute request path this resource is routed and registered at."""
    return configured_path()


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

    # `scopes` is a JSONField. Insisting on a list is not pedantry: `"mcp" in
    # "mcpx"` is True for a string, so a row that ever held one would pass a
    # membership test it should fail.
    scopes = grant.scopes if isinstance(grant.scopes, list) else []
    if "mcp" not in scopes:
        return raw_response(
            {"error": "insufficient_scope"}, 403,
            oauth_server.www_authenticate(
                path, error="insufficient_scope", scope="mcp"))

    if not request.user.has_permission(["view_admin", "assistant"]):
        return raw_response({"error": "permission_denied"}, 403)

    return None
