"""Fail-closed REST boundary for the owner-only Assistant setup surface.

Owner-only for BOTH read and write. The coarse readiness every operator needs
(is the Assistant on, is it usable) rides in the Admin bootstrap capabilities;
nothing below a literal superuser has a reason to read a key hint or a
verification outcome.
"""

from mojo import decorators as md
from mojo import errors as merrors
from mojo.apps.account.services import assistant_setup, system_setup


@md.GET("account/admin/assistant")
@md.denies_key_backed_session()
@md.requires_global_perms("manage_settings", "admin")
def on_admin_assistant(request):
    system_setup.require_request_admin(request)
    # `refresh=models` is the operator's explicit "re-read the catalogue"
    # control and the ONLY path that reaches Anthropic for the model list.
    # Every other read serves the shared 24h cache, so drawing this page costs
    # no provider round trip.
    refresh = request.DATA.get("refresh") == "models"
    return assistant_setup.state(refresh=refresh)


@md.POST("account/admin/assistant")
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("manage_settings", "admin")
def on_admin_assistant_mutate(request):
    system_setup.require_request_admin(request)
    system_setup.request_origin(request)
    action = request.DATA.get("action")
    keys = set(request.DATA.keys())
    # An exact key set, never a superset: silently ignoring a field a browser
    # sent is how it ends up believing it saved something this call never wrote.
    if action == "verify":
        if keys - {"action", "api_key", "target"}:
            raise merrors.ValueException(
                "Verify accepts only action, api_key, and target")
        result = assistant_setup.verify(
            request.user, request.DATA.get("api_key"),
            target=request.DATA.get("target"))
        return {"schema_version": assistant_setup.SCHEMA_VERSION,
                "verified": True, "result": result,
                "state": assistant_setup.state()}
    if action == "save":
        if keys - {"action", "enabled", "model", "api_key", "clear_api_key",
                   "handler_api_key", "clear_handler_api_key"}:
            raise merrors.ValueException(
                "Save accepts only action, enabled, model, api_key, clear_api_key, "
                "handler_api_key, and clear_handler_api_key")
        saved = assistant_setup.save(
            request.user,
            enabled=request.DATA.get("enabled") is True,
            model=request.DATA.get("model"),
            api_key=request.DATA.get("api_key"),
            clear_api_key=request.DATA.get("clear_api_key") is True,
            handler_api_key=request.DATA.get("handler_api_key"),
            clear_handler_api_key=request.DATA.get("clear_handler_api_key") is True)
        # Both actions answer with the fresh state, so a second editor holding a
        # stale page sees the truth on its very next call.
        return {"schema_version": assistant_setup.SCHEMA_VERSION,
                "saved": True, "state": saved}
    raise merrors.ValueException("action must be verify or save")
