"""
The consent page and the approve handler.

All of the page logic lives here rather than in the REST module so it can be
exercised in-process — with a private registry and no BASE_URL — and so the two
handlers are the seam the tests drive. The REST wrappers in
``rest/oauth_server.py`` are two lines each.

Two rules shape the validation order:

  - A bad `client_id` or an unregistered `redirect_uri` RENDERS an error and
    never redirects. Redirecting there is exactly the open-redirect the check
    exists to prevent.
  - Every later parameter error redirects to the (now trusted) redirect URI
    with an RFC error code, the echoed `state` and the `iss` (RFC 9207).

The Approve button posts the session bearer in an `Authorization` header. No
cookie is read anywhere on this page, so the POST is CSRF-proof by
construction.
"""
from urllib.parse import urlencode, urlsplit, urlunsplit

from django.http import HttpResponseRedirect

from mojo import errors as merrors
from mojo.apps.account.utils.jwtoken import JWToken

from . import clients, codes, resources

MAX_STATE_LENGTH = 512
DEFAULT_SCOPE = "mcp"
ACCESS_COPY = (
    "Use the Assistant's tools as {email} — the same permissions as your "
    "account; changes still need your approval in the Admin")


class RedirectableError(Exception):
    """A parameter error the client is entitled to receive at its redirect URI."""

    def __init__(self, code, description=""):
        self.code = code
        self.description = description or code
        super().__init__(self.description)


class RenderedError(Exception):
    """A client/redirect failure. Rendered as a page — NEVER redirected."""

    def __init__(self, title, message):
        self.title = title
        self.message = message
        super().__init__(message)


def _single(data, name):
    """One scalar parameter, or None. A repeated parameter is not a value."""
    value = data.get(name)
    if isinstance(value, (list, tuple, dict)):
        return None
    if value is None:
        return None
    return str(value)


