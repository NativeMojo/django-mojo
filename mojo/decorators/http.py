import sys
import traceback
from mojo.helpers.settings import settings
from mojo.helpers import modules as jm
from mojo.helpers import logit
import mojo.errors
from django.urls import path, re_path
# from django.http import JsonResponse
from mojo.helpers.response import JsonResponse
from functools import wraps
from mojo.helpers import modules
from mojo.models import rest
from django.http import HttpResponse
from mojo.apps import metrics

logger = logit.get_logger("error", "error.log")
# logger.info("created")

# Global registry for REST routes
REGISTERED_URLS = {}
URLPATTERN_METHODS = {}
# Global list for absolute URLs (those starting with "/") that bypass app prefixes
ABSOLUTE_URLPATTERNS = []


def _mojo_append_slash():
    return settings.get("MOJO_APPEND_SLASH", False)


def _api_metrics_enabled():
    return settings.get("API_METRICS", False)


def _api_metrics_granularity():
    return settings.get("API_METRICS_GRANULARITY", "days")


def _events_on_errors():
    return settings.get("EVENTS_ON_ERRORS", True)


def _status_200_on_error():
    return settings.get("MOJO_APP_STATUS_200_ON_ERROR", False)


_PERMISSION_DENIED_LEVELS = {
    "unauthenticated": 3,
    "feature_disabled": 3,
}


def _emit_permission_denied_event(err, request):
    """Report a PermissionDeniedException with full metadata to the incident system."""
    level = _PERMISSION_DENIED_LEVELS.get(err.event_type, 4)
    rest.MojoModel.class_report_incident_for_user(
        details=f"Permission denied: {err.reason}",
        event_type=err.event_type or "user_permission_denied",
        request=request,
        level=level,
        branch=err.branch,
        perms=err.perms,
        permission_keys=err.permission_keys,
        model_name=err.model_name,
        instance=err.instance,
        request_path=getattr(request, "path", None),
    )


def dispatcher(request, *args, **kwargs):
    """
    Dispatches incoming requests to the appropriate registered URL method.
    """
    key = kwargs.pop('__mojo_rest_root_key__', None)
    if "group" in request.DATA and request.DATA.group:
        try:
            request.group = modules.get_model_instance("account", "Group", int(request.DATA.group))
            if request.group is not None:
                request.group.touch()
            api_key = getattr(request, "api_key", None)
            if api_key and request.group and not api_key.is_group_allowed(request.group):
                return JsonResponse({"error": "Group not accessible with this API key", "code": 403}, status=403)
        except ValueError:
            if _events_on_errors():
                rest.MojoModel.class_report_incident(
                    details=f"Permission denied: Invalid group ID -> '{request.DATA.group}'",
                    event_type="rest_error",
                    request=request,
                    level=8,
                    request_path=getattr(request, "path", None),
                )
            return JsonResponse({"error": "Invalid group ID", "code": 400}, status=400)
    # Fallback: accept ?group_uuid=<uuid> for endpoints that pass the active
    # group by UUID rather than integer-id (e.g. OAuth begin, geofence pre-flight).
    # Only used when `group` was not already resolved above.
    #
    # SECURITY: filter is_active=True so inactive groups never become
    # request.group via this public path — prevents touching the modified
    # timestamp and avoids a slight existence-disclosure via side effect.
    # Endpoints that need to surface inactive-group state (e.g. /api/geo/check)
    # do their own lookup against Group.objects directly.
    elif "group_uuid" in request.DATA and request.DATA.group_uuid and not getattr(request, "group", None):
        from mojo.apps.account.models.group import Group
        group_uuid = str(request.DATA.group_uuid).strip()
        if group_uuid:
            grp = Group.objects.filter(uuid=group_uuid, is_active=True).first()
            if grp is not None:
                request.group = grp
                grp.touch()
                api_key = getattr(request, "api_key", None)
                if api_key and not api_key.is_group_allowed(grp):
                    return JsonResponse({"error": "Group not accessible with this API key", "code": 403}, status=403)
    method_key = f"{key}__{request.method}"
    if method_key not in URLPATTERN_METHODS:
        method_key = f"{key}__ALL"
    if method_key in URLPATTERN_METHODS:
        return dispatch_error_handler(URLPATTERN_METHODS[method_key])(request, *args, **kwargs)
    return JsonResponse({"error": "Endpoint not found", "code": 404}, status=404)


