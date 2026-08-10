"""Superuser-only System Setup and readiness endpoints."""

from mojo import decorators as md
from mojo.apps.account.services import system_readiness, system_setup


def _operation_id(request):
    return request.DATA.get("operation") or request.DATA.get("operation_id")


@md.GET("account/admin/setup/options")
@md.denies_key_backed_session()
@md.requires_global_perms("admin")
def on_setup_options(request):
    return system_setup.options(request)


@md.GET("account/admin/setup/readiness")
@md.denies_key_backed_session()
@md.requires_global_perms("admin")
def on_setup_readiness(request):
    system_setup.require_request_admin(request)
    section = request.DATA.get("section") or None
    return system_readiness.run(section, {
        "local_url": system_readiness.trusted_local_api_url(request),
        "timeout": 2.0, "retries": 1,
    })


@md.POST("account/admin/setup/create")
@md.denies_key_backed_session()
@md.requires_global_perms("admin")
@md.requires_fresh_auth(seconds=600)
def on_setup_create(request):
    mode = request.DATA.get("mode")
    operation, replayed = system_setup.create(
        request, mode, request.DATA.get("section", ""),
        request.DATA.get("replay_key", ""))
    data = system_setup.serialize(operation)
    data["replayed"] = replayed
    return data


@md.GET("account/admin/setup/detail")
@md.denies_key_backed_session()
@md.requires_global_perms("admin")
def on_setup_detail(request):
    return system_setup.serialize(system_setup.get(request, _operation_id(request)))


@md.POST("account/admin/setup/advance")
@md.denies_key_backed_session()
@md.requires_global_perms("admin")
@md.requires_fresh_auth(seconds=600)
def on_setup_advance(request):
    return system_setup.serialize(system_setup.advance(request, _operation_id(request)))


@md.POST("account/admin/setup/choose")
@md.denies_key_backed_session()
@md.requires_global_perms("admin")
@md.requires_fresh_auth(seconds=600)
def on_setup_choose(request):
    return system_setup.serialize(system_setup.choose(
        request, _operation_id(request), request.DATA.get("step_id"),
        request.DATA.get("definition_version"), request.DATA.get("choice_revision"),
        request.DATA.get("choice")))


@md.POST("account/admin/setup/cancel")
@md.denies_key_backed_session()
@md.requires_global_perms("admin")
@md.requires_fresh_auth(seconds=600)
def on_setup_cancel(request):
    return system_setup.serialize(system_setup.cancel(request, _operation_id(request)))
