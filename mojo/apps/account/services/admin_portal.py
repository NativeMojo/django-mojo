"""Short-lived, server-validated source session for the built-in Admin UI."""

import hashlib
import hmac
import re
import secrets
import time

from django.core.cache import cache

from mojo.apps.account.models import User
from mojo.apps.account.utils.jwtoken import JWToken
from mojo.helpers.settings import settings


_PATH_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
ADMIN_PATH = str(settings.get_static("MOJO_ADMIN_PATH", "admin")).strip("/")
if not _PATH_RE.fullmatch(ADMIN_PATH):
    raise RuntimeError("MOJO_ADMIN_PATH must be one URL-safe path segment")

COOKIE_NAME = str(settings.get_static("MOJO_ADMIN_COOKIE_NAME", "mojo_admin"))
SESSION_TTL = int(settings.get_static("MOJO_ADMIN_SESSION_TTL", 900))
CACHE_PREFIX = "mojo:admin:source-session:"


def _cache_key(session_id):
    return f"{CACHE_PREFIX}{session_id}"


def _auth_key_fingerprint(user):
    return hashlib.sha256(user.get_auth_key().encode("utf-8")).hexdigest()


def _delete(session_id):
    if not session_id:
        return
    try:
        cache.delete(_cache_key(session_id))
    except Exception:
        # Validation fails closed when Redis is unavailable; revocation remains
        # best-effort because the path-scoped browser cookie is also deleted.
        pass


def issue(request):
    """Issue a source session derived from a validated interactive JWT."""
    grant = issue_with_metadata(request)
    return grant["session_id"] if grant else None


def issue_with_metadata(request, *, cache_backend=cache, clock=time.time):
    """One integer deadline bounds the cache, cookie and public metadata."""
    if getattr(request, "bearer", None) != "bearer":
        return None
    raw_token = getattr(getattr(request, "auth_token", None), "token", None)
    if not raw_token:
        return None
    try:
        payload = JWToken().decode(raw_token, validate=False)
    except Exception:
        return None
    if payload.get("token_type") != "access":
        return None
    try:
        expires_at = int(payload.get("exp"))
    except (TypeError, ValueError):
        return None
    issued_at = int(clock())
    deadline = min(issued_at + SESSION_TTL, expires_at)
    ttl = deadline - issued_at
    if ttl <= 0:
        return None

    session_id = secrets.token_urlsafe(32)
    value = {
        "user_id": request.user.pk,
        "auth_key": _auth_key_fingerprint(request.user),
        "token_exp": expires_at,
        "source_session_expires_at": deadline,
    }
    try:
        cache_backend.set(_cache_key(session_id), value, timeout=ttl)
    except Exception:
        return None
    return {"session_id": session_id, "source_session_expires_in": ttl,
            "source_session_expires_at": deadline}


def validate(request):
    """Resolve an active user for the source session, or fail closed."""
    session_id = request.COOKIES.get(COOKIE_NAME, "")
    if not session_id:
        return None
    try:
        value = cache.get(_cache_key(session_id))
    except Exception:
        return None
    now = int(time.time())
    if (not isinstance(value, dict)
            or type(value.get("token_exp")) is not int
            or type(value.get("source_session_expires_at")) is not int
            or min(value["token_exp"], value["source_session_expires_at"]) <= now):
        _delete(session_id)
        return None
    user = User.objects.filter(pk=value.get("user_id"), is_active=True).first()
    if user is None or not hmac.compare_digest(
            str(value.get("auth_key", "")), _auth_key_fingerprint(user)):
        _delete(session_id)
        return None
    return user


def revoke(request):
    _delete(request.COOKIES.get(COOKIE_NAME, ""))


def set_cookie(response, session_id, *, expires_at=None):
    if expires_at is None:
        try:
            value = cache.get(_cache_key(session_id)) or {}
            expires_at = value.get("source_session_expires_at", 0)
        except Exception:
            expires_at = 0
    ttl = max(0, int(expires_at) - int(time.time()))
    response.set_cookie(
        COOKIE_NAME,
        session_id,
        max_age=ttl,
        path=f"/{ADMIN_PATH}",
        secure=bool(settings.get_static("MOJO_ADMIN_COOKIE_SECURE", not settings.DEBUG)),
        httponly=True,
        samesite="Strict",
    )


def delete_cookie(response):
    response.delete_cookie(
        COOKIE_NAME,
        path=f"/{ADMIN_PATH}",
        samesite="Strict",
    )
