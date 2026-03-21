"""
Apple Sign In OAuth provider.

Required settings:
    APPLE_CLIENT_ID    Service ID registered in Apple Developer portal (e.g. com.example.web)
    APPLE_TEAM_ID      10-character Apple Developer Team ID
    APPLE_KEY_ID       Key ID from the .p8 file
    APPLE_PRIVATE_KEY  Full PEM string of the .p8 private key

Flow is identical to Google:
    GET  /api/auth/oauth/apple/begin    -> authorization URL
    POST /api/auth/oauth/apple/complete -> exchange code, issue JWT
"""
import time

import jwt
import requests

from mojo.helpers import logit
from mojo.helpers.settings import settings

from .base import OAuthProvider

APPLE_AUTH_URL  = "https://appleid.apple.com/auth/authorize"
APPLE_TOKEN_URL = "https://appleid.apple.com/auth/token"
APPLE_AUDIENCE  = "https://appleid.apple.com"


class AppleOAuthProvider(OAuthProvider):

    name = "apple"

    def _build_client_secret(self):
        """
        Generate a short-lived ES256 JWT to use as the client_secret.
        Apple requires this instead of a static secret.
        """
        team_id    = settings.get("APPLE_TEAM_ID")
        client_id  = settings.get("APPLE_CLIENT_ID")
        key_id     = settings.get("APPLE_KEY_ID")
        private_key = settings.get("APPLE_PRIVATE_KEY")

        if not all([team_id, client_id, key_id, private_key]):
            raise ValueError(
                "Apple OAuth requires APPLE_TEAM_ID, APPLE_CLIENT_ID, "
                "APPLE_KEY_ID, and APPLE_PRIVATE_KEY to be set"
            )

        now = int(time.time())
        payload = {
            "iss": team_id,
            "iat": now,
            "exp": now + 300,  # 5 minutes — Apple's minimum accepted TTL
            "aud": APPLE_AUDIENCE,
            "sub": client_id,
        }
        return jwt.encode(
            payload,
            private_key,
            algorithm="ES256",
            headers={"kid": key_id},
        )

    def get_auth_url(self, state, redirect_uri):
        client_id = settings.get("APPLE_CLIENT_ID")
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "response_mode": "form_post",
            "scope": "openid email",
            "state": state,
        }
        query = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
        return f"{APPLE_AUTH_URL}?{query}"

    def exchange_code(self, code, redirect_uri):
        resp = requests.post(APPLE_TOKEN_URL, data={
            "client_id": settings.get("APPLE_CLIENT_ID"),
            "client_secret": self._build_client_secret(),
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }, timeout=10)

        if not resp.ok:
            logit.error("oauth.apple", f"Token exchange failed: {resp.status_code} {resp.text}")
            raise ValueError("Failed to exchange authorization code with Apple")

        return resp.json()

    def get_profile(self, tokens):
        id_token = tokens.get("id_token")
        if not id_token:
            raise ValueError("Apple token response missing id_token")

        # Decode without signature verification — received directly from Apple over HTTPS.
        data = jwt.decode(id_token, options={"verify_signature": False})

        uid   = data.get("sub")
        email = (data.get("email") or "").lower().strip()

        if not uid:
            raise ValueError("Apple id_token missing sub claim")
        if not email:
            raise ValueError("Apple id_token missing email claim")

        return {
            "uid": uid,
            "email": email,
            "display_name": None,  # Apple only provides name on first login; we don't rely on it
        }
