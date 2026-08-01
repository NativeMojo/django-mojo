from django.apps import AppConfig as BaseAppConfig

class AppConfig(BaseAppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mojo.apps.account'

    def ready(self):
        from mojo.helpers.settings import settings
        if settings.is_app_installed("django.contrib.admin"):
            self.unregister_apps()
        self._warn_dev_bypass()
        self._warn_origin_bypass()

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

    def _warn_origin_bypass(self):
        """Loud-log when BOUNCER_ALLOW_ANY_ORIGIN is enabled, so the CORS
        bypass is visible in prod startup logs. warn, not error: this is a
        supported configuration, and error would train operators to ignore it.
        Fires once per worker process, which is correct for a flag of this
        kind."""
        from mojo.helpers.settings import settings
        from mojo.helpers import logit
        if settings.get_static("BOUNCER_ALLOW_ANY_ORIGIN", False, kind="bool"):
            logit.warn(
                "account",
                "BOUNCER_ALLOW_ANY_ORIGIN is ENABLED — the public bouncer "
                "endpoints (/api/account/bouncer/{assess,event,message}) now "
                "answer credentialed cross-origin requests from ANY website, "
                "bypassing BOUNCER_ALLOWED_ORIGINS. Any page a visitor loads "
                "can drive the bouncer in that visitor's browser. Only enable "
                "this when the application validates the calling domain "
                "itself.")
