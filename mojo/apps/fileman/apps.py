from django.apps import AppConfig


class FilemanConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mojo.apps.fileman'
    verbose_name = 'File Manager'

    def ready(self):
        """
        Perform initialization tasks when the app is ready
        """
        # Write-time validation for the admin-configurable rendition options
        # (global and per group), on REST and Setting.set alike. The schema
        # lives in renderer/config.py; the renderer modules are imported
        # lazily inside the validators, after every app is loaded.
        from mojo.apps.account.models import Setting
        from mojo.apps.fileman.renderer import config
        Setting.register_validator(
            config.CATEGORY_KEYS["image"], config.validate_image, global_only=False)
        Setting.register_validator(
            config.CATEGORY_KEYS["video"], config.validate_video, global_only=False)
        Setting.register_validator(
            config.CATEGORY_KEYS["document"], config.validate_document, global_only=False)
        Setting.register_validator(
            config.VIDEO_ENGINE_KEY, config.validate_engine, global_only=True)
        # The platform cap on a CDN signed link's lifetime (maestro #8250).
        from mojo.apps.fileman.services import cdn
        Setting.register_validator(
            cdn.MAX_EXPIRES_IN_KEY, cdn.validate_max_expires_in, global_only=True)
        # One distribution serves every group on a bucket, so the CORS
        # response headers policy is global only (maestro #8286).
        Setting.register_validator(
            cdn.RESPONSE_HEADERS_POLICY_KEY, cdn.validate_response_headers_policy,
            global_only=True)
