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
MOJO_API_MODULE = settings.get("MOJO_API_MODULE", "api")
MOJO_APPEND_SLASH = settings.get("MOJO_APPEND_SLASH", False)

API_METRICS = settings.get("API_METRICS", False)
API_METRICS_GRANULARITY = settings.get("API_METRICS_GRANULARITY", "days")
EVENTS_ON_ERRORS = settings.get("EVENTS_ON_ERRORS", True)


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
        except ValueError:
            if EVENTS_ON_ERRORS:
                rest.MojoModel.class_report_incident(
                    details=f"Permission denied: Invalid group ID -> '{request.DATA.group}'",
                    event_type="rest_error",
                    request=request,
                    level=8,
                    request_path=getattr(request, "path", None),
                )
            return JsonResponse({"error": "Invalid group ID", "code": 400}, status=400)
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
            if API_METRICS:
                metrics.record("api_calls", category="mojo_api", min_granularity=API_METRICS_GRANULARITY)
            resp = func(request, *args, **kwargs)
            if not isinstance(resp, HttpResponse) and isinstance(resp, dict):
                return JsonResponse(resp)
            elif resp is None:
                return JsonResponse({"error": "No response", "code": 500}, status=500)
            return resp
        except mojo.errors.MojoException as err:
            if API_METRICS:
                metrics.record("api_errors", category="mojo_api", min_granularity=API_METRICS_GRANULARITY)
            if EVENTS_ON_ERRORS:
                rest.MojoModel.class_report_incident_for_user(
                    details=f"Rest Mojo Error: {err.reason}",
                    event_type="rest_error",
                    request_data=request.DATA,
                    request=request,
                    level=5,
                    request_path=getattr(request, "path", None),
                    stack_trace=traceback.format_exc(),
                )
            return JsonResponse({"error": err.reason, "code": err.code}, status=err.status)
        except ValueError as err:
            if API_METRICS:
                metrics.record("api_errors", category="mojo_api", min_granularity=API_METRICS_GRANULARITY)
            logger.exception(f"ValueErrror: {str(err)}, Path: {request.path}, IP: {request.META.get('REMOTE_ADDR')}")
            if EVENTS_ON_ERRORS:
                rest.MojoModel.class_report_incident_for_user(
                    details=f"Rest Value Error: {err}",
                    event_type="rest_error",
                    request_data=request.DATA,
                    request=request,
                    level=4,
                    request_path=getattr(request, "path", None),
                    stack_trace=traceback.format_exc()
                )
            return JsonResponse({"error": str(err), "code": 555 }, status=500)
        except Exception as err:
            if API_METRICS:
                metrics.record("api_errors", category="mojo_api", min_granularity=API_METRICS_GRANULARITY)
            # logger.exception(f"Unhandled REST Exception: {request.path}")
            logger.exception(f"Error: {str(err)}, Path: {request.path}, IP: {request.META.get('REMOTE_ADDR')}")
            if EVENTS_ON_ERRORS:
                rest.MojoModel.class_report_incident_for_user(
                    details=f"Rest Exception: {err}",
                    event_type="rest_error",
                    request_data=request.DATA,
                    request=request,
                    level=9,
                    stack_trace=traceback.format_exc(),
                    request_path=getattr(request, "path", None),
                )
            return JsonResponse({"error": str(err), "code": 500 }, status=500)

    return wrapper


def _register_route(method="ALL"):
    """
    Decorator to automatically register a Django view for a specific HTTP method.
    Supports defining a custom pattern inside the decorator.

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

            if MOJO_APPEND_SLASH:
                pattern_used = pattern if pattern_used.endswith("/") else f"{pattern_used}/"
            # Register view in URL mapping
            app_name = module.__name__.split(".")[-1]
            # print(f"{module.__name__}.urlpatterns")
            root_key = f"{app_name}__{pattern_used}"
            key = f"{root_key}__{method}"
            # print(f"{app_name} -> {pattern_used} -> {key}")
            URLPATTERN_METHODS[key] = view_func

            # Determine whether to use path() or re_path()
            url_func = path if not (pattern_used.startswith("^") or pattern_used.endswith("$")) else re_path

            # Add to `urlpatterns`
            module.urlpatterns.append(url_func(
                pattern_used, dispatcher,
                kwargs={
                    "__mojo_rest_root_key__": root_key
                }))
            # Attach metadata
            view_func.__app_module_name__ = module.__name__
            view_func.__app_name__ = app_name
            view_func.__url__ = (method, pattern_used)
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
