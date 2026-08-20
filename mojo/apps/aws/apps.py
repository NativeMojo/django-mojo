from django.apps import AppConfig as BaseAppConfig


class AppConfig(BaseAppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "mojo.apps.aws"

    def ready(self):
        from mojo.apps.account.services import system_settings
        from mojo.apps.aws.services import aws_setup, capacity, infra_setup
        from mojo.apps.aws.settings_validators import (
            monitoring_topic_arns, stable_outbound_ips)
        from mojo.apps.account.services.admin_settings import Descriptor, register_descriptor

        system_settings.register_protected_setting(
            system_settings.MONITORING_TOPICS, monitoring_topic_arns)
        system_settings.register_protected_setting(
            capacity.STABLE_EGRESS_SETTING, stable_outbound_ips)
        aws_setup.register_sections()
        # Without this line the aws_infrastructure section does not exist at
        # all — writing the module is not what registers it.
        infra_setup.register_sections()
        register_descriptor(Descriptor(
            "EMAIL_DELIVERY_POSTURE", "Email delivery", "Email",
            "System sender and shipped authentication-template readiness.", "object",
            resolver="email_posture", writable="owner", owner="System Setup",
            change_behavior="setup", owner_route="setup?focus=aws_email"))
