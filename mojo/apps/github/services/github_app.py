"""
GitHub App integration service.

Provides JWT generation, installation token management, and webhook
signature verification for GitHub App installations.

Settings:
    GITHUB_APP_ID            — GitHub App ID (string or int)
    GITHUB_APP_PRIVATE_KEY   — Path to the RSA .pem private key file
    GITHUB_WEBHOOK_SECRET    — Webhook HMAC secret for signature verification
"""
import hashlib
import hmac
import time

import jwt
import requests

from django.utils import timezone

from mojo.helpers import logit
from mojo.helpers.settings import settings

logger = logit.get_logger("github_app", "github.log")

# Refresh tokens this many seconds before they expire
TOKEN_BUFFER_SECONDS = 300

GITHUB_API_BASE = "https://api.github.com"


def _get_app_id():
    """Read GITHUB_APP_ID from settings."""
    return settings.get("GITHUB_APP_ID", None)


def _get_private_key():
    """Read the PEM private key from the file path in GITHUB_APP_PRIVATE_KEY."""
    key_path = settings.get("GITHUB_APP_PRIVATE_KEY", None)
    if not key_path:
        return None
    try:
        with open(key_path, "r") as f:
            return f.read()
    except FileNotFoundError:
        logger.error(f"GitHub App private key not found at {key_path}")
        return None


def is_configured():
    """Check if GitHub App integration is configured."""
    return bool(_get_app_id() and _get_private_key())


def generate_jwt():
    """Generate a JWT for GitHub App API authentication.

    JWTs are valid for 10 minutes max per GitHub's requirements.
    We use 9 minutes with a 60-second clock skew tolerance.

    Raises:
        ValueError: If GITHUB_APP_ID or GITHUB_APP_PRIVATE_KEY is not configured.
    """
    app_id = _get_app_id()
    private_key = _get_private_key()
    if not app_id or not private_key:
        raise ValueError(
            "GitHub App not configured — set GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY"
        )

    now = int(time.time())
    payload = {
        "iat": now - 60,   # 60s clock skew tolerance
        "exp": now + 540,  # 9 min (under 10 min max)
        "iss": str(app_id),
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


def is_token_valid(expires_at, buffer_seconds=TOKEN_BUFFER_SECONDS):
    """Check if a token expiry timestamp is still valid.

    Returns True if expires_at is not None and is more than buffer_seconds
    in the future.
    """
    if expires_at is None:
        return False
    return timezone.now() < expires_at - timezone.timedelta(seconds=buffer_seconds)


def get_install_token(install):
    """Get a valid installation access token, refreshing if expired.

    Checks the cached token on the GitHubInstall instance first. If still
    valid, returns it immediately. Otherwise fetches a new token from
    GitHub, stores it on the instance, and saves.

    Args:
        install: GitHubInstall model instance.

    Returns:
        str: Valid access token.

    Raises:
        ValueError: If token refresh fails or GitHub App is not configured.
    """
    # Return cached token if still valid
    cached_token = install.get_secret("token")
    if cached_token and is_token_valid(install.token_expires_at):
        return cached_token

    # Refresh the token
    app_jwt = generate_jwt()
    resp = requests.post(
        f"{GITHUB_API_BASE}/app/installations/{install.installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )

    if resp.status_code != 201:
        logger.error(f"Failed to get install token: {resp.status_code} {resp.text}")
        raise ValueError(f"GitHub token refresh failed: {resp.status_code}")

    data = resp.json()

    # Store new token on the install instance
    install.set_secret("token", data["token"])
    install.token_expires_at = timezone.datetime.fromisoformat(
        data["expires_at"].replace("Z", "+00:00")
    )
    if "permissions" in data:
        install.permissions = data["permissions"]
    install.save()

    logger.info(f"Refreshed GitHub token for installation {install.installation_id}")
    return data["token"]


def verify_webhook_signature(payload_body, signature_header):
    """Verify that a webhook payload was sent by GitHub.

    Uses HMAC-SHA256 with the GITHUB_WEBHOOK_SECRET setting.

    Args:
        payload_body: Raw request body bytes.
        signature_header: X-Hub-Signature-256 header value.

    Returns:
        bool: True if signature is valid, False otherwise.
    """
    secret = settings.get("GITHUB_WEBHOOK_SECRET", None)
    if not secret:
        logger.warning("GITHUB_WEBHOOK_SECRET not configured, rejecting webhook")
        return False

    if not signature_header:
        return False

    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        payload_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature_header)
