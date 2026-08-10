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
    if getattr(request, "bearer", None) != "bearer":
        return None
    raw_token = getattr(getattr(request, "auth_token", None), "token", None)
    if not raw_token:
        return None
    payload = JWToken().decode(raw_token, validate=False)
    if payload.get("token_type") != "access":
        return None
    try:
        expires_at = int(payload.get("exp"))
    except (TypeError, ValueError):
        return None
    ttl = min(SESSION_TTL, expires_at - int(time.time()))
    if ttl <= 0:
        return None

    session_id = secrets.token_urlsafe(32)
    value = {
        "user_id": request.user.pk,
        "auth_key": _auth_key_fingerprint(request.user),
        "token_exp": expires_at,
    }
    try:
        cache.set(_cache_key(session_id), value, timeout=ttl)
    except Exception:
        return None
    return session_id


def validate(request):
    """Resolve an active user for the source session, or fail closed."""
    session_id = request.COOKIES.get(COOKIE_NAME, "")
    if not session_id:
        return None
    try:
        value = cache.get(_cache_key(session_id))
    except Exception:
        return None
    if not isinstance(value, dict) or int(value.get("token_exp", 0)) <= int(time.time()):
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


def set_cookie(response, session_id):
    response.set_cookie(
        COOKIE_NAME,
        session_id,
        max_age=SESSION_TTL,
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
