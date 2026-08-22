"""The Assistant's MCP endpoint — one absolute route, stateless.

    POST /api/assistant/mcp        (path root: ASSISTANT_MCP_PATH)

Streamable HTTP without the stream: JSON-RPC in, ``application/json`` out, no
``Mcp-Session-Id`` ever issued, no server-initiated SSE. That is what makes the
door ordinary web-application traffic — live on every node behind the load
balancer with nothing extra to run and no sticky sessions.

Order of answering, each step the first thing that can respond:

1. **the switch.** While ``ASSISTANT_MCP_ENABLED`` is off the endpoint answers
   the PROJECT's own ``handler404`` — the same body and status an unknown route
   gets, HTML/JSON negotiation included. It is CALLED, not raised: a raised
   ``Http404`` inside a dispatched view becomes a 500
   (``mojo/decorators/http.py``). Reusing the project handler rather than
   imitating it is the point — a disabled door must not be distinguishable from
   a route that does not exist.
2. **the rate limit**, INSIDE ``_serve`` and therefore after the switch. The
   limiter's block path answers 429 and files a level-5 incident; applied
   outside the gate it would turn a disabled door into an existence oracle at
   the 121st probe, and an incident generator for a feature nobody enabled.
3. **the method.** ``@md.URL`` (every method) is required: a ``@md.POST`` route
   answers GET with the dispatcher's 404, and this endpoint owes a 405 with
   ``Allow: POST``. OPTIONS never arrives — the CORS middleware short-circuits
   it before any view.
4. **the credential**, via ``mcp/auth.py``.
5. **the protocol version header**, then the JSON-RPC envelope.

The envelope is read from ``request.body``, NOT ``request.DATA``: the parser
merges the query string under the body, folds a top-level JSON array into
``DATA.data`` and splits dotted keys — and a JSON-RPC envelope's top-level shape
IS the protocol. Tool ``arguments`` still reach the handlers exactly as the chat
path's ``tool_input`` does.
"""
from django.http import Http404, HttpResponse
from django.urls import get_resolver

from mojo import decorators as md
from mojo.helpers.settings import settings
from mojo.apps.assistant.mcp import auth as mcp_auth
from mojo.apps.assistant.mcp import protocol, server
from mojo.apps.assistant.services import agent


@md.rate_limit("assistant_mcp", ip_limit=120)
def _serve(request):
    """Everything after the enabled gate. Never routed directly."""
    if request.method != "POST":
        response = mcp_auth.raw_response({"error": "method_not_allowed"}, 405)
        response["Allow"] = "POST"
        return response

    refusal = mcp_auth.refusal(request)
    if refusal is not None:
        return refusal

    version = request.META.get("HTTP_MCP_PROTOCOL_VERSION")
    if version and version not in protocol.SUPPORTED_PROTOCOL_VERSIONS:
        return mcp_auth.raw_response({
            "error": "unsupported_protocol_version",
            "supported": list(protocol.SUPPORTED_PROTOCOL_VERSIONS),
        }, 400)

    try:
        raw = request.body
    except Exception:
        # A multipart body was consumed by request.POST in the mojo middleware
        # and is no longer readable. An empty body is a parse error, which is
        # the right answer for a client that did not send a JSON-RPC message.
        raw = b""

    status, payload = server.handle(
        raw, request.user, request.oauth_grant,
        agent._build_request_meta(request), server.server_name())
    if status == 202:
        return HttpResponse(status=202)
    return mcp_auth.raw_response(payload, status)


@md.URL("/" + settings.get_static(
    "ASSISTANT_MCP_PATH", "api/assistant/mcp").strip("/"))
@md.public_endpoint(
    "MCP resource server: the project 404 while disabled, then in-body auth — "
    "an OAuth grant carrying the mcp scope, checked in mcp/auth.py")
def on_assistant_mcp(request):
    if not mcp_auth.is_enabled():
        return get_resolver().resolve_error_handler(404)(request, exception=Http404())
    return _serve(request)
