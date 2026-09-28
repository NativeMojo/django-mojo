from django.apps import AppConfig as BaseAppConfig


class AppConfig(BaseAppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mojo.apps.chat'

    def ready(self):
        # Write-time validation for the live moderation settings (global and
        # per group), on REST and Setting.set alike.
        from mojo.apps.account.models import Setting
        from mojo.apps.chat import rules
        Setting.register_validator(
            rules.HIDE_LEVEL_KEY, rules.validate_hide_level, global_only=False)
        Setting.register_validator(
            rules.ALLOWED_DOMAINS_KEY, rules.validate_allowed_domains, global_only=False)
