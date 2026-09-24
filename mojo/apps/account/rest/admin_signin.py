"""Admin Sign-in page: the system login look and feel plus the Google, Apple
and GitHub sign-in providers. Trusted-admin configuration — global
manage_settings or admin (or a superuser)."""

from mojo import decorators as md
from mojo import errors as merrors
from mojo.apps.account.services import signin_setup


@md.GET("account/admin/signin")
@md.denies_key_backed_session()
@md.requires_global_perms("manage_settings", "admin")
def on_admin_signin(request):
    return signin_setup.state(request)


@md.POST("account/admin/signin")
@md.denies_key_backed_session()
@md.requires_global_perms("manage_settings", "admin")
def on_admin_signin_save(request):
    """Body: any of
    {"auth": {"theme.app_title": "...", "login.methods": [...]}}
    {"provider": "apple", "values": {"APPLE_TEAM_ID": "...", "APPLE_PRIVATE_KEY": null},
     "enabled": true}
    Returns the full page state."""
    auth = request.DATA.get("auth")
    provider = request.DATA.get("provider")
    if auth is None and provider is None:
        raise merrors.ValueException("Nothing to save: send auth and/or provider")
    if auth is not None:
        signin_setup.save_auth(request.user, auth)
    if provider is not None:
        signin_setup.save_provider(
            request.user, provider,
            values=request.DATA.get("values"),
            enabled=request.DATA.get("enabled"))
    return signin_setup.state(request)
