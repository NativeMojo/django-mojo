from django.apps import AppConfig as BaseAppConfig

class AppConfig(BaseAppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mojo.apps.account'

    def ready(self):
        from mojo.helpers.settings import settings
        if settings.is_app_installed("django.contrib.admin"):
            self.unregister_apps()
        self._warn_dev_bypass()

    def unregister_apps(self):
        from django.contrib import admin
        from django.contrib.auth.models import Group
        for model in [Group]:
            admin.site.unregister(model)

    def _warn_dev_bypass(self):
        """Loud-log when AUTH_PHONE_VERIFY_DEV_BYPASS_CODE is set, so
        operators see the misconfiguration in prod startup logs if they
        forget to unset it."""
        from mojo.helpers.settings import settings
        from mojo.helpers import logit
        bypass = settings.get_static("AUTH_PHONE_VERIFY_DEV_BYPASS_CODE", "")
        if bypass:
            logit.warn(
                "account",
                "AUTH_PHONE_VERIFY_DEV_BYPASS_CODE is SET — phone-register "
                "verify endpoint will accept a fixed bypass code. This must "
                "ONLY be set in dev/test environments.")
