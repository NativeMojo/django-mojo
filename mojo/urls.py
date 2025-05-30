import importlib
import os
from django.urls import path, include
from mojo.helpers.settings import settings
from mojo.helpers import modules

MOJO_API_MODULE = settings.get("MOJO_API_MODULE", "rest")

urlpatterns = []

def load_mojo_modules():
    module = modules.load_module(f"mojo.{MOJO_API_MODULE}", ignore_errors=False)
    add_urlpatterns("mojo", module, prefix="")

    for app in settings.INSTALLED_APPS:
        # print(f"=== {app} ===")
        module_name = f"{app}.{MOJO_API_MODULE}"
        if not modules.module_exists(module_name):
            continue
        module = modules.load_module(module_name, ignore_errors=False)
        if module:
            app_name = app
            if "." in app:
                app_name = app.split('.')[-1]
            prefix = getattr(module, 'APP_NAME', app_name)
            add_urlpatterns(app, module, prefix)

def add_urlpatterns(app, module, prefix):
    app_module = modules.load_module(app)
    if len(prefix) > 1:
        prefix += "/"
    # urls = path(prefix, include(module))
    # print(f">> {app}")
    # print(f"mod: {module}\napp mod: {app_module}\nprefix: {prefix}")
    if not hasattr(app_module, "urlpatterns"):
        # print("no  urlpatterns")
        return
    # print('---')
    urls = path(prefix, include(app_module))
    urlpatterns.append(urls)

load_mojo_modules()
