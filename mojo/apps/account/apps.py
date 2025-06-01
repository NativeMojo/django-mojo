from django.apps import AppConfig as BaseAppConfig

class AppConfig(BaseAppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mojo.apps.account'

    def ready(self):
        from mojo.helpers.settings import settings
        if settings.is_app_installed("django.contrib.admin"):
            self.unregister_apps()

    def unregister_apps(self):
        from django.contrib import admin
        from django.contrib.auth.models import Group
        for model in [Group]:
            admin.site.unregister(model)
