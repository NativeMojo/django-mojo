from django.apps import AppConfig


class FilemanConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mojo.apps.fileman'
    verbose_name = 'File Manager'
    
    def ready(self):
        """
        Perform initialization tasks when the app is ready
        """
        # Import signal handlers if any
        # from . import signals
        pass