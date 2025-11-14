"""
Simple passkey REST endpoints.

Registration flow:
  1. POST /api/account/passkeys/register/begin → get challenge
  2. Browser calls navigator.credentials.create()
  3. POST /api/account/passkeys/register/complete → save passkey

Login flow:
  1. POST /api/auth/passkeys/login/begin → get challenge
  2. Browser calls navigator.credentials.get()
  3. POST /api/auth/passkeys/login/complete → get JWT tokens

Management:
  - GET /api/account/passkeys → list user's passkeys
  - POST /api/account/passkeys/<id> → update friendly_name, is_enabled
  - DELETE /api/account/passkeys/<id> → remove passkey
"""
import datetime

from django.db import transaction
from django.db.models import Q

from mojo import decorators as md
from mojo import errors as merrors
from mojo.apps.account.models import Passkey, User
from mojo.apps.account.rest.user import jwt_login
from mojo.apps.account.utils.passkeys import (
    PasskeyService,
    get_origin_from_request,
    origin_to_rp_id,
    CHALLENGE_TTL,
)
from mojo.helpers import dates
from mojo.helpers.response import JsonResponse

# -----------------------------------------------------------------
# Passkey Management
# -----------------------------------------------------------------

@md.URL("account/passkeys")
@md.URL("account/passkeys/<int:pk>")
@md.requires_auth()
def on_account_passkey(request, pk=None):
    """Standard REST endpoint for managing passkeys."""
    if request.method == "POST" and pk is None:
        return Passkey.rest_error_response(
            request,
            405,
            error="Use the register endpoints to create a passkey.",
        )
    return Passkey.on_rest_request(request, pk)


# -----------------------------------------------------------------
# Registration (Authenticated)
# -----------------------------------------------------------------

@md.POST("account/passkeys/register/begin")
@md.requires_auth()
def on_passkeys_register_begin(request):
    """Begin passkey registration for authenticated user."""
    origin = get_origin_from_request(request)
    rp_id = origin_to_rp_id(origin)

    service = PasskeyService(rp_id=rp_id, origin=origin)
    public_key, challenge_id = service.register_begin(request.user)

    expires_at = dates.utcnow() + datetime.timedelta(seconds=CHALLENGE_TTL)

    return JsonResponse({
        "status": True,
        "data": {
            "challenge_id": challenge_id,
            "publicKey": public_key,
            "expiresAt": expires_at.isoformat(),
        },
    })


@md.POST("account/passkeys/register/complete")
@md.requires_auth()
@md.requires_params("challenge_id", "credential")
def on_passkeys_register_complete(request):
    """Complete passkey registration."""
    challenge_id = request.DATA.get("challenge_id")
    credential = dict(request.DATA.get("credential"))
    friendly_name = request.DATA.get("friendly_name")

    origin = get_origin_from_request(request)
    rp_id = origin_to_rp_id(origin)

    service = PasskeyService(rp_id=rp_id, origin=origin)

    try:
        result = service.register_complete(
            user=request.user,
            challenge_id=challenge_id,
            credential=credential,
        )
    except ValueError as exc:
        raise merrors.PermissionDeniedException(str(exc))

    # Save or update passkey
    with transaction.atomic():
        passkey, created = Passkey.objects.get_or_create(
            credential_id=result["credential_id"],
            defaults={
                "user": request.user,
                "token": result["token"],
                "rp_id": rp_id,
                "sign_count": result["sign_count"],
                "transports": result.get("transports", []),
                "aaguid": result.get("aaguid"),
                "friendly_name": friendly_name,
            },
        )

        if not created:
            # Re-registering same credential (allowed for same user)
            if passkey.user != request.user:
                raise merrors.PermissionDeniedException(
                    "Credential already registered to another user"
                )

            # Update existing passkey
            passkey.token = result["token"]
            passkey.rp_id = rp_id
            passkey.sign_count = result["sign_count"]
            passkey.transports = result.get("transports", [])
            passkey.aaguid = result.get("aaguid")
            if friendly_name:
                passkey.friendly_name = friendly_name
            passkey.is_enabled = True
            passkey.last_used = None
            passkey.save(
                update_fields=[
                    "token", "rp_id", "sign_count", "transports",
                    "aaguid", "friendly_name", "is_enabled",
                    "last_used", "modified",
                ]
            )

    return passkey.on_rest_get(request)


# -----------------------------------------------------------------
# Authentication (Public)
# -----------------------------------------------------------------

@md.POST("auth/passkeys/login/begin")
@md.requires_params("username")
@md.public_endpoint()
def on_passkeys_login_begin(request):
    """Begin passkey authentication (passwordless login)."""
    username = request.DATA.get("username", "").lower().strip()
    if not username:
        raise merrors.ValueException("Username is required")

    # Find user
    user = User.objects.filter(Q(username=username) | Q(email=username)).first()
    if not user:
        User.class_report_incident(
            f"Passkey login attempt with unknown username: {username}",
            event_type="login:unknown",
            level=8,
            request=request,
        )
        raise merrors.PermissionDeniedException("Invalid username or no passkeys registered")

    origin = get_origin_from_request(request)
    rp_id = origin_to_rp_id(origin)

    # Check if user has passkeys for this portal
    if not user.passkeys.filter(is_enabled=True, rp_id=rp_id).exists():
        raise merrors.PermissionDeniedException("No passkeys registered for this portal")

    service = PasskeyService(rp_id=rp_id, origin=origin)

    try:
        public_key, challenge_id = service.authenticate_begin(user)
    except ValueError as exc:
        raise merrors.PermissionDeniedException(str(exc))

    expires_at = dates.utcnow() + datetime.timedelta(seconds=CHALLENGE_TTL)

    return JsonResponse({
        "status": True,
        "data": {
            "challenge_id": challenge_id,
            "publicKey": public_key,
            "expiresAt": expires_at.isoformat(),
        },
    })


@md.POST("auth/passkeys/login/complete")
@md.requires_params("challenge_id", "credential")
@md.public_endpoint()
def on_passkeys_login_complete(request):
    """Complete passkey authentication and issue JWT tokens."""
    challenge_id = request.DATA.get("challenge_id")
    credential = dict(request.DATA.get("credential"))

    origin = get_origin_from_request(request)
    rp_id = origin_to_rp_id(origin)

    # Find passkey by credential_id
    credential_id = credential.get("id") or credential.get("rawId")
    if not credential_id:
        raise merrors.ValueException("Credential ID missing")

    try:
        passkey = Passkey.objects.select_related("user").get(
            credential_id=credential_id,
            rp_id=rp_id,
            is_enabled=True,
        )
    except Passkey.DoesNotExist:
        raise merrors.PermissionDeniedException("Invalid passkey")

    service = PasskeyService(rp_id=rp_id, origin=origin)

    try:
        service.authenticate_complete(
            challenge_id=challenge_id,
            credential=credential,
            passkey=passkey,
        )
    except ValueError as exc:
        raise merrors.PermissionDeniedException(str(exc))

    # Issue JWT tokens
    return jwt_login(request, passkey.user)
