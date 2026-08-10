from django.apps import AppConfig


class EdgeConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mojo.apps.edge'
    verbose_name = 'Edge (vhosts and serving)'

    def ready(self):
        from mojo.apps import jobs
        from mojo.apps.account.services import system_settings
        from mojo.apps.edge.services import readiness
        from mojo.apps.edge.settings_validators import expected_topology

        # Inert everywhere except a runner daemon: registration happens in
        # every Django process, but hooks fire only from JobEngine.start().
        # The handler itself honors EDGE_CONVERGE_ENABLED.
        jobs.register_startup_hook("mojo.apps.edge.asyncjobs.on_engine_start")
        system_settings.register_protected_setting(
            system_settings.EXPECTED_EDGE_TOPOLOGY, expected_topology)
        readiness.register_sections()
