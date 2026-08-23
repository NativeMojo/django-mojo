from django.apps import AppConfig as BaseAppConfig


class AppConfig(BaseAppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mojo.apps.incident'

    def ready(self):
        # Inert everywhere except a runner daemon: registration happens in
        # every Django process, but hooks fire only from JobEngine.start().
        # The handler reconciles this node's kernel firewall on boot, which
        # no broadcast can do for it — see asyncjobs.on_engine_start.
        from mojo.apps import jobs
        jobs.register_startup_hook(
            "mojo.apps.incident.asyncjobs.on_engine_start")
