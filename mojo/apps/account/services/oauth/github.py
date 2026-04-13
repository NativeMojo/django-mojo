"""
GitHub OAuth2 provider.

Required settings:
    GITHUB_CLIENT_ID
    GITHUB_CLIENT_SECRET

Optional:
    GITHUB_SCOPES  (default: read:user user:email)

GitHub may not return an email on the /user endpoint if the user has set
their email to private. In that case we fall back to GET /user/emails and
pick the primary verified address.
"""
from urllib.parse import urlencode, quote

import requests

from mojo.helpers import logit
from mojo.helpers.settings import settings

from .base import OAuthProvider

GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_EMAILS_URL = "https://api.github.com/user/emails"


class GitHubOAuthProvider(OAuthProvider):

    name = "github"

    def get_auth_url(self, state, redirect_uri):
        client_id = settings.get("GITHUB_CLIENT_ID")
        scopes = settings.get("GITHUB_SCOPES", "read:user user:email")
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": scopes,
        }
        return f"{GITHUB_AUTH_URL}?{urlencode(params, quote_via=quote)}"

    def exchange_code(self, code, redirect_uri):
        resp = requests.post(GITHUB_TOKEN_URL, json={
            "client_id": settings.get("GITHUB_CLIENT_ID"),
            "client_secret": settings.get("GITHUB_CLIENT_SECRET"),
            "code": code,
        }, headers={"Accept": "application/json"}, timeout=10)

        if not resp.ok:
            logit.error("oauth.github", f"Token exchange failed: {resp.status_code}")
            raise ValueError("Failed to exchange authorization code with GitHub")

        return resp.json()

    def get_profile(self, tokens):
        access_token = tokens.get("access_token")
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

        # Fetch user profile
        resp = requests.get(GITHUB_USER_URL, headers=headers, timeout=10)
        if not resp.ok:
            logit.error("oauth.github", f"Profile fetch failed: {resp.status_code}")
            raise ValueError("Failed to fetch profile from GitHub")

        data = resp.json()
        email = (data.get("email") or "").lower().strip()

        # GitHub may not return email if it is set to private.
        # Fall back to the /user/emails endpoint.
        if not email:
            email = self._fetch_primary_email(headers)

        if not email:
            raise ValueError("Could not retrieve verified email from GitHub")

        return {
            "uid": str(data["id"]),
            "email": email,
            "display_name": data.get("name") or data.get("login"),
        }

    def _fetch_primary_email(self, headers):
        """Fetch the primary verified email from GET /user/emails."""
        resp = requests.get(GITHUB_EMAILS_URL, headers=headers, timeout=10)
        if not resp.ok:
            logit.error("oauth.github", f"Email fetch failed: {resp.status_code}")
            return None

        for entry in resp.json():
            if entry.get("primary") and entry.get("verified"):
                return (entry.get("email") or "").lower().strip()

        return None
