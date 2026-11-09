"""
Base OAuth provider interface.

To add a new provider, subclass OAuthProvider and implement all
abstract methods, then register it in __init__.py PROVIDERS dict.
"""
import json
import uuid

from mojo.helpers.redis import get_connection
from mojo.helpers.settings import settings

_STATE_PREFIX = "oauth:state:"


class OAuthProvider:
    """
    Base class for OAuth2 providers.

    Subclasses must implement:
        - name            (str)
        - get_auth_url()  -> str
        - exchange_code() -> dict with keys: access_token, refresh_token (opt), id_token (opt)
        - get_profile()   -> dict with keys: uid, email, display_name

    A provider that needs per-sign-in secrets (a PKCE verifier, an OIDC nonce)
    overrides begin_state(). Its result is stored in the server-side state
    record, and only then are get_auth_url, exchange_code and get_profile
    called with an extra ``state_data`` argument holding that record.

    A profile may carry ``email_verified`` (bool). When present, the email
    links to an existing account only if it is True; see
    rest/oauth.py _find_or_create_user. Without the key, today's rule holds.
    """

    name = None

    # --- State management (Redis) ---

    def create_state(self, extra=None):
        """
        Generate and store a CSRF state token in Redis.
        Returns the state string.
        """
        state = uuid.uuid4().hex
        data = json.dumps(extra or {})
        oauth_state_ttl = settings.get("OAUTH_STATE_TTL", 600)  # 10 minutes
        get_connection().setex(f"{_STATE_PREFIX}{state}", oauth_state_ttl, data)
        return state

    def peek_state(self, state):
        """
        Read a state token without consuming it.
        Returns the stored extra data dict, or None if invalid/expired.
        """
        raw = get_connection().get(f"{_STATE_PREFIX}{state}")
        if not raw:
            return None
        return json.loads(raw)

    def consume_state(self, state):
        """
        Validate and consume (delete) a state token.
        Returns the stored extra data dict, or None if invalid/expired.
        """
        r = get_connection()
        key = f"{_STATE_PREFIX}{state}"
        raw = r.get(key)
        if not raw:
            return None
        r.delete(key)
        return json.loads(raw)

    # --- Subclass contract ---

    def begin_state(self):
        """Extra values to keep in the state record for this sign-in.

        Never sent to the browser; only the opaque state key is.
        """
        return {}

    def get_auth_url(self, state, redirect_uri):
        """Return the provider's authorization URL.

        Providers overriding begin_state() also take ``state_data=None``.
        """
        raise NotImplementedError

    def exchange_code(self, code, redirect_uri):
        """
        Exchange an authorization code for tokens.
        Returns a dict: { access_token, refresh_token (opt), id_token (opt) }
        """
        raise NotImplementedError

    def get_profile(self, tokens):
        """
        Fetch the user's profile using the tokens dict from exchange_code().
        Returns a dict: { uid, email, display_name }
        """
        raise NotImplementedError
