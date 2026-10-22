"""
Microsoft identity platform (Entra ID) OAuth2 / OpenID Connect provider.

Signs in personal Microsoft accounts and work or school accounts through the
v2.0 endpoints, with PKCE and a nonce. Identity is the ID token's ``tid:oid``,
taken only from a token whose signature, audience, lifetime, nonce, issuer and
signing-key issuer all check out. There is no Graph or UserInfo call.

Required settings:
    MICROSOFT_CLIENT_ID      Application (client) ID
    MICROSOFT_CLIENT_SECRET  Client secret value

Optional:
    MICROSOFT_TENANT  common (default) | organizations | consumers | <tenant GUID>
    MICROSOFT_SCOPES  (default: openid profile email)

The profile carries ``email_verified``: True only when the token says
``xms_edov`` is true, so ``rest/oauth.py`` links by email only on an email
Microsoft confirmed.
"""
import base64
import hashlib
import hmac
import json
import secrets
import uuid
from urllib.parse import urlencode, quote

import jwt
import requests

from mojo.helpers import logit
from mojo.helpers.redis import get_connection
from mojo.helpers.settings import settings

from .base import OAuthProvider

MICROSOFT_AUTHORITY = "https://login.microsoftonline.com/{tenant}"
MICROSOFT_AUTH_URL = MICROSOFT_AUTHORITY + "/oauth2/v2.0/authorize"
MICROSOFT_TOKEN_URL = MICROSOFT_AUTHORITY + "/oauth2/v2.0/token"
MICROSOFT_JWKS_URL = MICROSOFT_AUTHORITY + "/discovery/v2.0/keys"
MICROSOFT_ISSUER = "https://login.microsoftonline.com/{tid}/v2.0"

# The tenant every personal Microsoft account (MSA) signs in through.
CONSUMERS_TID = "9188040d-6c67-4c5b-b112-36a304b66dad"
TENANT_WORDS = ("common", "organizations", "consumers")

JWKS_CACHE_KEY = "oauth:microsoft:jwks:{tenant}"
JWKS_CACHE_TTL = 24 * 60 * 60
JWKS_REFETCH_KEY = "oauth:microsoft:jwks_refetch:{tenant}"
JWKS_REFETCH_TTL = 5 * 60
CLOCK_LEEWAY = 60

VERIFY_FAILED = "Microsoft sign-in could not be verified"


