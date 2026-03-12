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

    # 2. Existing user by email
    user = User.lookup(email=email)

    # 3. Create new user
    if not user:
        user = User(email=email)
        user.username = user.generate_username_from_email()
        if display_name:
            user.display_name = display_name
        user.is_email_verified = True
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
