"""
Google OAuth2 provider.

Required settings:
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET

Optional:
    GOOGLE_SCOPES  (default: openid email profile)
"""
import requests

from mojo.helpers import logit
from mojo.helpers.settings import settings

from .base import OAuthProvider

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


class GoogleOAuthProvider(OAuthProvider):

    name = "google"

    def get_auth_url(self, state, redirect_uri):
        client_id = settings.get("GOOGLE_CLIENT_ID")
        scopes = settings.get("GOOGLE_SCOPES", "openid email profile")
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scopes,
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
        query = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
        return f"{GOOGLE_AUTH_URL}?{query}"

    def exchange_code(self, code, redirect_uri):
        resp = requests.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": settings.get("GOOGLE_CLIENT_ID"),
            "client_secret": settings.get("GOOGLE_CLIENT_SECRET"),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }, timeout=10)

        if not resp.ok:
            logit.error("oauth.google", f"Token exchange failed: {resp.status_code} {resp.text}")
            raise ValueError("Failed to exchange authorization code with Google")

        return resp.json()

    def get_profile(self, tokens):
        access_token = tokens.get("access_token")
        resp = requests.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )

        if not resp.ok:
            logit.error("oauth.google", f"Profile fetch failed: {resp.status_code} {resp.text}")
            raise ValueError("Failed to fetch profile from Google")

        data = resp.json()
        return {
            "uid": data.get("sub"),
            "email": (data.get("email") or "").lower().strip(),
            "display_name": data.get("name") or data.get("given_name"),
        }
