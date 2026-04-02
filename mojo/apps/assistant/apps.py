from django.apps import AppConfig as BaseAppConfig
from django.utils.module_loading import autodiscover_modules


class AppConfig(BaseAppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mojo.apps.assistant'

    def ready(self):
        # Register built-in tools first
        from mojo.apps.assistant.services import tools as _  # noqa: F401
        # Then discover assistant_tools.py in all installed apps
        autodiscover_modules("assistant_tools")
