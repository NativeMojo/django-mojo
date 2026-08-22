"""
Grants, access tokens, refresh rotation, revocation — and the confinement check.

The access token is a framework JWT signed with the user's own ``auth_key``,
carrying ``token_type="mcp"`` and an ``aud`` naming exactly one registered
resource. ``validate_access`` below is the body of the ``mcp`` branch of
``User.validate_jwt``: it is the single chokepoint every ``Bearer`` request
passes through, so a token minted for the MCP door authenticates there and
nowhere else — no endpoint can forget the check because no endpoint performs
it.

The refresh token is an opaque secret, stored as a hash and rotated atomically
on every use, with an absolute 30-day ceiling that is never slid. A short grace
window forgives a lost response; a reuse outside it is replay and kills the
whole family.
"""
import datetime
import hashlib
import secrets
from urllib.parse import urlsplit

from django.db.models import F

from mojo.helpers import dates, logit
from mojo.apps.account.utils.jwtoken import JWToken

from . import resources
from .discovery import www_authenticate

# The one refusal string every failure returns. Never say WHICH check failed:
# a caller holding a stolen token must not learn whether the account exists,
# the grant is revoked, or the resource is switched off.
INVALID_TOKEN = "Invalid token"
EXPIRED_TOKEN = "Token expired"


class TokenError(Exception):
    """An RFC 6749 error code plus a description safe to return."""

    def __init__(self, code="invalid_grant", description=""):
        self.code = code
        self.description = description or code
        super().__init__(self.description)


class RotationLost(Exception):
    """Another process rotated this refresh token first (concurrent refresh)."""