def normalize_tenant(value):
    """Return the tenant as stored: a word or a lower-case GUID; "" reads as common.

    Raises ValueError for anything else.
    """
    tenant = (value or "").strip().lower() or "common"
    if tenant in TENANT_WORDS:
        return tenant
    try:
        return str(uuid.UUID(tenant))
    except ValueError:
        raise ValueError(
            "Tenant must be common, organizations, consumers or a tenant ID (GUID)")


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class MicrosoftOAuthProvider(OAuthProvider):

    name = "microsoft"

    # --- Config ---

    def _client_id(self):
        return settings.get("MICROSOFT_CLIENT_ID")

    def _tenant(self):
        return normalize_tenant(settings.get("MICROSOFT_TENANT", "common"))

    # --- Flow ---

    def begin_state(self):
        return {
            "code_verifier": secrets.token_urlsafe(64),
            "nonce": secrets.token_urlsafe(32),
        }

    def get_auth_url(self, state, redirect_uri, state_data=None):
        state_data = state_data or {}
        challenge = _b64url(hashlib.sha256(
            state_data["code_verifier"].encode("ascii")).digest())
        params = {
            "client_id": self._client_id(),
            "response_type": "code",
            "response_mode": "query",
            "redirect_uri": redirect_uri,
            "scope": settings.get("MICROSOFT_SCOPES", "openid profile email"),
            "state": state,
            "nonce": state_data["nonce"],
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        url = MICROSOFT_AUTH_URL.format(tenant=self._tenant())
        return f"{url}?{urlencode(params, quote_via=quote)}"

    def exchange_code(self, code, redirect_uri, state_data=None):
        state_data = state_data or {}
        url = MICROSOFT_TOKEN_URL.format(tenant=self._tenant())
        resp = requests.post(url, data={
            "client_id": self._client_id(),
            "client_secret": settings.get("MICROSOFT_CLIENT_SECRET"),
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": state_data.get("code_verifier", ""),
        }, timeout=10)

        if not resp.ok:
            # Never log the body: error_description can echo request details.
            error, codes = None, None
            try:
                body = resp.json()
                error, codes = body.get("error"), body.get("error_codes")
            except ValueError:
                pass
            logit.error("oauth.microsoft",
                        f"Token exchange failed: {resp.status_code} error={error} error_codes={codes}")
            raise ValueError("Failed to exchange authorization code with Microsoft")

        return resp.json()

    def get_profile(self, tokens, state_data=None):
        state_data = state_data or {}
        id_token = (tokens or {}).get("id_token")
        if not id_token:
            logit.warn("oauth.microsoft", "Token response carried no id_token")
            raise ValueError(VERIFY_FAILED)
        claims = self._validate_id_token(id_token, state_data.get("nonce"))
        email = (claims.get("email") or "").lower().strip() or None
        return {
            "uid": f"{claims['tid']}:{claims['oid']}",
            "email": email,
            "email_verified": email is not None and claims.get("xms_edov") is True,
            "display_name": claims.get("name"),
            "given_name": claims.get("given_name"),
            "family_name": claims.get("family_name"),
        }

    # --- ID token validation ---

    def fetch_jwks(self, tenant):
        """GET the tenant's signing keys. Returns the parsed JSON document."""
        resp = requests.get(MICROSOFT_JWKS_URL.format(tenant=tenant), timeout=10)
        if not resp.ok:
            raise ValueError(f"JWKS fetch returned {resp.status_code}")
        return resp.json()

    def _jwks(self, tenant, refresh=False):
        r = get_connection()
        key = JWKS_CACHE_KEY.format(tenant=tenant)
        if not refresh:
            raw = r.get(key)
            if raw:
                return json.loads(raw)
        doc = self.fetch_jwks(tenant)
        if not isinstance(doc, dict) or not isinstance(doc.get("keys"), list):
            raise ValueError("JWKS document has no keys")
        r.setex(key, JWKS_CACHE_TTL, json.dumps(doc))
        return doc

    def _signing_key(self, tenant, kid):
        def find(doc):
            for entry in doc.get("keys") or []:
                if isinstance(entry, dict) and entry.get("kid") == kid:
                    return entry
            return None

        found = find(self._jwks(tenant))
        if found is None:
            # One re-fetch per tenant per 5 minutes, for a rotated key.
            guard = JWKS_REFETCH_KEY.format(tenant=tenant)
            if not get_connection().set(guard, "1", nx=True, ex=JWKS_REFETCH_TTL):
                raise ValueError(f"unknown kid {kid}; JWKS re-fetch throttled")
            found = find(self._jwks(tenant, refresh=True))
        if found is None:
            raise ValueError(f"unknown kid {kid}")
        return found

    def _validate_id_token(self, id_token, nonce, client_id=None, tenant=None):
        """Return the claims of a fully validated ID token, or raise ValueError.

        ``client_id`` and ``tenant`` default to the settings.
        """
        client_id = client_id or self._client_id()
        tenant = normalize_tenant(tenant) if tenant else self._tenant()
        try:
            return self._check_id_token(id_token, nonce, client_id, tenant)
        except ValueError as exc:
            logit.warn("oauth.microsoft", f"ID token rejected: {exc}")
        except jwt.PyJWTError as exc:
            logit.warn("oauth.microsoft", f"ID token rejected: {type(exc).__name__}: {exc}")
        except Exception as exc:
            logit.warn("oauth.microsoft", f"ID token rejected: {type(exc).__name__}")
        raise ValueError(VERIFY_FAILED)

    def _check_id_token(self, id_token, nonce, client_id, tenant):
        if not client_id:
            raise ValueError("MICROSOFT_CLIENT_ID is not set")
        if not nonce:
            raise ValueError("no nonce in the sign-in state")

        # 1. Header: RS256 with a key id, nothing else.
        header = jwt.get_unverified_header(id_token)
        if header.get("alg") != "RS256":
            raise ValueError(f"alg {header.get('alg')!r} is not RS256")
        kid = header.get("kid")
        if not kid:
            raise ValueError("no kid in the token header")

        # 2-3. The signing key, keeping its raw issuer (PyJWK drops it).
        raw_key = self._signing_key(tenant, kid)
        key_issuer = raw_key.get("issuer")
        if not key_issuer:
            raise ValueError("signing key has no issuer")
        key = jwt.PyJWK(raw_key)

        # 4. Signature, audience and lifetime. The issuer is checked by hand.
        claims = jwt.decode(
            id_token, key, algorithms=["RS256"], audience=client_id,
            options={"require": ["exp", "iat", "nbf", "aud", "iss", "tid", "oid"]},
            leeway=CLOCK_LEEWAY)

        # 5. Nonce.
        token_nonce = claims.get("nonce")
        if not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce, nonce):
            raise ValueError("nonce mismatch")

        # 6. tid is a lower-case GUID and the issuer names it.
        tid = claims["tid"]
        try:
            canonical = str(uuid.UUID(tid))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("tid is not a GUID")
        if canonical != tid:
            raise ValueError("tid is not a canonical lower-case GUID")
        if claims["iss"] != MICROSOFT_ISSUER.format(tid=tid):
            raise ValueError("iss does not match tid")

        # 7. The key may only sign for its own issuer.
        if key_issuer.replace("{tenantid}", tid) != claims["iss"]:
            raise ValueError("signing key issuer does not match iss")

        # 8. Tenant policy.
        if tenant == "organizations" and tid == CONSUMERS_TID:
            raise ValueError("personal accounts are not allowed (tenant organizations)")
        if tenant == "consumers" and tid != CONSUMERS_TID:
            raise ValueError("only personal accounts are allowed (tenant consumers)")
        if tenant not in TENANT_WORDS and tid != tenant:
            raise ValueError("token is from another tenant")

        if not claims.get("oid"):
            raise ValueError("no oid")
        return claims
