from django.apps import AppConfig


class EdgeConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mojo.apps.edge'
    verbose_name = 'Edge (vhosts and serving)'
