"""Human-only WebApp onboarding and frozen summary endpoints."""

import mojo.decorators as md

from mojo import errors as me
from mojo.apps.account.models import Group
from mojo.apps.edge.models import WebApp, WebAppOnboardingOperation
from mojo.apps.edge.services import webapp_onboarding


def _human(request):
    from mojo.helpers.request import is_request_user

    if (not is_request_user(request) or
            not getattr(request.user, "is_authenticated", False) or
            getattr(request, "group_token", None) is not None):
        raise me.PermissionDeniedException(
            "An interactive user session is required for WebApp onboarding")


def _group(request):
    _human(request)
    group = Group.get_instance_or_404(request.DATA.get("group"))
    if not group.is_effectively_active():
        raise me.PermissionDeniedException("The selected group is inactive")
    if not group.user_has_permission(
            request.user, ["manage_webapp", "security"]):
        raise me.PermissionDeniedException(
            "WebApp management is not granted in this group")
    return group


def _operation(request, mutate=False):
    _human(request)
    operation = WebAppOnboardingOperation.objects.select_related(
        "group", "actor", "web_app", "domain", "certificate", "vhost").filter(
            operation_id=request.DATA.get("operation")).first()
    if operation is None:
        raise me.RestErrorException(
            "WebApp onboarding operation not found", code=404, status=404)
    webapp_onboarding._assert_current(operation, request, mutate=mutate)
    return operation


@md.GET("webapp/onboarding/options")
@md.denies_key_backed_session()
@md.requires_params("group")
@md.requires_perms("manage_webapp", "security")
def on_webapp_onboarding_options(request):
    return webapp_onboarding.options(_group(request))


@md.POST("webapp/onboarding/create")
@md.denies_key_backed_session()
@md.requires_params("group", "slug", "bucket")
@md.requires_perms("manage_webapp", "security")
def on_webapp_onboarding_create(request):
    group = _group(request)
    operation, created = webapp_onboarding.create(
        group, request.user, webapp_onboarding.request_origin(request), request.DATA)
    return {"created": created, "operation": webapp_onboarding.serialize(operation)}


@md.GET("webapp/onboarding/detail")
@md.denies_key_backed_session()
@md.requires_params("operation")
@md.custom_security("operation actor and RestMeta group scope in body")
def on_webapp_onboarding_detail(request):
    return webapp_onboarding.serialize(_operation(request))


@md.POST("webapp/onboarding/choose")
@md.denies_key_backed_session()
@md.requires_fresh_auth(600)
@md.requires_params("operation", "revision", "step", "choice")
@md.custom_security("operation actor, origin, revision, and RestMeta group scope in body")
def on_webapp_onboarding_choose(request):
    operation = webapp_onboarding.choose(
        _operation(request, mutate=True), request, request.DATA)
    operation.refresh_from_db()
    return webapp_onboarding.serialize(operation)


@md.POST("webapp/onboarding/cancel")
@md.denies_key_backed_session()
@md.requires_fresh_auth(600)
@md.requires_params("operation")
@md.custom_security("operation actor, origin, and RestMeta group scope in body")
def on_webapp_onboarding_cancel(request):
    operation = webapp_onboarding.cancel(
        _operation(request, mutate=True), request)
    return webapp_onboarding.serialize(operation)


@md.POST("webapp/onboarding/workflow")
@md.denies_key_backed_session()
@md.requires_fresh_auth(600)
@md.requires_params("webapp")
@md.custom_security("exact WebApp group manage permission in body")
def on_webapp_onboarding_workflow(request):
    """Return safe workflow text and optionally a newly minted key once."""
    from mojo.apps.edge.services import webapp_keys

    _human(request)
    web_app = WebApp.get_instance_or_404(request.DATA.get("webapp"))
    if (not web_app.group.is_effectively_active() or not
            web_app.group.user_has_permission(
                request.user, ["manage_webapp", "security"])):
        raise me.PermissionDeniedException(
            "WebApp management is not granted in this group")
    result = webapp_onboarding.workflow(web_app)
    action = str(request.DATA.get("action") or "").strip().lower()
    if action:
        operation_id = request.DATA.get("operation_id")
        if not operation_id:
            raise me.ValueException("operation_id is required to create a key")
        receipt = webapp_keys.link_once(
            web_app, action, request.user, operation_id)
        result["deployment_key"] = receipt
        if receipt.get("replayed") and not receipt.get("token"):
            result["deployment_key"]["delivery"] = "secret_unavailable"
    return result


@md.GET("webapp/summary")
@md.denies_key_backed_session()
@md.requires_params("webapp")
@md.custom_security("WebApp RestMeta group scope in body")
def on_webapp_summary(request):
    _human(request)
    web_app = WebApp.get_instance_or_404(request.DATA.get("webapp"))
    WebApp.rest_check_permission_or_raise(
        request, ["VIEW_PERMS", "SAVE_PERMS"], web_app)
    return webapp_onboarding.summary_for(web_app)
