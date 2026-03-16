"""
MojoAuthMixin — adds JWT authentication to any MojoModel subclass.

Usage:
    class Player(MojoSecrets, MojoAuthMixin, MojoModel):
        username = models.TextField(unique=True)
        email = models.EmailField(unique=True)
        uuid = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
        ...

Requires the model to have:
    - uuid (UUIDField)
    - is_active (BooleanField)

Provides:
    - get_auth_key()     — lazily generates and persists a signing key
    - validate_jwt()     — classmethod, compatible with AUTH_BEARER_HANDLERS
    - generate_jwt()     — issue a token package for this instance
"""
import uuid as _uuid

from mojo.apps.account.utils.jwtoken import JWToken
from mojo.helpers.settings import settings

JWT_TOKEN_EXPIRY = settings.get("JWT_TOKEN_EXPIRY", 21600)
JWT_REFRESH_TOKEN_EXPIRY = settings.get("JWT_REFRESH_TOKEN_EXPIRY", 604800)


class MojoAuthMixin:
    """
    Mixin that gives any MojoModel JWT authentication capability.

    The model must have: uuid, is_active, and mojo_secrets (via MojoSecrets).
    auth_key is stored in mojo_secrets so no migration is needed.
    """

    def get_auth_key(self):
        """Return the per-instance JWT signing key, generating one if needed."""
        key = self.get_secret("auth_key")
        if not key:
            key = _uuid.uuid4().hex
            self.set_secret("auth_key", key)
            self.atomic_save()
        return key

    def generate_jwt(self, request=None, extra=None):
        """
        Issue a JWT token package for this instance.

        Returns an objict with access_token, refresh_token, expires_in.
        extra: optional dict of additional JWT payload claims.
        """
        keys = {"uid": self.pk}
        if request is not None:
            keys["ip"] = getattr(request, "ip", None)
            device = getattr(request, "device", None)
            if device:
                keys["device"] = device.id
        if extra:
            keys.update(extra)
        token_package = JWToken(
            self.get_auth_key(),
            access_token_expiry=JWT_TOKEN_EXPIRY,
            refresh_token_expiry=JWT_REFRESH_TOKEN_EXPIRY,
        ).create(**keys)
        return token_package

    @classmethod
    def validate_jwt(cls, token, request=None):
        """
        Validate a JWT and return (instance, error_string).

        Compatible with AUTH_BEARER_HANDLERS:
            AUTH_BEARER_HANDLERS = {"player": "game.models.Player.validate_jwt"}

        Returns (instance, None) on success.
        Returns (None, error_string) on failure.

        Handles two token types:
          - "api_key": per-key signing secret stored in UserAPIKey record;
                       revocable individually without affecting the user session.
          - "access" (default): signed with the user's auth_key.
        """
        from mojo.helpers import dates

        token_manager = JWToken()
        jwt_data = token_manager.decode(token, validate=False)
        if not jwt_data or jwt_data.get("uid") is None:
            return None, "Invalid token data"

        instance = cls.objects.filter(pk=jwt_data.uid, is_active=True).first()
        if instance is None:
            return None, "Invalid token: account not found"

        token_manager.key = instance.get_auth_key()
        if not token_manager.is_token_valid(token):
            if token_manager.is_expired:
                return instance, "Token expired"
            return instance, "Token has invalid signature"

        if isinstance(jwt_data.get("allowed_ips"), list):
            if request and request.ip not in jwt_data.allowed_ips:
                return instance, "Not allowed from this location"

        return instance, None
