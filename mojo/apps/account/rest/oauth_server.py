"""
The OAuth 2.1 authorization server's HTTP surface.

Wire shapes here are RAW RFC JSON — no `{status, code, data}` envelope — because
a spec client parses them, not this platform's SDK. `mojo.helpers.response.
JsonResponse` injects `code`/`server`, so these handlers build their own
responses and the dispatcher passes any HttpResponse through unchanged.

Routes are absolute, derived from one constant (`OAUTH_SERVER_PATH`) that also
derives the issuer and the sensitive-body labels:

    /.well-known/oauth-authorization-server/api/account/oauth   (RFC 8414)
    /.well-known/oauth-protected-resource/<resource path>       (RFC 9728)
    /api/account/oauth/authorize | approve | token | revoke | register

Only the path-suffixed discovery forms are served. The root forms stay free for
another product's authorization server on the same host, and the protected-
resource route claims only REGISTERED resource paths — an unregistered one does
not match at all, so a downstream app may publish its own document there.

Every endpoint answers 404 while the server is unconfigured — no BASE_URL, or no
registered resource currently enabled.
"""
import ujson
from django.http import HttpResponse
from django.urls import register_converter
from django.urls.converters import get_converters

from mojo import decorators as md
from mojo.apps.account.services.oauth_server import (
    clients, codes, consent, discovery, resources, tokens,
)

_ROOT = resources.SERVER_PATH


def _raw(payload, status=200):
    """One RFC-shaped JSON response.

    `Access-Control-Allow-Origin: *` is set explicitly rather than left to the
    CORS middleware so these documents keep working if that default ever
    narrows, and `no-store` because two of the three carry credentials.
    """
    response = HttpResponse(
        ujson.dumps(payload), content_type="application/json", status=status)
    response["Access-Control-Allow-Origin"] = "*"
    response["Cache-Control"] = "no-store"
    return response


def _oauth_error(code, description, status=400):
    return _raw({"error": code, "error_description": description}, status)


def _not_found():
    return _raw({"error": "not_found"}, 404)


_RESOURCE_CONVERTER = "mojo_oauth_resource"


class _RegisteredResourceConverter:
    """Claim only the resource paths this installation has registered.

    Django treats a ValueError from `to_python` as NO MATCH — not a 404 —
    so an unclaimed path falls through to whatever urlpattern comes next
    instead of being swallowed here. That is what lets a downstream app
    publish its own document at this well-known URL without patching the
    framework.

    Registration is the ONLY test. The `enabled` callable is deliberately
    not consulted: it ends in a DB-backed settings read, and resolution
    runs before the view's rate limiter.
    """
    regex = ".+"

    def to_python(self, value):
        if resources.resolve("/" + value) is None:
            raise ValueError(value)
        return value

    def to_url(self, value):
        return value


if _RESOURCE_CONVERTER not in get_converters():
    register_converter(_RegisteredResourceConverter, _RESOURCE_CONVERTER)


# --- discovery ------------------------------------------------------------

@md.GET("/.well-known/oauth-authorization-server" + _ROOT)
@md.public_endpoint("RFC 8414 authorization-server metadata")
@md.rate_limit("oauth_discovery", ip_limit=120)
def on_authorization_server_metadata(request):
    origin = resources.public_origin()
    document = discovery.authorization_server_metadata(origin)
    if document is None:
        return _not_found()
    return _raw(document)


@md.GET("/.well-known/oauth-protected-resource/<mojo_oauth_resource:resource>")
@md.public_endpoint("RFC 9728 protected-resource metadata")
@md.rate_limit("oauth_discovery", ip_limit=120)
def on_protected_resource_metadata(request, resource):
    origin = resources.public_origin()
    document = discovery.protected_resource_metadata(origin, "/" + resource)
    if document is None:
        return _not_found()
    return _raw(document)


# --- client registration --------------------------------------------------

@md.POST(f"{_ROOT}/register")
@md.public_endpoint("RFC 7591 dynamic client registration")
@md.strict_rate_limit("oauth_register", ip_limit=10, ip_window=300)
def on_register(request):
    origin = resources.public_origin()
    if not resources.is_ready(origin):
        return _not_found()
    try:
        return _raw(clients.register_client(request.DATA), 201)
    except clients.ClientError as err:
        return _oauth_error(err.code, err.description, 400)


# --- the consent page -----------------------------------------------------

