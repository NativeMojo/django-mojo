from django.apps import AppConfig
import os

class TasksAppConfig(AppConfig):
    name = 'mojo.apps.tasks'

    def ready(self):
        """
        This method is called when the Django application is ready.
        """
        from . import local_queue
        local_queue.start_worker_thread()
