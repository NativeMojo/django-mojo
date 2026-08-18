"""Fail-closed REST boundary for the curated Admin Settings catalog."""

from mojo import decorators as md
from mojo import errors as merrors
from mojo.apps.account.services import admin_settings as _admin_settings


def _capabilities(user):
    permissions = user.permissions if isinstance(user.permissions, dict) else {}
    superuser = bool(user.is_superuser)
    catalog_write = superuser or permissions.get("manage_settings") is True or permissions.get("admin") is True
    owner_display = superuser or any(permissions.get(key) is True for key in (
        "view_advanced_settings", "manage_advanced", "admin"))
    owner_edit = superuser and (
        superuser or permissions.get("manage_advanced") is True or permissions.get("admin") is True)
    return {
        "catalog_write": catalog_write,
        "owner_display": owner_display,
        "owner_edit": owner_edit,
    }


@md.GET("account/admin/settings")
@md.denies_key_backed_session()
@md.requires_global_perms(
    "manage_settings", "view_advanced_settings", "manage_advanced", "admin")
def on_admin_settings(request):
    report = _admin_settings.catalog(capabilities=_capabilities(request.user))
    if request.user.is_superuser:
        from mojo.apps.account.services import provider_setup
        report["provider_setup"] = provider_setup.state()
    return report


@md.POST("account/admin/settings")
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("manage_settings", "admin")
def on_admin_settings_mutate(request):
    action = request.DATA.get("action")
    if action in ("configure_providers", "test_providers"):
        if set(request.DATA.keys()) != {"action", "providers"}:
            raise merrors.ValueException(
                "Provider setup accepts only action and providers")
        from mojo.apps.account.services import system_setup
        system_setup.require_request_admin(request)
        system_setup.request_origin(request)
        from mojo.apps.account.services import provider_setup
        if action == "test_providers":
            return provider_setup.test(request.user, request.DATA.get("providers"))
        return provider_setup.apply(request.user, request.DATA.get("providers"))
    key = request.DATA.get("key")
    if not isinstance(key, str):
        raise merrors.ValueException("A catalog setting key is required")
    keys = set(request.DATA.keys())
    if action == "set":
        if keys != {"action", "key", "value"}:
            raise merrors.ValueException("Set accepts only action, key, and value")
        saved = _admin_settings.set_override(request.user, key, request.DATA.get("value"))
        return {"schema_version": 1, "saved": True, "key": key,
                "effective_value": saved}
    if action == "clear":
        if keys != {"action", "key"}:
            raise merrors.ValueException("Clear accepts only action and key")
        removed = _admin_settings.clear_override(request.user, key)
        return {"schema_version": 1, "cleared": True, "key": key,
                "removed": removed}
    raise merrors.ValueException("action must be set or clear")
