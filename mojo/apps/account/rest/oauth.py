"""
OAuth REST endpoints.

Flow:
  GET  /api/auth/oauth/<provider>/begin    -> returns authorization URL
  POST /api/auth/oauth/<provider>/complete -> exchange code, auto-link user, issue JWT

Supported providers: google (more can be added to services/oauth/__init__.py)
"""
from mojo import decorators as md
from mojo import errors as merrors
from mojo.apps.account.models import User
from mojo.apps.account.models.oauth import OAuthConnection
from mojo.apps.account.rest.user import jwt_login
from mojo.apps.account.services.oauth import get_provider
from mojo.helpers import logit
from mojo.helpers.response import JsonResponse
from mojo.helpers.settings import settings

OAUTH_REDIRECT_URI = settings.get("OAUTH_REDIRECT_URI", "")


def _get_redirect_uri(request, provider_name):
    """Use configured redirect URI or build one from the request origin."""
    if OAUTH_REDIRECT_URI:
        return OAUTH_REDIRECT_URI
    origin = request.META.get("HTTP_ORIGIN") or request.DATA.get("origin", "")
    return f"{origin}/auth/oauth/{provider_name}/complete"


def _find_or_create_user(provider_name, profile):
    """
    Auto-link logic:
    1. Existing OAuthConnection  -> return that user
    2. Matching email on User    -> create OAuthConnection, return that user
    3. Neither                   -> create User + OAuthConnection
    """
    uid = profile["uid"]
    email = profile["email"]
    display_name = profile.get("display_name")

    # 1. Existing connection
    conn = OAuthConnection.objects.filter(
        provider=provider_name, provider_uid=uid
    ).select_related("user").first()
    if conn:
        return conn.user, conn

    # 2. Existing user by email — OAuth has confirmed ownership of this address
    user = User.lookup(email=email)
    if user and not user.is_email_verified:
        user.is_email_verified = True
        user.save(update_fields=["is_email_verified", "modified"])
        logit.info("oauth", f"Marked email verified for {user.username} via {provider_name} OAuth")

    # 3. Create new user
    if not user:
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
    return user, conn


# -----------------------------------------------------------------
# Begin
# -----------------------------------------------------------------

@md.GET("auth/oauth/<str:provider>/begin")
@md.public_endpoint()
def on_oauth_begin(request, provider):
    """Return the provider's authorization URL."""
    try:
        svc = get_provider(provider)
    except ValueError:
        raise merrors.ValueException(f"Unknown provider: {provider}")

    redirect_uri = _get_redirect_uri(request, provider)
    state = svc.create_state()
    auth_url = svc.get_auth_url(state=state, redirect_uri=redirect_uri)

    return JsonResponse({
        "status": True,
        "data": {
            "auth_url": auth_url,
            "state": state,
        },
    })


# -----------------------------------------------------------------
# Complete
# -----------------------------------------------------------------

@md.POST("auth/oauth/<str:provider>/complete")
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
    redirect_uri = _get_redirect_uri(request, provider)

    try:
        tokens = svc.exchange_code(code=code, redirect_uri=redirect_uri)
        profile = svc.get_profile(tokens)
    except ValueError as exc:
        raise merrors.PermissionDeniedException(str(exc), 401, 401)

    if not profile.get("uid"):
        raise merrors.PermissionDeniedException("Could not retrieve user identity from provider")

    user, conn = _find_or_create_user(provider, profile)

    if not user.is_active:
        raise merrors.PermissionDeniedException("Account is disabled")

    # Store fresh tokens on the connection
    conn.set_secret("access_token", tokens.get("access_token"))
    if tokens.get("refresh_token"):
        conn.set_secret("refresh_token", tokens.get("refresh_token"))
    conn.save()

    logit.info("oauth", f"User {user.username} logged in via {provider}")
    return jwt_login(request, user)


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
