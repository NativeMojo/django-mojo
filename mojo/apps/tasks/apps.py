from django.apps import AppConfig
import os

class TasksAppConfig(AppConfig):
    name = 'mojo.apps.tasks'

    def ready(self):
        """
        This method is called when the Django application is ready.

        We check for the RUN_MAIN environment variable to ensure this code
        only runs once in the main Django process, not in the reloader process.
        """
        if os.environ.get('RUN_MAIN', None) == 'true':
            from . import local_queue
            local_queue.start_worker_thread()
