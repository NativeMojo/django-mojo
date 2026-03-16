"""
Helpers for resolving the frontend webapp base URL and auth path.

Multi-tenant deployments have multiple frontends. HTTP_ORIGIN reflects the
admin portal making the request, not necessarily the tenant's webapp. Use
the lookup chain below to find the correct base URL for token links.
"""
from mojo.helpers.settings import settings


def get_webapp_base_url(request=None, user=None, group=None):
    """
    Resolve the frontend webapp base URL.

    Lookup order (first non-empty value wins):
    1. request.DATA["webapp_base_url"]            — explicit per-request override
    2. group.get_metadata_value(...)              — tenant group config (traverses parents)
    3. user.org.get_metadata_value(...)           — user's primary org
    4. settings.WEBAPP_BASE_URL                  — project-wide default
    5. user.metadata["protected"]["orig_webapp_url"] — recorded at first login
    6. request HTTP_ORIGIN                        — last-resort request context
    7. settings.BASE_URL                          — final fallback
    """
    if request is not None:
        val = request.DATA.get("webapp_base_url")
        if val:
            return val.rstrip("/")
    if group is not None:
        val = group.get_metadata_value("webapp_base_url")
        if val:
            return val.rstrip("/")
    if user is not None:
        org = getattr(user, "org", None)
        if org is not None:
            val = org.get_metadata_value("webapp_base_url")
            if val:
                return val.rstrip("/")
    val = settings.get("WEBAPP_BASE_URL") or ""
    if val:
        return val.rstrip("/")
    if user is not None:
        val = user.get_protected_metadata("orig_webapp_url") or ""
        if val:
            return val.rstrip("/")
    if request is not None:
        val = request.META.get("HTTP_ORIGIN") or ""
        if val:
            return val.rstrip("/")
    return settings.get("BASE_URL", "/").rstrip("/")


def get_webapp_auth_path(group=None):
    """
    Resolve the frontend auth path (e.g. "/auth" or "/login").

    Lookup order:
    1. group.get_metadata_value("webapp_auth_path")  — per-tenant override
    2. settings.WEBAPP_AUTH_PATH                     — project-wide default
    3. "/auth"                                       — built-in default
    """
    if group is not None:
        val = group.get_metadata_value("webapp_auth_path")
        if val:
            return val.rstrip("/")
    return settings.get("WEBAPP_AUTH_PATH", "/auth")


def build_token_url(flow, token, request=None, user=None, group=None):
    """
    Build a full frontend token URL.

    Returns: {base_url}{auth_path}?flow={flow}&token={token}
    """
    base_url = get_webapp_base_url(request=request, user=user, group=group)
    auth_path = get_webapp_auth_path(group=group)
    return f"{base_url}{auth_path}?flow={flow}&token={token}"
