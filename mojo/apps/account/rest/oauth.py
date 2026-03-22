"""
OAuth REST endpoints.

Flow:
  GET  /api/auth/oauth/<provider>/begin    -> returns authorization URL
  POST /api/auth/oauth/<provider>/complete -> exchange code, auto-link user, issue JWT

Supported providers: google (more can be added to services/oauth/__init__.py)
"""
from urllib.parse import urlencode, quote

from django.http import HttpResponseRedirect

from mojo import decorators as md
from mojo import errors as merrors
from mojo.apps.account.models import User
from mojo.apps.account.models.oauth import OAuthConnection
from mojo.apps.account.rest.user import jwt_login
from mojo.apps.account.services.oauth import get_provider
from mojo.helpers import logit
from mojo.helpers.response import JsonResponse
from mojo.helpers.settings import settings

def _get_redirect_uri(request, provider_name):
    """Use configured redirect URI or build one from the request origin."""
    oauth_redirect_uri = settings.get("OAUTH_REDIRECT_URI", "")
    if oauth_redirect_uri:
        return oauth_redirect_uri
    origin = request.META.get("HTTP_ORIGIN") or request.DATA.get("origin", "")
    return f"{origin}/auth/oauth/{provider_name}/complete"


def _validate_redirect_uri(request, redirect_uri):
    """
    Validate redirect_uri against the allowlist.

    Sources (combined):
      - ALLOWED_REDIRECT_URLS setting (list of allowed URL prefixes)
      - group.metadata["allowed_redirect_urls"] (traverses parent chain)

    Raises ValueException (400) if the URI is not on the allowlist or if no
    allowlist is configured at all.
    """
    allowed = list(settings.get("ALLOWED_REDIRECT_URLS", []) or [])
    group = getattr(request, "group", None)
    if group:
        group_allowed = group.get_metadata_value("allowed_redirect_urls")
        if group_allowed:
            allowed.extend(group_allowed)

    if not allowed:
        raise merrors.ValueException(
            "redirect_uri is not permitted: no ALLOWED_REDIRECT_URLS configured"
        )

    for prefix in allowed:
        if redirect_uri.startswith(prefix):
            return

    raise merrors.ValueException("redirect_uri is not on the allowlist")


def _find_or_create_user(provider_name, profile):
    """
    Auto-link logic:
    1. Existing OAuthConnection  -> return (user, conn, False)
    2. Matching email on User    -> create OAuthConnection, return (user, conn, False)
    3. Neither                   -> create User + OAuthConnection, return (user, conn, True)

    Path 3 raises PermissionDeniedException if OAUTH_ALLOW_REGISTRATION is False.
    """
    uid = profile["uid"]
    email = profile["email"]
    display_name = profile.get("display_name")

    # 1. Existing connection
    conn = OAuthConnection.objects.filter(
        provider=provider_name, provider_uid=uid
    ).select_related("user").first()
    if conn:
        return conn.user, conn, False

    # 2. Existing user by email — OAuth has confirmed ownership of this address
    user = User.lookup(email=email)
    if user and not user.is_email_verified:
        user.is_email_verified = True
        user.save(update_fields=["is_email_verified", "modified"])
        logit.info("oauth", f"Marked email verified for {user.username} via {provider_name} OAuth")

    # 3. Create new user
    if not user:
        if not settings.get("OAUTH_ALLOW_REGISTRATION", True):
            raise merrors.PermissionDeniedException("Account registration via OAuth is not permitted")
        user = User(email=email)
        user.username = user.generate_username_from_email()
        if display_name:
            user.display_name = display_name
        user.is_email_verified = True
        user.set_unusable_password()
        user.save()
        logit.info("oauth", f"Created new user {user.username} via {provider_name}")
        conn = OAuthConnection.objects.create(
            user=user,
            provider=provider_name,
            provider_uid=uid,
            email=email,
        )
        return user, conn, True

    conn = OAuthConnection.objects.create(
        user=user,
        provider=provider_name,
        provider_uid=uid,
        email=email,
    )
    return user, conn, False


# -----------------------------------------------------------------
# Begin
# -----------------------------------------------------------------

@md.GET("auth/oauth/<str:provider>/begin")
@md.public_endpoint()
def on_oauth_begin(request, provider):
    """Return the provider's authorization URL.

    Optional query parameter:
      redirect_uri — override the default redirect URI; must be on the allowlist
                     (ALLOWED_REDIRECT_URLS setting or group.metadata["allowed_redirect_urls"]).
    """
    try:
        svc = get_provider(provider)
    except ValueError:
        raise merrors.ValueException(f"Unknown provider: {provider}")

    custom_redirect_uri = request.DATA.get("redirect_uri", "")
    if custom_redirect_uri:
        _validate_redirect_uri(request, custom_redirect_uri)
        redirect_uri = custom_redirect_uri
    else:
        redirect_uri = _get_redirect_uri(request, provider)

    if provider == "apple":
        # Apple requires response_mode=form_post so the callback lands on a
        # backend endpoint. Derive it from the request origin so multiple
        # domains work without extra settings. The frontend URI is stored
        # separately so on_apple_callback knows where to bounce the browser.
        origin = request.META.get("HTTP_ORIGIN", "").rstrip("/")
        if not origin:
            host = request.META.get("HTTP_HOST", "")
            scheme = "https" if request.is_secure() else "http"
            origin = f"{scheme}://{host}"
        apple_redirect_uri = f"{origin}/api/auth/oauth/apple/callback"
        state = svc.create_state(extra={"redirect_uri": apple_redirect_uri, "frontend_uri": redirect_uri})
        auth_url = svc.get_auth_url(state=state, redirect_uri=apple_redirect_uri)
    else:
        state = svc.create_state(extra={"redirect_uri": redirect_uri})
        auth_url = svc.get_auth_url(state=state, redirect_uri=redirect_uri)

    return JsonResponse({
        "status": True,
        "data": {
            "auth_url": auth_url,
            "state": state,
        },
    })


