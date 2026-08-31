"""Fail-closed REST boundary for the owner-only Assistant setup surface.

Owner-only for BOTH read and write. The coarse readiness every operator needs
(is the Assistant on, is it usable) rides in the Admin bootstrap capabilities;
nothing below a literal superuser has a reason to read a key hint or a
verification outcome.
"""

from mojo import decorators as md
from mojo import errors as merrors
from mojo.apps.account.services import assistant_setup, system_setup


@md.GET("account/admin/llm-safety")
@md.denies_key_backed_session()
@md.requires_global_perms("view_security", "security")
def on_admin_llm_safety(request):
    from mojo.apps.account.services import llm_safety
    return {"schema_version": 1, "safety": llm_safety.aggregate_state(
        hours=request.DATA.get("hours", 24))}


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
    # `check=discovery` is the operator's explicit "is my front door actually
    # forwarding the discovery documents" control, and the ONLY read that makes
    # an outbound request — to this installation's own public address, never a
    # provider. It is rate-limited by a 60 second server-side cache, and
    # drawing the page without it costs no request at all.
    check = request.DATA.get("check") == "discovery"
    return assistant_setup.state(refresh=refresh, check=check)


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
                   "handler_api_key", "clear_handler_api_key", "mcp_enabled",
                   "emergency_stop", "autonomous_triage"}:
            raise merrors.ValueException(
                "Save accepts only action, enabled, model, api_key, clear_api_key, "
                "handler_api_key, clear_handler_api_key, mcp_enabled, "
                "emergency_stop, and autonomous_triage")
        saved = assistant_setup.save(
            request.user,
            enabled=request.DATA.get("enabled") is True,
            model=request.DATA.get("model"),
            api_key=request.DATA.get("api_key"),
            clear_api_key=request.DATA.get("clear_api_key") is True,
            handler_api_key=request.DATA.get("handler_api_key"),
            clear_handler_api_key=request.DATA.get("clear_handler_api_key") is True,
            # Raw, never coerced: a JSON `null` arrives as None and means
            # "leave the switch alone"; the service refuses every other
            # non-boolean rather than reading it as an intent.
            mcp_enabled=request.DATA.get("mcp_enabled"),
            emergency_stop=request.DATA.get("emergency_stop"),
            autonomous_triage=request.DATA.get("autonomous_triage"))
        # Both actions answer with the fresh state, so a second editor holding a
        # stale page sees the truth on its very next call.
        return {"schema_version": assistant_setup.SCHEMA_VERSION,
                "saved": True, "state": saved}
    if action == "reset_breaker":
        if keys - {"action", "provider"}:
            raise merrors.ValueException(
                "Reset breaker accepts only action and provider")
        from mojo.apps.account.services import llm_safety
        reset = llm_safety.reset_breakers(
            request.user, provider=request.DATA.get("provider"))
        return {"schema_version": assistant_setup.SCHEMA_VERSION,
                "reset": reset, "state": assistant_setup.state()}
    if action == "historical_triage":
        if keys != {"action", "before", "limit"}:
            raise merrors.ValueException(
                "Historical triage requires exactly action, before, and limit")
        from django.utils.dateparse import parse_datetime
        from mojo.apps.incident.services import llm_dispatch
        before = parse_datetime(str(request.DATA.get("before") or ""))
        if before is None:
            raise merrors.ValueException("before must be an ISO timestamp")
        queued = llm_dispatch.start_historical_backlog(
            before, request.DATA.get("limit"), request.user)
        return {"schema_version": assistant_setup.SCHEMA_VERSION,
                "queued": queued, "state": assistant_setup.state()}
    if action == "revoke_grant":
        if keys != {"action", "grant_id"}:
            raise merrors.ValueException(
                "Revoke requires exactly action and grant_id")
        revoked = assistant_setup.revoke_grant(
            request.user, request.DATA.get("grant_id"))
        return {"schema_version": assistant_setup.SCHEMA_VERSION,
                "revoked": revoked, "state": assistant_setup.state()}
    if action == "revoke_all_grants":
        if keys != {"action"}:
            raise merrors.ValueException("Revoke all requires exactly action")
        revoked = assistant_setup.revoke_all_grants(request.user)
        return {"schema_version": assistant_setup.SCHEMA_VERSION,
                "revoked": revoked, "state": assistant_setup.state()}
    raise merrors.ValueException(
        "action must be verify, save, reset_breaker, historical_triage, "
        "revoke_grant, or revoke_all_grants")
