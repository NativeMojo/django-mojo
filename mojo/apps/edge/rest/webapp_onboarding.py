"""Human-only WebApp onboarding and frozen summary endpoints."""

import mojo.decorators as md

from mojo import errors as me
from mojo.apps.account.models import Group
from mojo.apps.account.services import webapp_authority
from mojo.apps.edge.models import WebApp, WebAppOnboardingOperation
from mojo.apps.edge.services import webapp_onboarding as _webapp_onboarding


def _human(request):
    if (not webapp_authority.is_interactive_request(request) or
            not getattr(request.user, "is_authenticated", False) or
            getattr(request, "group_token", None) is not None):
        raise me.PermissionDeniedException(
            "An interactive user session is required for WebApp onboarding")


def _positive_group(value):
    if isinstance(value, bool) or isinstance(value, (list, tuple, dict)):
        raise me.ValueException("group must be a positive numeric id")
    if isinstance(value, str):
        value = value.strip()
        if not value.isdigit():
            raise me.ValueException("group must be a positive numeric id")
    try:
        group_id = int(value)
    except (TypeError, ValueError):
        raise me.ValueException("group must be a positive numeric id")
    if group_id <= 0:
        raise me.ValueException("group must be a positive numeric id")
    group = Group.get_active(group_id)
    if group is None:
        raise me.PermissionDeniedException("The selected group is unavailable")
    return group


def _group_intent(request):
    _human(request)
    has_group = "group" in request.DATA
    has_intent = "group_intent" in request.DATA
    if has_group == has_intent:
        raise me.ValueException(
            "Provide exactly one of group or group_intent=new")
    if has_intent:
        intent = request.DATA.get("group_intent")
        if not isinstance(intent, str) or intent.strip().lower() != "new":
            raise me.ValueException("group_intent must be new")
        if not webapp_authority.can_create_webapp_group(request.user):
            raise me.PermissionDeniedException(
                "Creating a WebApp group is not granted globally")
        return "new", None
    group = _positive_group(request.DATA.get("group"))
    if not webapp_authority.can_manage_group_webapps(request.user, group):
        raise me.PermissionDeniedException(
            "WebApp and DNS management are not granted in this group")
    return "existing", group


def _group(request):
    """Compatibility helper for concrete-group callers and focused tests."""
    intent, group = _group_intent(request)
    if intent != "existing":
        raise me.ValueException("A concrete group is required")
    return group


def _operation(request, mutate=False):
    _human(request)
    operation = WebAppOnboardingOperation.objects.select_related(
        "group", "actor", "web_app", "domain", "certificate", "vhost").filter(
            operation_id=request.DATA.get("operation")).first()
    if operation is None:
        raise me.RestErrorException(
            "WebApp onboarding operation not found", code=404, status=404)
    _webapp_onboarding._assert_current(operation, request, mutate=mutate)
    return operation


@md.GET("webapp/onboarding/options")
@md.denies_key_backed_session()
@md.custom_security("interactive user plus centralized WebApp group authority")
def on_webapp_onboarding_options(request):
    intent, group = _group_intent(request)
    return _webapp_onboarding.options(group, group_intent=intent)


@md.POST("webapp/onboarding/create")
@md.denies_key_backed_session()
@md.custom_security("interactive user plus centralized WebApp group authority")
def on_webapp_onboarding_create(request):
    intent, group = _group_intent(request)
    operation, created = _webapp_onboarding.create(
        group, request.user, _webapp_onboarding.request_origin(request),
        request.DATA, group_intent=intent)
    return {"created": created, "operation": _webapp_onboarding.serialize(operation)}


@md.GET("webapp/onboarding/detail")
@md.denies_key_backed_session()
@md.requires_params("operation")
@md.custom_security("operation actor and RestMeta group scope in body")
def on_webapp_onboarding_detail(request):
    return _webapp_onboarding.serialize(_operation(request))


@md.POST("webapp/onboarding/choose")
@md.denies_key_backed_session()
@md.requires_fresh_auth(600)
@md.requires_params("operation", "revision", "step", "choice")
@md.custom_security("operation actor, origin, revision, and RestMeta group scope in body")
def on_webapp_onboarding_choose(request):
    operation = _webapp_onboarding.choose(
        _operation(request, mutate=True), request, request.DATA)
    operation.refresh_from_db()
    return _webapp_onboarding.serialize(operation)


@md.POST("webapp/onboarding/cancel")
@md.denies_key_backed_session()
@md.requires_fresh_auth(600)
@md.requires_params("operation")
@md.custom_security("operation actor, origin, and RestMeta group scope in body")
def on_webapp_onboarding_cancel(request):
    operation = _webapp_onboarding.cancel(
        _operation(request, mutate=True), request)
    return _webapp_onboarding.serialize(operation)


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
    if not webapp_authority.can_manage_group_webapps(
            request.user, web_app.group):
        raise me.PermissionDeniedException(
            "WebApp and DNS management are not granted in this group")
    result = _webapp_onboarding.workflow(web_app)
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
    return _webapp_onboarding.summary_for(web_app)