def _sha256_hex(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


# --- grants ---------------------------------------------------------------

def create_grant(user, client, scopes, resource, auth_time):
    """Create the standing authorization behind a credential pair."""
    from mojo.apps.account.models.oauth_grant import OAuthGrant

    now = dates.utcnow()
    grant = OAuthGrant(
        user=user,
        client=client,
        # Random placeholders on both unique columns from the very first write:
        # a constant would collide on the second row, and a live token must
        # never resolve a grant that has not issued one yet.
        access_jti=secrets.token_hex(16),
        refresh_hash=secrets.token_hex(32),
        refresh_expires=now + datetime.timedelta(days=resources.refresh_ttl_days()),
        scopes=list(scopes or []),
        resource=resource,
        auth_time=int(auth_time or 0),
        is_active=True)
    grant.save()
    try:
        user.log(
            f"OAuth grant {grant.pk} created for "
            f"{client.client_name or client.client_id}",
            "oauth:grant_created")
    except Exception:
        logit.exception("oauth: could not write the grant_created audit line")
    return grant


def mint_access_token(grant, ttl=None):
    """Mint one access token and record its jti on the grant.

    Every mint replaces ``access_jti``, so the previous access token stops
    resolving to a grant the moment a new one is issued — the stateful edge a
    self-contained JWT would otherwise lack. A negative `ttl` is the test seam
    for an already-expired token.
    """
    from mojo.apps.account.models.oauth_grant import OAuthGrant

    seconds = resources.access_ttl() if ttl is None else int(ttl)
    jti = secrets.token_hex(16)
    expires = dates.utcnow() + datetime.timedelta(seconds=seconds)
    OAuthGrant.objects.filter(pk=grant.pk).update(
        access_jti=jti, access_expires=expires)
    grant.access_jti = jti
    grant.access_expires = expires
    manager = JWToken(grant.user.get_auth_key(), access_token_expiry=seconds)
    return manager.create_access_token(
        token_type="mcp",
        uid=grant.user_id,
        aud=grant.resource,
        scope=" ".join(grant.scopes or []),
        auth_time=grant.auth_time,
        jti=jti)


def _token_response(grant, raw_refresh, ttl=None):
    seconds = resources.access_ttl() if ttl is None else int(ttl)
    return {
        "access_token": mint_access_token(grant, ttl=seconds),
        "token_type": "Bearer",
        "expires_in": seconds,
        "refresh_token": raw_refresh,
        "scope": " ".join(grant.scopes or []),
    }


def issue_tokens(grant, expected_refresh_hash=None):
    """Rotate the refresh secret and mint a matching access token.

    The rotation is ONE conditional UPDATE. Two concurrent refreshes therefore
    settle in the database rather than in a read-check-write window: exactly
    one moves the row, and the loser raises RotationLost and is handed a
    working pair through the grace path instead of a dead one.
    """
    from mojo.apps.account.models.oauth_grant import OAuthGrant

    now = dates.utcnow()
    raw_refresh = secrets.token_urlsafe(32)
    new_hash = _sha256_hex(raw_refresh)
    query = OAuthGrant.objects.filter(pk=grant.pk)
    if expected_refresh_hash is not None:
        query = query.filter(refresh_hash=expected_refresh_hash)
    updated = query.update(
        prev_refresh_hash=F("refresh_hash"),
        refresh_hash=new_hash,
        last_refreshed=now)
    if updated != 1:
        raise RotationLost()
    grant.prev_refresh_hash = grant.refresh_hash
    grant.refresh_hash = new_hash
    grant.last_refreshed = now
    return _token_response(grant, raw_refresh)


def _issue_grace_pair(grant):
    """Re-issue inside the lost-response window.

    Deliberately does NOT move ``prev_refresh_hash`` or ``last_refreshed``: the
    window keeps ticking from the original rotation, so a client that retries
    forever cannot walk it forward. The pair that was never received dies
    anyway, because both ``refresh_hash`` and ``access_jti`` are replaced.
    """
    from mojo.apps.account.models.oauth_grant import OAuthGrant

    raw_refresh = secrets.token_urlsafe(32)
    new_hash = _sha256_hex(raw_refresh)
    updated = OAuthGrant.objects.filter(pk=grant.pk).update(refresh_hash=new_hash)
    if updated != 1:
        raise TokenError("invalid_grant", "invalid refresh token")
    grant.refresh_hash = new_hash
    return _token_response(grant, raw_refresh)


def _check_refreshable(grant, client, resource=None, scope=None, registry=None):
    """Every condition under which a grant may still mint credentials."""
    if grant is None or not grant.is_active:
        raise TokenError("invalid_grant", "invalid refresh token")
    if client is None or grant.client_id != client.pk:
        raise TokenError("invalid_grant", "invalid refresh token")
    if not grant.user.is_active or not grant.client.is_active:
        raise TokenError("invalid_grant", "invalid refresh token")
    if grant.refresh_expires <= dates.utcnow():
        raise TokenError("invalid_grant", "invalid refresh token")
    entry = resources.resolve(resources.resource_path(grant.resource), registry)
    if entry is None or not resources.is_enabled(entry, registry):
        # Dormant, not revoked: the row survives so the Admin still sees it,
        # and re-enabling the resource brings the grant back.
        raise TokenError("invalid_grant", "invalid refresh token")
    if resource is not None and resource != grant.resource:
        raise TokenError("invalid_grant", "invalid refresh token")
    if scope is not None and scope != " ".join(grant.scopes or []):
        raise TokenError("invalid_grant", "invalid refresh token")


def _grace_or_replay(grant, client, resource=None, scope=None, registry=None):
    """Inside the window this is a lost response; outside it, it is replay."""
    now = dates.utcnow()
    last = grant.last_refreshed
    if last is not None and (now - last).total_seconds() <= resources.refresh_grace_seconds():
        _check_refreshable(grant, client, resource, scope, registry)
        return _issue_grace_pair(grant)
    revoke_grant(grant, reason="refresh_replay")
    try:
        grant.user.report_incident(
            f"OAuth refresh token replayed for grant {grant.pk}",
            "oauth:refresh_replay", level=6)
    except Exception:
        logit.exception("oauth: could not report the refresh replay incident")
    raise TokenError("invalid_grant", "invalid refresh token")


def refresh_grant(raw_refresh, client, resource=None, scope=None, registry=None):
    """Exchange a refresh token for a fresh pair, or raise TokenError."""
    from mojo.apps.account.models.oauth_grant import OAuthGrant

    if not isinstance(raw_refresh, str) or not raw_refresh:
        raise TokenError("invalid_grant", "invalid refresh token")
    presented = _sha256_hex(raw_refresh)

    grant = OAuthGrant.objects.filter(
        refresh_hash=presented).select_related("user", "client").first()
    if grant is not None:
        _check_refreshable(grant, client, resource, scope, registry)
        try:
            return issue_tokens(grant, expected_refresh_hash=presented)
        except RotationLost:
            grant = OAuthGrant.objects.filter(
                pk=grant.pk).select_related("user", "client").first()
            if grant is None:
                raise TokenError("invalid_grant", "invalid refresh token")
            return _grace_or_replay(grant, client, resource, scope, registry)

    grant = OAuthGrant.objects.filter(
        prev_refresh_hash=presented).select_related("user", "client").first()
    if grant is None:
        raise TokenError("invalid_grant", "invalid refresh token")
    return _grace_or_replay(grant, client, resource, scope, registry)


def revoke_grant(grant, reason="admin", actor=None):
    """Kill a grant and every credential it has issued. Idempotent."""
    from mojo.apps.account.models.oauth_grant import OAuthGrant

    if grant is None:
        return False
    updated = OAuthGrant.objects.filter(pk=grant.pk, is_active=True).update(
        is_active=False,
        revoked_reason=str(reason or "admin")[:32],
        # Fresh random values on every column a live credential resolves
        # through: the outstanding access token's jti and both refresh hashes
        # now match nothing, and uniqueness still holds.
        access_jti=secrets.token_hex(16),
        refresh_hash=secrets.token_hex(32),
        prev_refresh_hash=secrets.token_hex(32))
    if updated != 1:
        return False
    grant.is_active = False
    grant.revoked_reason = str(reason or "admin")[:32]
    by = f" by {getattr(actor, 'username', actor)}" if actor is not None else ""
    try:
        grant.user.log(
            f"OAuth grant {grant.pk} revoked ({reason}){by}", "oauth:grant_revoked")
    except Exception:
        logit.exception("oauth: could not write the grant_revoked audit line")
    return True


def revoke_token(raw, client):
    """RFC 7009 revocation. Always returns None — the endpoint answers 200."""
    from mojo.apps.account.models.oauth_grant import OAuthGrant

    if not isinstance(raw, str) or not raw or client is None:
        return None
    presented = _sha256_hex(raw)
    grant = OAuthGrant.objects.filter(
        refresh_hash=presented).select_related("user").first()
    if grant is None:
        grant = OAuthGrant.objects.filter(
            prev_refresh_hash=presented).select_related("user").first()
    if grant is None:
        # Maybe it is an access token: read its jti WITHOUT verifying anything.
        # The grant lookup is the authority, and the client check below is what
        # stops one client revoking another's credential.
        try:
            payload = JWToken().decode(raw, validate=False)
            jti = payload.get("jti")
        except Exception:
            jti = None
        if jti:
            grant = OAuthGrant.objects.filter(
                access_jti=jti).select_related("user").first()
    if grant is None or grant.client_id != client.pk:
        return None
    revoke_grant(grant, reason="client")
    return None


# --- Admin-facing API (the #2615 surface) ---------------------------------

def _iso(value):
    return value.isoformat() if value is not None else None


def list_grants(user=None, include_inactive=False):
    """Every grant, newest first, as plain dicts for the Admin surface."""
    from mojo.apps.account.models.oauth_grant import OAuthGrant

    now = dates.utcnow()
    query = OAuthGrant.objects.all().select_related("user", "client")
    if user is not None:
        query = query.filter(user=user)
    if not include_inactive:
        query = query.filter(is_active=True, refresh_expires__gt=now)
    rows = []
    for grant in query.order_by("-created"):
        rows.append({
            "id": grant.pk,
            "client": {
                "id": grant.client_id,
                "client_id": grant.client.client_id,
                "name": grant.client.client_name,
            },
            "user": {
                "id": grant.user_id,
                "email": grant.user.email,
                "display_name": grant.user.display_name,
            },
            "resource": grant.resource,
            "scopes": list(grant.scopes or []),
            "created": _iso(grant.created),
            "last_used": _iso(grant.last_used),
            "expires": _iso(grant.refresh_expires),
            "is_active": bool(grant.is_active and grant.refresh_expires > now),
            "revoked_reason": grant.revoked_reason,
        })
    return rows


def revoke_grant_by_id(grant_id, actor=None):
    """True when a live grant was revoked; False when absent or already dead."""
    from mojo.apps.account.models.oauth_grant import OAuthGrant

    grant = OAuthGrant.objects.filter(
        pk=grant_id, is_active=True).select_related("user").first()
    if grant is None:
        return False
    return revoke_grant(grant, reason="admin", actor=actor)


def revoke_all_grants(actor=None, user=None):
    """Revoke every live grant (optionally for one user). Returns the count."""
    from mojo.apps.account.models.oauth_grant import OAuthGrant

    query = OAuthGrant.objects.filter(is_active=True).select_related("user")
    if user is not None:
        query = query.filter(user=user)
    count = 0
    for grant in query:
        if revoke_grant(grant, reason="admin", actor=actor):
            count += 1
    return count


# --- confinement ----------------------------------------------------------

def validate_access(token, jwt_data, request, registry=None):
    """The `mcp` branch of `User.validate_jwt`. Returns (user, None) or (None, error).

    The rule, in order — and every refusal returns the SAME generic string
    except expiry, which the existing branches also disclose:

      1. no request at all -> refuse. The refresh endpoint and the realtime
         consumer both call validate_jwt without one, so this is what stops an
         mcp token becoming a session pair or opening a WebSocket.
      2. `aud` must be a single string, and its PATH must equal the request
         path exactly. A list `aud` is refused: PyJWT would otherwise match by
         membership, which is not confinement.
      3. that path must be a registered resource whose switch is on. From here
         on a refusal also stamps the RFC 9728 challenge, because the caller is
         at a live resource door and is entitled to be told where to
         authenticate.
      4. the grant must exist, be active, name the same resource, and belong to
         an active user and an active client.
      5. signature, expiry and audience are verified together with the USER's
         auth_key — so a disable, a closure or a `revoke_sessions` kills every
         live mcp token on the next request.
      6. stamp `request.oauth_grant` for the resource server to read.

    Scope is deliberately NOT checked here: the resource server reads
    `request.oauth_grant.scopes` so it can answer 403 `insufficient_scope`
    rather than a blanket 401.
    """
    from mojo.apps.account.models.oauth_grant import OAuthGrant

    if request is None:
        return None, INVALID_TOKEN

    aud = jwt_data.get("aud")
    if not isinstance(aud, str) or not aud:
        return None, INVALID_TOKEN
    try:
        path = urlsplit(aud).path
    except ValueError:
        return None, INVALID_TOKEN
    if not path or getattr(request, "path", None) != path:
        return None, INVALID_TOKEN

    entry = resources.resolve(path, registry)
    if entry is None or not resources.is_enabled(entry, registry):
        return None, INVALID_TOKEN

    # Past this point the caller IS at a live resource door, so every refusal
    # carries the challenge that tells a spec client where to go.
    def _refuse(error=INVALID_TOKEN):
        try:
            request.www_authenticate = www_authenticate(path, error="invalid_token")
        except Exception:
            pass
        return None, error

    jti = jwt_data.get("jti")
    if not jti:
        return _refuse()
    grant = OAuthGrant.objects.filter(
        access_jti=jti, is_active=True).select_related("user", "client").first()
    if grant is None or grant.resource != aud:
        return _refuse()
    if not grant.user.is_active or not grant.client.is_active:
        return _refuse()

    manager = JWToken(grant.user.auth_key)
    if not manager.is_token_valid(token, audience=aud):
        if manager.is_expired:
            return _refuse(EXPIRED_TOKEN)
        return _refuse()

    request.oauth_grant = grant
    try:
        OAuthGrant.objects.filter(pk=grant.pk).update(last_used=dates.utcnow())
    except Exception:
        pass
    return grant.user, None
