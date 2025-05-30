import sys
import traceback
from mojo.helpers.settings import settings
from mojo.helpers import modules as jm
from mojo.helpers import logit
import mojo.errors
from django.urls import path, re_path
from django.http import JsonResponse
from functools import wraps
from mojo.helpers.request import parse_request_data
from mojo.helpers import modules
from mojo.models import rest

logger = logit.get_logger("error", "error.log")
# logger.info("created")

# Global registry for REST routes
REGISTERED_URLS = {}
URLPATTERN_METHODS = {}
MOJO_API_MODULE = settings.get("MOJO_API_MODULE", "api")
MOJO_APPEND_SLASH = settings.get("MOJO_APPEND_SLASH", False)


def dispatcher(request, *args, **kwargs):
    """
    Dispatches incoming requests to the appropriate registered URL method.
    """
    rest.ACTIVE_REQUEST = request
    key = kwargs.pop('__mojo_rest_key__', None)
    request.DATA = parse_request_data(request)
    if "group" in request.DATA:
        request.group = modules.get_model_instance("account", "Group", int(request.DATA.group))
    logger.info(request.DATA)
    if key in URLPATTERN_METHODS:
        return dispatch_error_handler(URLPATTERN_METHODS[key])(request, *args, **kwargs)
    return JsonResponse({"error": "Endpoint not found", "code": 404}, status=404)


def dispatch_error_handler(func):
    """
    Decorator to catch and handle errors.
    It logs exceptions and returns appropriate HTTP responses.
    """
    @wraps(func)
    def wrapper(request, *args, **kwargs):
        try:
            return func(request, *args, **kwargs)
        except mojo.errors.MojoException as err:
            return JsonResponse({"error": err.reason, "code": err.code}, status=err.status)
        except ValueError as err:
            logger.exception(f"Error: {str(err)}, Path: {request.path}, IP: {request.META.get('REMOTE_ADDR')}")
            return JsonResponse({"error": str(err), "code": 555 }, status=500)
        except Exception as err:
            # logger.exception(f"Unhandled REST Exception: {request.path}")
            logger.exception(f"Error: {str(err)}, Path: {request.path}, IP: {request.META.get('REMOTE_ADDR')}")
            return JsonResponse({"error": str(err) }, status=500)

    return wrapper


def _register_route(method="ALL"):
    """
    Decorator to automatically register a Django view for a specific HTTP method.
    Supports defining a custom pattern inside the decorator.

    :param method: The HTTP method (GET, POST, etc.).
    """
    def decorator(pattern=None):
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
            key = f"{app_name}__{pattern_used}__{method}"
            # print(f"{app_name} -> {pattern_used} -> {key}")
            URLPATTERN_METHODS[key] = view_func

            # Determine whether to use path() or re_path()
            url_func = path if not (pattern_used.startswith("^") or pattern_used.endswith("$")) else re_path

            # Add to `urlpatterns`
            module.urlpatterns.append(url_func(
                pattern_used, dispatcher,
                kwargs={
                    "__mojo_rest_key__": key
                }))
            # Attach metadata
            view_func.__url__ = (method, pattern_used)
            return view_func
        return wrapper
    return decorator

# Public-facing URL decorators
URL = _register_route()
GET = _register_route("GET")
POST = _register_route("POST")
PUT = _register_route("PUT")
DELETE = _register_route("DELETE")