@md.GET(f"{_ROOT}/authorize")
@md.public_endpoint("OAuth 2.1 authorization (consent) page")
@md.strict_rate_limit("oauth_authorize", ip_limit=60)
def on_authorize(request):
    return consent.handle_authorize(request, resources.public_origin())


@md.POST(f"{_ROOT}/approve")
@md.denies_key_backed_session()
@md.requires_auth()
@md.strict_rate_limit("oauth_approve", ip_limit=20)
def on_approve(request):
    # This endpoint is the consent page's own, not an RFC wire shape, so it
    # returns a plain dict and takes the framework envelope.
    return consent.handle_approve(request, resources.public_origin())


# --- token issuance -------------------------------------------------------

def _exchange_code(request, origin):
    raw_code = request.DATA.get("code")
    verifier = request.DATA.get("code_verifier")
    redirect_uri = request.DATA.get("redirect_uri")
    client_id = request.DATA.get("client_id")
    if not raw_code or not verifier or not redirect_uri or not client_id:
        return _oauth_error(
            "invalid_request",
            "code, code_verifier, redirect_uri and client_id are required")

    try:
        client = clients.resolve_client(client_id)
    except clients.ClientError as err:
        return _oauth_error(err.code, err.description, 401)

    try:
        code = codes.consume_code(raw_code, client, redirect_uri, verifier)
    except tokens.TokenError as err:
        return _oauth_error(err.code, err.description, 400)

    if not code.user.is_active:
        # The account was disabled between consent and exchange. Creating the
        # grant would only produce a credential that fails at every door.
        return _oauth_error("invalid_grant", "invalid authorization code")

    wanted = request.DATA.get("resource")
    if wanted and wanted != code.resource:
        return _oauth_error("invalid_target", "resource does not match the code")

    # The resource may have been switched off between consent and exchange.
    entry = resources.resolve(resources.resource_path(code.resource))
    if entry is None or not resources.is_enabled(entry):
        return _oauth_error("invalid_grant", "invalid authorization code")

    scopes = [token for token in (code.scope or "").split() if token]
    grant = tokens.create_grant(
        code.user, client, scopes, code.resource, code.auth_time)
    type(code).objects.filter(pk=code.pk).update(grant=grant)
    try:
        return _raw(tokens.issue_tokens(grant))
    except tokens.RotationLost:
        return _oauth_error("invalid_grant", "invalid authorization code")


def _refresh(request, origin):
    raw_refresh = request.DATA.get("refresh_token")
    client_id = request.DATA.get("client_id")
    if not raw_refresh or not client_id:
        return _oauth_error(
            "invalid_request", "refresh_token and client_id are required")
    try:
        client = clients.resolve_client(client_id)
    except clients.ClientError as err:
        return _oauth_error(err.code, err.description, 401)
    try:
        return _raw(tokens.refresh_grant(
            raw_refresh, client,
            resource=request.DATA.get("resource"),
            scope=request.DATA.get("scope")))
    except tokens.TokenError as err:
        return _oauth_error(err.code, err.description, 400)


@md.POST(f"{_ROOT}/token")
@md.public_endpoint("OAuth 2.1 token endpoint (authorization_code, refresh_token)")
@md.strict_rate_limit("oauth_token", ip_limit=60)
def on_token(request):
    origin = resources.public_origin()
    if not resources.is_ready(origin):
        return _not_found()
    grant_type = request.DATA.get("grant_type")
    if grant_type == "authorization_code":
        return _exchange_code(request, origin)
    if grant_type == "refresh_token":
        return _refresh(request, origin)
    return _oauth_error(
        "unsupported_grant_type", "grant_type must be authorization_code or refresh_token")


# --- revocation -----------------------------------------------------------

@md.POST(f"{_ROOT}/revoke")
@md.public_endpoint("RFC 7009 token revocation")
@md.strict_rate_limit("oauth_revoke", ip_limit=30)
def on_revoke(request):
    origin = resources.public_origin()
    if not resources.is_ready(origin):
        return _not_found()
    raw = request.DATA.get("token")
    client_id = request.DATA.get("client_id")
    if not raw or not client_id:
        return _oauth_error("invalid_request", "token and client_id are required")
    try:
        client = clients.resolve_client(client_id)
    except clients.ClientError:
        # RFC 7009 §2.2: revocation answers 200 regardless. An unknown client
        # must not learn anything from this endpoint either.
        return _raw({})
    tokens.revoke_token(raw, client)
    return _raw({})
