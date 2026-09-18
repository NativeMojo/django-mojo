"""Interactive-superuser API for structured fleet configuration."""
import os
import re
import socket

from django.core import signing

from mojo import decorators as md
from mojo import errors as merrors
from mojo.helpers.settings import settings


def _actor(request, write=False):
    from mojo.apps.account.services import system_setup
    actor = system_setup.require_request_admin(request)
    if write:
        system_setup.request_origin(request)
    return actor


@md.GET("account/admin/fleet")
@md.denies_key_backed_session()
@md.requires_global_perms("admin")
def on_admin_fleet(request):
    from mojo.apps.account.services import fleet_config, fleet_apply
    actor = _actor(request)
    result = fleet_config.state(actor)
    result["fleet"] = fleet_apply.observe(result.get("revision"))
    return result


@md.GET("account/admin/fleet/history")
@md.denies_key_backed_session()
@md.requires_global_perms("admin")
def on_admin_fleet_history(request):
    from mojo.apps.account.services import fleet_config
    return fleet_config.history(_actor(request))


@md.GET("account/admin/fleet/operation/<str:operation_id>")
@md.denies_key_backed_session()
@md.requires_global_perms("admin")
def on_admin_fleet_operation(request, operation_id):
    from mojo.apps.account.services import fleet_apply
    return fleet_apply.operation(_actor(request), operation_id)


@md.POST("account/admin/fleet")
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("admin")
def on_admin_fleet_mutate(request):
    from mojo.apps.account.services import fleet_config, fleet_apply
    actor = _actor(request, write=True)
    action = request.DATA.get("action")
    payload = {key: value for key, value in request.DATA.items() if key != "action"}
    if action == "publish":
        return fleet_config.publish(actor, payload)
    if action == "restore":
        return fleet_config.restore(actor, payload)
    if action == "apply":
        return fleet_apply.apply(actor, payload)
    raise merrors.ValueException("action must be publish, restore, or apply")


@md.GET("account/admin/fleet/proof")
def on_fleet_serving_proof(request):
    # UDS probes are signed. Proxy headers may rewrite REMOTE_ADDR, so an
    # apparent loopback address alone cannot establish machine authority.
    token = request.META.get("HTTP_X_MOJO_FLEET_PROOF", "")
    try:
        if not isinstance(token, str) or len(token) > 2048:
            raise ValueError()
        challenge = signing.loads(token, salt="mojo.fleet.config.proof", max_age=30)
        if (not isinstance(challenge, dict) or set(challenge) != {"revision", "nonce", "node"}
                or not isinstance(challenge["nonce"], str)
                or not re.fullmatch(r"[a-f0-9]{32}", challenge["nonce"])
                or not isinstance(challenge["revision"], str)
                or not re.fullmatch(r"[a-f0-9]{32,64}", challenge["revision"])
                or challenge["node"] != socket.gethostname().lower()):
            raise ValueError()
    except (signing.BadSignature, ValueError, TypeError, KeyError):
        raise merrors.PermissionDeniedException("Invalid configuration proof challenge") from None
    healthy = False
    try:
        from mojo.apps.account.services import admin_platform
        healthy = admin_platform._database()["reachable"] and admin_platform._redis()["reachable"]
    except Exception:
        pass
    return {"loaded_revision": settings.get_static("MOJO_FLEET_CONFIG_REVISION", None),
            "pid": os.getpid(), "node": socket.gethostname().lower(),
            "nonce": challenge["nonce"], "healthy": bool(healthy)}
