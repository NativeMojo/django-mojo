import importlib
import os
from django.urls import path, include
from mojo.helpers.settings import settings
from mojo.helpers import modules

MOJO_API_MODULE = settings.get("MOJO_API_MODULE", "rest")

# REST_AUTO_PREFIX controls how MOJO_PREFIX is applied:
# - False (default): Django project must handle MOJO_PREFIX (old way)
#     Django: path(MOJO_PREFIX, include('mojo.urls'))
#     Result: /api/myapp/endpoint
# - True: MOJO framework handles MOJO_PREFIX automatically
#     Django: path("", include('mojo.urls'))
#     Result: /api/myapp/endpoint (app routes)
#     Result: /absolute/endpoint (absolute routes starting with "/")
REST_AUTO_PREFIX = settings.get("REST_AUTO_PREFIX", False)
MOJO_PREFIX = settings.get("MOJO_PREFIX", "api").strip("/")

urlpatterns = []

def load_mojo_modules():
    # load the module to load its patterns
    rest_module = modules.load_module(f"mojo.{MOJO_API_MODULE}", ignore_errors=False)
    add_urlpatterns("mojo", prefix="")

    for app in settings.INSTALLED_APPS:
        module_name = f"{app}.{MOJO_API_MODULE}"
        if not modules.module_exists(module_name):
            continue
        rest_module = modules.load_module(module_name, ignore_errors=False)
        if rest_module:
            app_name = app
            if "." in app:
                app_name = app.split('.')[-1]
            prefix = getattr(rest_module, 'APP_NAME', app_name)
            add_urlpatterns(app, prefix)

    # Add absolute URL patterns (those starting with "/") without app prefix or MOJO_PREFIX
    add_absolute_urlpatterns()

def add_urlpatterns(app, prefix):
    """
    Add app-specific URL patterns with appropriate prefixing.

    If REST_AUTO_PREFIX is enabled, patterns are wrapped with MOJO_PREFIX.
    """
    app_module = modules.load_module(app)
    if len(prefix) > 1:
        prefix += "/"
    if not hasattr(app_module, "urlpatterns"):
        print(f"{app} has no api routes")
        return

    # If REST_AUTO_PREFIX is enabled, wrap with MOJO_PREFIX
    if REST_AUTO_PREFIX and MOJO_PREFIX:
        # Combine MOJO_PREFIX with app prefix
        full_prefix = f"{MOJO_PREFIX}/{prefix}"
        urls = path(full_prefix, include(app_module))
    else:
        # Old behavior: just use app prefix (Django project handles MOJO_PREFIX)
        urls = path(prefix, include(app_module))

    urlpatterns.append(urls)

def add_absolute_urlpatterns():
    """
    Add URL patterns that were registered with absolute paths (starting with "/").

    - If REST_AUTO_PREFIX=True: mounted at root (bypasses both app prefix AND MOJO_PREFIX)
    - If REST_AUTO_PREFIX=False: mounted under whatever prefix Django project used (bypasses only app prefix)
    """
    from mojo.decorators.http import ABSOLUTE_URLPATTERNS

    if not ABSOLUTE_URLPATTERNS:
        return

    # When REST_AUTO_PREFIX is enabled, absolute URLs are mounted directly at root
    # When disabled, they are mounted under whatever prefix the Django project used
    # In both cases, they bypass the app-specific prefix
    urlpatterns.extend(ABSOLUTE_URLPATTERNS)

load_mojo_modules()
