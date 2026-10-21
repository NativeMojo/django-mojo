"""Explicit shared-validation actions; ordinary DNS authority never grants trust."""
import mojo.decorators as md
from mojo import errors as me
from mojo.apps.dnsman.services import delegation_setup as setup


def _input(request, allowed, required=()):
    data = request.DATA
    if not isinstance(data, dict) or set(data) - set(allowed) or not set(required).issubset(data):
        raise me.ValueException("Invalid shared DNS setup input")
    return data


def _target(request, data, write=False):
    return setup.resolve_target(domain=data.get("domain"), group=data.get("group"),
                                name=data.get("name"), actor=request, write=write)


def _action(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except setup.SetupError as error:
        return dict(status=False, code=error.status, error=str(error),
                    error_code=error.error_code, data=error.data)


@md.GET("delegation/plan")
@md.custom_security("Resolve exact Domain or active tenant and check DNS view authority before any hub I/O")
def on_delegation_plan(request):
    data = _input(request, ("domain", "group", "name", "allocation_ref"))
    target = _target(request, data)
    return setup.plan(target, allocation_ref=data.get("allocation_ref"), actor=request)


@md.POST("delegation/connect")
@md.custom_security("Exact tenant DNS management authority; static hub identity confers no controller power")
def on_delegation_connect(request):
    data = _input(request, ("domain", "group", "name", "allocation_ref", "restart"), ("allocation_ref",))
    return _action(lambda: setup.connect(_target(request, data), data["allocation_ref"],
                                        restart=data.get("restart", False), actor=request))


@md.POST("delegation/check")
@md.custom_security("Resolve the local connection with exact tenant DNS management authority before proof")
def on_delegation_check(request):
    data = _input(request, ("delegation",), ("delegation",))
    return _action(setup.check, data["delegation"], actor=request)


@md.POST("delegation/request")
@md.custom_security("Resolve exact local connection and recheck DNS management, active trust and immutable profile")
def on_delegation_request(request):
    data = _input(request, ("delegation",), ("delegation",))
    return _action(setup.request, data["delegation"], actor=request)


@md.GET("delegation/group-choice")
@md.custom_security("Return only active local groups on which the underlying caller holds exact DNS setup authority")
def on_delegation_group_choice(request):
    data = _input(request, ("id", "search", "start", "size"))
    if "id" in data and len(data) != 1:
        raise me.ValueException("Provide id or a paginated DNS group query")
    return setup.group_choices(request, **data)