def dispatch_error_handler(func):
    """
    Decorator to catch and handle errors.
    It logs exceptions and returns appropriate HTTP responses.
    """
    @wraps(func)
    def wrapper(request, *args, **kwargs):
        try:
            if _api_metrics_enabled():
                metrics.record("api_calls", category="mojo_api", min_granularity=_api_metrics_granularity())
            resp = func(request, *args, **kwargs)
            if isinstance(resp, HttpResponse):
                return resp
            if resp is None:
                return JsonResponse({"error": "No response", "code": 500}, status=500)
            if isinstance(resp, (list, tuple)):
                return JsonResponse({"status": True, "code": 200, "data": resp, "size": len(resp)})
            if isinstance(resp, dict):
                if isinstance(resp.get("status"), bool) and ("data" in resp or "error" in resp or "message" in resp):
                    return JsonResponse(resp, status=resp.get("code", 200))
                return JsonResponse({"status": True, "code": 200, "data": resp})
            return resp
        except mojo.errors.MojoException as err:
            is_perm_denied = isinstance(err, mojo.errors.PermissionDeniedException)
            # A step-up (440) is an expected access gate, not a server error: count
            # it like a denial, and never fire an error incident — doing so would
            # log the request body (which may carry a new email/username/TOTP code)
            # and pollute mojo_rest_error alerting on a routine re-auth prompt.
            is_reauth = isinstance(err, mojo.errors.ReauthRequiredException)
            metric_key = "api_denied" if (is_perm_denied or is_reauth) else "api_errors"
            if _api_metrics_enabled():
                metrics.record(metric_key, category="mojo_api", min_granularity=_api_metrics_granularity())
            if _events_on_errors() and not is_reauth:
                if is_perm_denied:
                    _emit_permission_denied_event(err, request)
                else:
                    rest.MojoModel.class_report_incident_for_user(
                        details=f"Rest Mojo Error: {err.reason}",
                        event_type="mojo_rest_error",
                        request_data=request.DATA,
                        request=request,
                        level=5,
                        request_path=getattr(request, "path", None),
                        error_code=err.code,
                        stack_trace=traceback.format_exc(),
                    )
            wire_status = 200 if _status_200_on_error() else err.status
            return JsonResponse({"error": err.reason, "code": err.code, "status": False }, status=wire_status)
        except PermissionError as err:
            if _api_metrics_enabled():
                metrics.record("api_denied", category="mojo_api", min_granularity=_api_metrics_granularity())
            if _events_on_errors():
                rest.MojoModel.class_report_incident_for_user(
                    details=f"Permission Denied: {err}",
                    event_type="api_denied",
                    request_data=request.DATA,
                    request=request,
                    level=4,
                    request_path=getattr(request, "path", None)
                )
            return JsonResponse({"error": str(err), "code": 403, "status": False }, status=403)
        except ValueError as err:
            if _api_metrics_enabled():
                metrics.record("api_errors", category="mojo_api", min_granularity=_api_metrics_granularity())
            logger.exception(f"ValueErrror: {str(err)}, Path: {request.path}, IP: {request.META.get('REMOTE_ADDR')}")
            if _events_on_errors():
                rest.MojoModel.class_report_incident_for_user(
                    details=f"Rest Value Error: {err}",
                    event_type="rest_value_error",
                    request_data=request.DATA,
                    request=request,
                    level=4,
                    request_path=getattr(request, "path", None),
                    stack_trace=traceback.format_exc()
                )
            return JsonResponse({"error": str(err), "code": 400, "status": False  }, status=400)
        except Exception as err:
            if _api_metrics_enabled():
                metrics.record("api_errors", category="mojo_api", min_granularity=_api_metrics_granularity())
            # logger.exception(f"Unhandled REST Exception: {request.path}")
            logger.exception(f"Error: {str(err)}, Path: {request.path}, IP: {request.META.get('REMOTE_ADDR')}")
            if _events_on_errors():
                rest.MojoModel.class_report_incident_for_user(
                    details=f"Rest Exception: {err}",
                    event_type="rest_error",
                    request_data=request.DATA,
                    request=request,
                    level=12,
                    stack_trace=traceback.format_exc(),
                    request_path=getattr(request, "path", None),
                )
            return JsonResponse({"error": str(err), "code": 500, "status": False  }, status=500)

    return wrapper


