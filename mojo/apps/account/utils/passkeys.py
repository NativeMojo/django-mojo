"""
Simple FIDO2/WebAuthn passkey implementation.

Key principles (KISS):
- RP ID is derived from the Origin header (hostname only)
- Challenges stored in Redis with 5-minute TTL (auto-cleanup)
- No tenant configuration needed
- User can have multiple passkeys for different portals
"""
import json
import logging
import uuid
from typing import List, Tuple
from urllib.parse import urlparse

from fido2.server import Fido2Server
from fido2.utils import websafe_decode, websafe_encode
from fido2.webauthn import (
    AttestedCredentialData,
    AuthenticationResponse,
    PublicKeyCredentialDescriptor,
    PublicKeyCredentialRpEntity,
    PublicKeyCredentialType,
    PublicKeyCredentialUserEntity,
    RegistrationResponse,
)

from mojo import errors as merrors
from mojo.helpers import dates
from mojo.helpers.redis import get_connection
from mojo.helpers.settings import settings

logger = logging.getLogger(__name__)

CHALLENGE_TTL = 300  # 5 minutes


def get_origin_from_request(request) -> str:
    """Extract origin from request headers."""
    origin = (
        request.META.get("HTTP_ORIGIN")
        or getattr(request, "headers", {}).get("Origin")
        or request.DATA.get("origin")
    )
    if not origin:
        raise merrors.ValueException("Origin header is required for passkey operations")
    return origin.strip()


def origin_to_rp_id(origin: str) -> str:
    """Convert origin to RP ID (just the hostname)."""
    parsed = urlparse(origin)
    host = parsed.netloc or parsed.path
    return host.lower()


def get_rp_name() -> str:
    """Get RP name from settings or use a default."""
    return settings.get("PASSKEYS_RP_NAME", "MOJO")


