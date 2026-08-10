from django.apps import AppConfig as BaseAppConfig


class AppConfig(BaseAppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "mojo.apps.aws"

    def ready(self):
        from mojo.apps.account.services import system_settings
        from mojo.apps.aws.services import aws_setup
        from mojo.apps.aws.settings_validators import monitoring_topic_arns

        system_settings.register_protected_setting(
            system_settings.MONITORING_TOPICS, monitoring_topic_arns)
        aws_setup.register_sections()
