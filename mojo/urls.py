import importlib
import os
from django.urls import path, include
from mojo.helpers.settings import settings
from mojo.helpers import modules

MOJO_API_MODULE = settings.get("MOJO_API_MODULE", "rest")

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

def add_urlpatterns(app, prefix):
    app_module = modules.load_module(app)
    if len(prefix) > 1:
        prefix += "/"
    if not hasattr(app_module, "urlpatterns"):
        print(f"{app} has no api routes")
        return
    urls = path(prefix, include(app_module))
    urlpatterns.append(urls)

load_mojo_modules()
