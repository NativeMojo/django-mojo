import importlib
import os
from django.urls import path, include
from mojo.helpers.settings import settings
from mojo.helpers import modules

MOJO_API_MODULE = settings.get("MOJO_API_MODULE", "rest")

urlpatterns = []

def load_jest_modules():
    for app in settings.INSTALLED_APPS:
        module_name = f"{app}.{MOJO_API_MODULE}"
        if not modules.module_exists(module_name):
            continue
        module = modules.load_module(module_name, ignore_errors=False)
        app_module = modules.load_module(app)
        if module:
            prefix = getattr(module, 'APP_NAME', app)
            if len(prefix) > 1:
                prefix += "/"
            # urls = path(prefix, include(module))
            urls = path(prefix, include(app_module))
            urlpatterns.append(urls)

load_jest_modules()