class PasskeyService:
    """Simple passkey service using Redis for challenge storage."""

    def __init__(self, rp_id: str, origin: str):
        self.rp_id = rp_id
        self.origin = origin
        self.rp_name = get_rp_name()
        self.server = Fido2Server(
            PublicKeyCredentialRpEntity(id=rp_id, name=self.rp_name),
            verify_origin=self._verify_origin,
        )
        self.redis = get_connection()

    def _verify_origin(self, origin: str) -> bool:
        """Verify origin matches expected origin."""
        return origin.lower() == self.origin.lower()

    # -----------------------------------------------------------------
    # Challenge Management (Redis)
    # -----------------------------------------------------------------
    def _save_challenge(self, user_id: str, purpose: str, state: dict, challenge: str) -> str:
        """Store challenge in Redis with TTL."""
        challenge_id = uuid.uuid4().hex
        data = {
            "user_id": user_id,
            "purpose": purpose,
            "state": state,
            "challenge": challenge,
            "rp_id": self.rp_id,
            "origin": self.origin,
        }
        key = f"passkey:challenge:{challenge_id}"
        self.redis.setex(key, CHALLENGE_TTL, json.dumps(data))
        return challenge_id

    def _load_challenge(self, challenge_id: str, purpose: str, user_id: str = None):
        """Load challenge from Redis and validate."""
        key = f"passkey:challenge:{challenge_id}"
        raw = self.redis.get(key)
        if not raw:
            raise ValueError("Invalid or expired challenge")

        data = json.loads(raw)

        # Validate purpose
        if data.get("purpose") != purpose:
            raise ValueError("Challenge purpose mismatch")

        # Validate user if provided (for authenticated flows)
        if user_id and data.get("user_id") != user_id:
            raise ValueError("Challenge does not belong to this user")

        # Validate RP ID matches
        if data.get("rp_id") != self.rp_id:
            raise ValueError("Challenge RP ID mismatch")

        return data

    def _delete_challenge(self, challenge_id: str):
        """Delete challenge from Redis (single-use)."""
        key = f"passkey:challenge:{challenge_id}"
        self.redis.delete(key)

    # -----------------------------------------------------------------
    # Registration
    # -----------------------------------------------------------------
    def register_begin(self, user) -> Tuple[dict, str]:
        """
        Begin passkey registration.
        Returns (publicKey_options, challenge_id).
        """
        user_entity = self._build_user_entity(user)
        credentials = self._load_user_credentials(user)

        options, state = self.server.register_begin(
            user_entity,
            credentials=credentials or None,
        )

        # Normalize to JSON-safe dict
        public_key = self._normalize(dict(options.public_key))

        # Store challenge in Redis
        challenge_id = self._save_challenge(
            user_id=str(user.uuid),
            purpose="register",
            state=self._normalize(state),
            challenge=public_key.get("challenge"),
        )

        return public_key, challenge_id

    def register_complete(self, user, challenge_id: str, credential: dict) -> dict:
        """
        Complete passkey registration.
        Returns credential data to store in Passkey model.
        """
        # Load and validate challenge
        challenge_data = self._load_challenge(
            challenge_id,
            purpose="register",
            user_id=str(user.uuid),
        )

        # Verify registration
        registration = RegistrationResponse.from_dict(credential)
        auth_data = self.server.register_complete(
            challenge_data["state"],
            registration,
        )

        if not auth_data.credential_data:
            raise ValueError("Credential data missing from registration")

        # Delete challenge (single-use)
        self._delete_challenge(challenge_id)

        # Return data for Passkey model
        return {
            "token": websafe_encode(bytes(auth_data.credential_data)),
            "credential_id": websafe_encode(auth_data.credential_data.credential_id),
            "sign_count": auth_data.counter,
            "transports": credential.get("transports") or [],
            "aaguid": str(auth_data.credential_data.aaguid),
        }

    # -----------------------------------------------------------------
    # Authentication
    # -----------------------------------------------------------------
    def authenticate_begin(self, user) -> Tuple[dict, str]:
        """
        Begin passkey authentication.
        Returns (publicKey_options, challenge_id).
        """
        credentials = self._load_user_credentials(user)
        if not credentials:
            raise ValueError("No passkeys registered for this portal")

        options, state = self.server.authenticate_begin(credentials)

        # Normalize to JSON-safe dict
        public_key = self._normalize(dict(options.public_key))

        # Store challenge in Redis
        challenge_id = self._save_challenge(
            user_id=str(user.uuid),
            purpose="authenticate",
            state=self._normalize(state),
            challenge=public_key.get("challenge"),
        )

        return public_key, challenge_id

    def authenticate_complete(self, challenge_id: str, credential: dict, passkey):
        """
        Complete passkey authentication.
        Updates passkey sign_count and last_used.
        """
        # Load challenge (no user_id check since this is pre-auth)
        challenge_data = self._load_challenge(
            challenge_id,
            purpose="authenticate",
        )

        # Load stored credential
        stored = self._decode_passkey(passkey)

        # Verify authentication
        authentication = AuthenticationResponse.from_dict(credential)
        self.server.authenticate_complete(
            challenge_data["state"],
            credentials=[stored],
            response=authentication,
        )

        # Check counter
        auth_data = authentication.response.authenticator_data
        new_sign_count = auth_data.counter

        if passkey.sign_count and new_sign_count <= passkey.sign_count:
            raise ValueError("Passkey counter did not advance; possible cloned credential")

        # Update passkey
        passkey.sign_count = new_sign_count
        passkey.last_used = dates.utcnow()
        passkey.save(update_fields=["sign_count", "last_used", "modified"])

        # Delete challenge (single-use)
        self._delete_challenge(challenge_id)

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------
    def _build_user_entity(self, user) -> PublicKeyCredentialUserEntity:
        """Build user entity for WebAuthn."""
        display_name = (
            getattr(user, "display_name", None)
            or getattr(user, "username", None)
            or getattr(user, "email", None)
        )

        user_id = getattr(user, "uuid", None)
        if not user_id:
            raise ValueError("User must have a UUID")

        user_id_bytes = user_id.bytes if hasattr(user_id, "bytes") else str(user_id).encode("utf-8")

        username = getattr(user, "username", None)
        if not username:
            raise ValueError("User must have a username")

        return PublicKeyCredentialUserEntity(
            id=user_id_bytes,
            name=username,
            display_name=display_name,
        )

    def _load_user_credentials(self, user) -> List[AttestedCredentialData]:
        """Load user's credentials for this RP ID."""
        credentials = []

        # Get user's passkeys for this RP ID only
        passkeys = user.passkeys.filter(is_enabled=True, rp_id=self.rp_id)

        for passkey in passkeys:
            try:
                credentials.append(self._decode_passkey(passkey))
            except Exception:
                logger.warning(f"Failed to decode passkey {passkey.pk}", exc_info=True)

        return credentials

    def _decode_passkey(self, passkey) -> AttestedCredentialData:
        """Decode passkey token to AttestedCredentialData."""
        return AttestedCredentialData(websafe_decode(passkey.token))

    def _normalize(self, value):
        """Normalize enums/bytes to JSON-safe types."""
        from enum import Enum

        if isinstance(value, Enum):
            return value.value
        if isinstance(value, bytes):
            return websafe_encode(value)
        if isinstance(value, dict):
            return {k: self._normalize(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._normalize(item) for item in value]
        return value
