"""
Authorization codes and PKCE.

A code is single-use, short-lived, bound to one client, one redirect URI, one
resource and one PKCE challenge, and stored only as a sha256 hash. Presenting a
code that was already consumed is not an error to shrug at — it means either
the client is broken or somebody else has the code — so it revokes the whole
grant family and reports a security incident.

Only S256 is accepted. `plain` and a missing challenge are refused outright, as
OAuth 2.1 requires.
"""
import base64
import datetime
import hashlib
import hmac
import re
import secrets

from mojo.helpers import dates

from . import resources
from .clients import redirect_uri_matches
from .tokens import TokenError

# RFC 7636 §4.1 — the unreserved character set, 43..128 characters.
PKCE_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")


def _sha256_hex(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def validate_pkce_challenge(method, challenge):
    """Raise ValueError unless this is a well-formed S256 challenge."""
    if method != "S256":
        raise ValueError("code_challenge_method must be S256")
    if not isinstance(challenge, str) or not PKCE_RE.match(challenge):
        raise ValueError("code_challenge is missing or malformed")
    return challenge


def verify_pkce(challenge, verifier):
    """True when `verifier` is the pre-image of `challenge` under S256."""
    if not isinstance(verifier, str) or not PKCE_RE.match(verifier):
        return False
    if not isinstance(challenge, str) or not PKCE_RE.match(challenge):
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return hmac.compare_digest(computed, challenge)


def mint_code(user, client, redirect_uri, code_challenge, scope, resource, auth_time):
    """Create one authorization code. Returns the RAW code — stored hashed."""
    from mojo.apps.account.models.oauth_code import OAuthCode

    now = dates.utcnow()
    raw = secrets.token_urlsafe(32)
    OAuthCode(
        client=client,
        user=user,
        code_hash=_sha256_hex(raw),
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        scope=scope or "",
        resource=resource,
        auth_time=int(auth_time or 0),
        expires=now + datetime.timedelta(seconds=resources.code_ttl()),
    ).save()

    # Opportunistic purge — codes are worthless once long expired, and this
    # keeps the table from needing a cron job of its own.
    try:
        OAuthCode.objects.filter(
            expires__lt=now - datetime.timedelta(days=1)).delete()
    except Exception:
        pass

    try:
        type(client).objects.filter(pk=client.pk).update(last_used=now)
    except Exception:
        pass
    return raw


def consume_code(raw, client, redirect_uri, code_verifier):
    """Burn a code and return its row, or raise TokenError("invalid_grant").

    Every refusal is the same error: the token endpoint must not tell a caller
    which of "unknown", "wrong client", "wrong redirect", "expired" or "bad
    verifier" happened. A failed exchange still burns the code — the code was
    presented, so it is spent either way.
    """
    from mojo.apps.account.models.oauth_code import OAuthCode

    if not isinstance(raw, str) or not raw:
        raise TokenError("invalid_grant", "invalid authorization code")

    row = OAuthCode.objects.filter(
        code_hash=_sha256_hex(raw)).select_related("user", "grant").first()
    if row is None:
        raise TokenError("invalid_grant", "invalid authorization code")

    if row.consumed:
        # Replay. Somebody presented a code that was already spent: kill the
        # family the first exchange produced and tell the incident system.
        from . import tokens
        if row.grant is not None:
            tokens.revoke_grant(row.grant, reason="code_replay")
        try:
            row.user.report_incident(
                f"OAuth authorization code replayed for client {row.client_id}",
                "oauth:code_replay", level=6)
        except Exception:
            pass
        raise TokenError("invalid_grant", "invalid authorization code")

    # Single use, settled by rowcount rather than a read-then-write race.
    burned = OAuthCode.objects.filter(pk=row.pk, consumed=False).update(consumed=True)
    if burned != 1:
        raise TokenError("invalid_grant", "invalid authorization code")

    if client is None or row.client_id != client.pk:
        raise TokenError("invalid_grant", "invalid authorization code")
    if not redirect_uri_matches(row.redirect_uri, redirect_uri):
        raise TokenError("invalid_grant", "invalid authorization code")
    if row.expires <= dates.utcnow():
        raise TokenError("invalid_grant", "invalid authorization code")
    if not verify_pkce(row.code_challenge, code_verifier):
        raise TokenError("invalid_grant", "invalid authorization code")
    return row
