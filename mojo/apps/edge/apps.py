from django.apps import AppConfig


class EdgeConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mojo.apps.edge'
    verbose_name = 'Edge (vhosts and serving)'

    def ready(self):
        from mojo.apps import jobs
        from mojo.apps.account.services import system_settings
        from mojo.apps.edge.services import readiness
        from mojo.apps.edge import settings_validators
        from mojo.apps.edge.settings_validators import expected_topology
        from mojo.apps.account.services.admin_settings import Descriptor, register_descriptor

        # Inert everywhere except a runner daemon: registration happens in
        # every Django process, but hooks fire only from JobEngine.start().
        # The handler itself honors EDGE_CONVERGE_ENABLED.
        jobs.register_startup_hook("mojo.apps.edge.asyncjobs.on_engine_start")
        system_settings.register_protected_setting(
            system_settings.EXPECTED_EDGE_TOPOLOGY, expected_topology)
        # Protected, not catalog-writable: this value decides which framework
        # every node in the fleet installs, so the generic Setting write path
        # must refuse it and the dedicated writer must prove a superuser.
        system_settings.register_protected_setting(
            settings_validators.FRAMEWORK_VERSION_KEY,
            settings_validators.framework_pin)
        readiness.register_sections()
        register_descriptor(Descriptor(
            "EDGE_EXPECTED_TOPOLOGY", "Expected fleet", "Edge & Web Apps",
            "Expected edge nodes and pools used by fleet readiness.", "topology",
            resolver="protected", writable="owner", owner="Settings fleet editor",
            change_behavior="typed_owner"))
        register_descriptor(Descriptor(
            "EDGE_FRAMEWORK_VERSION", "Framework version hold", "Edge & Web Apps",
            "Pins the django-mojo version every fleet deploy installs. "
            "Unset installs the newest published release.", "string",
            resolver="protected", writable="owner",
            owner="Settings platform editor", change_behavior="typed_owner",
            constraints="A published django-mojo version, 'hold' to stay on the "
                        "last converged fleet version, or unset for newest",
            unset_meaning="installs the newest published release"))
        register_descriptor(Descriptor(
            "WEBAPP_BASE_URL", "Default WebApp address", "Edge & Web Apps",
            "Default public HTTPS origin used when a WebApp has no group-specific address.",
            "origin", resolver="dynamic", writable="catalog", owner="Settings",
            change_behavior="immediate", constraints="Canonical public HTTPS origin"))