def _register_route(method="ALL"):
    """
    Decorator to automatically register a Django view for a specific HTTP method.
    Supports defining a custom pattern inside the decorator.

    Paths starting with "/" are treated as absolute and bypass the app prefix:
    - md.URL("myapi") in "myapp" -> /api/myapp/myapi
    - md.URL("/root/myapi") in "myapp" -> /api/root/myapi (bypasses "myapp" prefix)

    :param method: The HTTP method (GET, POST, etc.).
    """
    def decorator(pattern=None, docs=None):
        def wrapper(view_func):
            module = jm.get_root_module(view_func)
            if not module:
                print("!!!!!!!")
                print(sys._getframe(2).f_code.co_filename)
                raise RuntimeError(f"Could not determine module for {view_func.__name__}")

            # Ensure `urlpatterns` exists in the calling module
            if not hasattr(module, 'urlpatterns'):
                module.urlpatterns = []

            # If no pattern is provided, use the function name as the pattern
            if pattern is None:
                pattern_used = f"{view_func.__name__}"
            else:
                pattern_used = pattern

            # Check if this is an absolute path (starts with "/")
            is_absolute = pattern_used.startswith("/")

            # Strip leading "/" for absolute paths since Django path() doesn't expect it
            if is_absolute:
                pattern_used = pattern_used.lstrip("/")

            if _mojo_append_slash():
                pattern_used = pattern if pattern_used.endswith("/") else f"{pattern_used}/"

            # Register view in URL mapping
            app_name = module.__name__.split(".")[-1]

            # For absolute paths, use a special prefix to indicate they bypass app prefix
            if is_absolute:
                root_key = f"__absolute__{pattern_used}"
            else:
                root_key = f"{app_name}__{pattern_used}"

            key = f"{root_key}__{method}"
            URLPATTERN_METHODS[key] = view_func

            # Determine whether to use path() or re_path()
            url_func = path if not (pattern_used.startswith("^") or pattern_used.endswith("$")) else re_path

            # Create the URL pattern
            url_pattern = url_func(
                pattern_used, dispatcher,
                kwargs={
                    "__mojo_rest_root_key__": root_key
                })

            # Add to appropriate urlpatterns list
            if is_absolute:
                # Absolute paths go to global list (bypass app prefix)
                ABSOLUTE_URLPATTERNS.append(url_pattern)
            else:
                # Regular paths go to module urlpatterns (get app prefix)
                module.urlpatterns.append(url_pattern)

            # Attach metadata
            view_func.__app_module_name__ = module.__name__
            view_func.__app_name__ = app_name
            view_func.__url__ = (method, pattern_used)
            view_func.__is_absolute__ = is_absolute
            view_func.__docs__ = docs or {}
            return view_func
        return wrapper
    return decorator

# Public-facing URL decorators
URL = _register_route()
GET = _register_route("GET")
POST = _register_route("POST")
PUT = _register_route("PUT")
DELETE = _register_route("DELETE")