def _with_params(uri, params):
    """Append query parameters to a validated redirect URI."""
    parts = urlsplit(uri)
    query = f"{parts.query}&{urlencode(params)}" if parts.query else urlencode(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _display_host(uri):
    """The host (and non-default port) a redirect actually goes to."""
    try:
        parts = urlsplit(uri)
        port = parts.port
    except ValueError:
        return ""
    host = parts.hostname or ""
    if not host:
        return ""
    shown = f"[{host}]" if ":" in host else host
    if port is not None and port not in (80, 443):
        shown = f"{shown}:{port}"
    return shown


def _error_redirect(redirect_uri, origin, code, description, state):
    params = {"error": code, "error_description": description}
    if state:
        params["state"] = state
    params["iss"] = resources.issuer(origin)
    return HttpResponseRedirect(_with_params(redirect_uri, params))


def _request_data(request):
    data = getattr(request, "DATA", None)
    if data is None:
        data = getattr(request, "GET", {})
    return data


def _resolve_client_and_redirect(data):
    """Stage one: the two values a failure must never be redirected on."""
    client_id = _single(data, "client_id")
    try:
        client = clients.resolve_client(client_id)
    except clients.ClientError:
        raise RenderedError(
            "We couldn't verify that app",
            "The application that sent you here is not registered with this "
            "installation, so we can't hand it any access.")
    redirect_uri = _single(data, "redirect_uri")
    if not redirect_uri:
        raise RenderedError(
            "We couldn't verify that app",
            "The application that sent you here did not say where to send you "
            "back, so we can't continue.")
    try:
        # The PRESENTED value gets the same scrutiny as a registered one. The
        # loopback branch of redirect_uri_matches ignores the port, so without
        # this an attacker could smuggle userinfo, a fragment, or a value long
        # enough to overflow OAuthCode.redirect_uri past the match.
        clients.validate_redirect_uri(redirect_uri)
    except ValueError:
        raise RenderedError(
            "We couldn't verify that app",
            "The address the application asked us to return to is not one we "
            "can use, so we can't continue.")
    for registered in client.redirect_uris or []:
        if clients.redirect_uri_matches(registered, redirect_uri):
            return client, redirect_uri
    raise RenderedError(
        "We couldn't verify that app",
        "The application that sent you here asked us to return to an address "
        "it has not registered, so we can't continue.")


def _resolve_resource(origin, data, registry=None):
    """The RFC 8707 `resource`: echoed exactly, or defaulted when unambiguous."""
    enabled = resources.enabled_resources(registry)
    raw = data.get("resource")
    if isinstance(raw, (list, tuple)):
        raise RedirectableError("invalid_request", "resource must be a single value")
    if raw:
        wanted = str(raw)
        for entry in enabled:
            if resources.canonical_url(origin, entry.path) == wanted:
                return entry, wanted
        raise RedirectableError("invalid_target", "unknown resource")
    if len(enabled) == 1:
        entry = enabled[0]
        return entry, resources.canonical_url(origin, entry.path)
    raise RedirectableError("invalid_target", "resource is required")


def _validate_parameters(data, origin, registry=None):
    """Stage two: everything a failure may be redirected on."""
    state = _single(data, "state")
    if state is not None and len(state) > MAX_STATE_LENGTH:
        # Too long to echo safely, and echoing is the only thing state is for.
        raise RenderedError(
            "That request looks malformed",
            "The application sent a sign-in request we can't safely answer. "
            "Please try again from the application.")

    if _single(data, "response_type") != "code":
        raise RedirectableError("unsupported_response_type", "response_type must be code")

    try:
        challenge = codes.validate_pkce_challenge(
            _single(data, "code_challenge_method"), _single(data, "code_challenge"))
    except ValueError as err:
        raise RedirectableError("invalid_request", str(err))

    raw_scope = data.get("scope")
    if isinstance(raw_scope, (list, tuple)):
        raise RedirectableError("invalid_request", "scope must be a single value")
    requested = str(raw_scope).split() if raw_scope else [DEFAULT_SCOPE]
    granted = []
    for token in requested:
        if token != DEFAULT_SCOPE:
            raise RedirectableError("invalid_scope", "only the mcp scope is offered")
        if token not in granted:
            granted.append(token)
    # De-duplicated: "mcp mcp mcp…" is the same grant as "mcp", and the joined
    # string is stored on the code and the grant and rides in every token.
    scope = " ".join(granted) or DEFAULT_SCOPE

    entry, resource = _resolve_resource(origin, data, registry)
    return {
        "state": state or "",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": scope,
        "resource": resource,
        "entry": entry,
    }


def _page_context(request, extra=None):
    from mojo.apps.account.rest.bouncer.views import _auth_context

    ctx = _auth_context(request)
    # The visitor is meant to be signed in already; auth_base's own session
    # check would otherwise bounce them to success_redirect instead of showing
    # the consent question.
    ctx["skip_session_check"] = True
    ctx["page_title"] = "Authorize access"
    ctx["subtitle"] = ""
    ctx["error"] = ""
    ctx.update(extra or {})
    return ctx


def _render(request, ctx, status=200):
    from mojo.apps.account.rest.bouncer.views import _render_with_csp

    response = _render_with_csp(request, "account/oauth_consent.html", ctx)
    response.status_code = status
    # The page names the signed-in person and the app asking for access, and it
    # is one click from a credential. Nothing about it belongs in a shared cache
    # or a back-button replay.
    response["Cache-Control"] = "no-store"
    # Unconditional, not merely via the CSP: AUTH_CSP_ENABLED ships False, and
    # a one-click credential-minting page must not be frameable on the default
    # configuration.
    response["X-Frame-Options"] = "DENY"
    return response


def _render_error(request, title, message, status=400):
    return _render(request, _page_context(request, {
        "error": "1",
        "error_title": title,
        "error_message": message,
    }), status=status)


def _not_available(request):
    return _render_error(
        request,
        "Not available",
        "This installation is not offering remote application access right now.",
        status=404)


def handle_authorize(request, origin, registry=None):
    """GET the consent page. Always returns an HttpResponse."""
    if not resources.is_ready(origin, registry):
        return _not_available(request)

    data = _request_data(request)
    try:
        client, redirect_uri = _resolve_client_and_redirect(data)
    except RenderedError as err:
        return _render_error(request, err.title, err.message)

    try:
        params = _validate_parameters(data, origin, registry)
    except RenderedError as err:
        return _render_error(request, err.title, err.message)
    except RedirectableError as err:
        return _error_redirect(
            redirect_uri, origin, err.code, err.description,
            _single(data, "state") or "")

    approve_payload = {
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "state": params["state"],
        "code_challenge": params["code_challenge"],
        "code_challenge_method": params["code_challenge_method"],
        "scope": params["scope"],
        "resource": params["resource"],
    }
    deny_params = {"error": "access_denied"}
    if params["state"]:
        deny_params["state"] = params["state"]
    deny_params["iss"] = resources.issuer(origin)

    consent_return = "/"
    try:
        consent_return = request.get_full_path()
    except Exception:
        consent_return = f"{resources.SERVER_PATH}/authorize"

    return _render(request, _page_context(request, {
        "client_name": client.client_name or client.client_id,
        "client_id": client.client_id,
        "access_copy": ACCESS_COPY,
        "requested_resource": params["resource"],
        # Anti-phishing. A name is whatever the client typed, so show the two
        # facts it cannot forge — where the credential is being sent, and
        # whether anything vouched for the name — beside it.
        "redirect_host": _display_host(redirect_uri),
        "client_verified_url": client.client_id if client.kind == "cimd" else "",
        "client_unverified": client.kind != "cimd",
        "deny_url": _with_params(redirect_uri, deny_params),
        "approve_url": f"{resources.SERVER_PATH}/approve",
        "approve_payload": approve_payload,
        "consent_return": consent_return,
    }))


def _session_auth_time(request):
    """The approving browser session's auth_time, or None if this is not one.

    The bearer must be an ordinary interactive session token: a `user_api_key`
    JWT (or anything else that decodes) is refused outright, and a legacy token
    minted before `auth_time` shipped has to re-login. That value is copied into
    every access token this grant ever mints, so step-up semantics carry
    through to the resource server instead of being reset by the handoff.
    """
    from mojo.apps.account.services import fresh_auth

    auth_token = getattr(request, "auth_token", None)
    raw = getattr(auth_token, "token", None) if auth_token else None
    if not raw:
        return None
    try:
        payload = JWToken().decode(raw, validate=False)
    except Exception:
        return None
    if payload.get("token_type") != "access":
        return None
    return fresh_auth.token_auth_time(request)


def handle_approve(request, origin, registry=None):
    """POST from the consent page. Returns {"redirect_url": …} or raises."""
    if not resources.is_ready(origin, registry):
        raise merrors.PermissionDeniedException(
            "Remote application access is not available", 404, 404)

    if getattr(request, "bearer", None) != "bearer":
        raise merrors.PermissionDeniedException(
            "An interactive session is required to approve access")
    auth_time = _session_auth_time(request)
    if auth_time is None:
        raise merrors.PermissionDeniedException(
            "An interactive session is required to approve access")

    data = _request_data(request)
    # Re-run every check from scratch against what was POSTed. The page's own
    # values are a convenience, never a trust boundary.
    try:
        client, redirect_uri = _resolve_client_and_redirect(data)
    except RenderedError as err:
        raise merrors.ValueException(err.message)
    try:
        params = _validate_parameters(data, origin, registry)
    except RenderedError as err:
        raise merrors.ValueException(err.message)
    except RedirectableError as err:
        raise merrors.ValueException(err.description)

    raw_code = codes.mint_code(
        request.user, client, redirect_uri, params["code_challenge"],
        params["scope"], params["resource"], auth_time)

    result = {"code": raw_code}
    if params["state"]:
        result["state"] = params["state"]
    result["iss"] = resources.issuer(origin)
    return {"redirect_url": _with_params(redirect_uri, result)}