# -----------------------------------------------------------------
# Apple callback — receives Apple's form_post, bounces to frontend
# -----------------------------------------------------------------

@md.POST("auth/oauth/apple/callback")
@md.public_endpoint()
def on_apple_callback(request):
    """
    Receives Apple's form_post redirect (code + state as POST body).
    Redirects the browser to the frontend with code + state as query params
    so the JS flow is identical to Google.

    Requires APPLE_CALLBACK_REDIRECT setting — the frontend URL to redirect to.
    """
    code = request.POST.get("code", "")
    state = request.POST.get("state", "")

    if not code or not state:
        raise merrors.ValueException("Missing code or state in Apple callback")

    # Peek at state (without consuming) to find the frontend URI to bounce to.
    # The state is consumed later by on_oauth_complete.
    svc = get_provider("apple")
    state_data = svc.peek_state(state)
    if not state_data:
        raise merrors.PermissionDeniedException("Invalid or expired OAuth state", 401, 401)

    frontend_uri = state_data.get("frontend_uri", "")
    if not frontend_uri:
        raise merrors.ValueException("No frontend_uri in OAuth state")

    params = urlencode({"code": code, "state": state}, quote_via=quote)
    return HttpResponseRedirect(f"{frontend_uri}?{params}")


# -----------------------------------------------------------------
# Complete
# -----------------------------------------------------------------

@md.POST("auth/oauth/<str:provider>/complete")
@md.POST("oauth/<str:provider>/complete")
@md.requires_params("code", "state")
@md.public_endpoint()
def on_oauth_complete(request, provider):
    """Exchange authorization code for tokens, resolve user, issue JWT."""
    try:
        svc = get_provider(provider)
    except ValueError:
        raise merrors.ValueException(f"Unknown provider: {provider}")

    state = request.DATA.get("state")
    state_data = svc.consume_state(state)
    if state_data is None:
        raise merrors.PermissionDeniedException("Invalid or expired OAuth state", 401, 401)

    code = request.DATA.get("code")
    # redirect_uri is bound to the state — use it for the token exchange.
    # Falls back to the default if a legacy state has no stored URI.
    redirect_uri = state_data.get("redirect_uri") or _get_redirect_uri(request, provider)

    try:
        tokens = svc.exchange_code(code=code, redirect_uri=redirect_uri)
        profile = svc.get_profile(tokens)
    except ValueError as exc:
        raise merrors.PermissionDeniedException(str(exc), 401, 401)

    if not profile.get("uid"):
        raise merrors.PermissionDeniedException("Could not retrieve user identity from provider")

    user, conn, created = _find_or_create_user(provider, profile)

    if not user.is_active:
        raise merrors.PermissionDeniedException("Account is disabled")

    # Store fresh tokens on the connection
    conn.set_secret("access_token", tokens.get("access_token"))
    if tokens.get("refresh_token"):
        conn.set_secret("refresh_token", tokens.get("refresh_token"))
    conn.save()

    logit.info("oauth", f"User {user.username} logged in via {provider}")
    extra = {"is_new_user": True} if created else None
    return jwt_login(request, user, extra=extra)


# -----------------------------------------------------------------
# OAuth Connection Management
# -----------------------------------------------------------------

@md.URL("account/oauth_connection")
@md.URL("account/oauth_connection/<int:pk>")
@md.requires_auth()
def on_oauth_connection(request, pk=None):
    """Standard CRUD for OAuth connections (GET list, GET detail, POST update).
    DELETE is handled by the custom endpoint below with lockout protection."""
    if request.method == "DELETE" and pk is not None:
        return on_oauth_connection_delete(request, pk)
    return OAuthConnection.on_rest_request(request, pk)


def on_oauth_connection_delete(request, pk):
    """Unlink an OAuth connection with lockout protection."""
    # Admin bypass — manage_users can delete any connection
    if request.user.has_permission("manage_users"):
        conn = OAuthConnection.objects.filter(pk=pk).first()
        if not conn:
            raise merrors.PermissionDeniedException("Not found", 404, 404)
        conn.delete()
        return JsonResponse({"status": True})

    # Owner path — must be own connection
    conn = OAuthConnection.objects.filter(pk=pk, user=request.user).first()
    if not conn:
        raise merrors.PermissionDeniedException("Not found", 404, 404)

    # Lockout guard: if no usable password and this is the last active connection, block
    try:
        has_password = request.user.has_usable_password()
        active_count = OAuthConnection.objects.filter(user=request.user, is_active=True).count()
        if not has_password and active_count <= 1:
            raise merrors.ValueException("Cannot unlink your only login method. Set a password first.")
    except merrors.ValueException:
        raise
    except Exception:
        # Fail-closed: if guard check itself errors, deny the delete
        request.user.report_incident("OAuth unlink guard check failed", "oauth:unlink_guard_error")
        raise merrors.PermissionDeniedException("Unable to process request")

    conn.delete()
    return JsonResponse({"status": True})
