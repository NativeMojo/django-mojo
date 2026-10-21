from django.apps import AppConfig as BaseAppConfig


class AppConfig(BaseAppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mojo.apps.incident'

    def ready(self):
        from django.db.models.signals import post_migrate
        from mojo.apps.incident.services import mojosec_defaults
        post_migrate.connect(
            mojosec_defaults.after_migrate, sender=self,
            dispatch_uid="incident.mojosec_defaults.v1")
        # Inert everywhere except a runner daemon: registration happens in
        # every Django process, but hooks fire only from JobEngine.start().
        # The handler reconciles this node's kernel firewall on boot, which
        # no broadcast can do for it — see asyncjobs.on_engine_start.
        from mojo.apps import jobs
        from mojo.apps.jobs.capabilities import register
        from mojo.apps.incident.services import firewall_readiness
        register("firewall_reconcile", firewall_readiness.provider,
                 firewall_readiness.on_ready)
        jobs.register_startup_hook(
            "mojo.apps.incident.asyncjobs.on_engine_start")
