default_app_config = 'mojo.apps.tasks.apps.TasksAppConfig'

from .local_queue import publish_local, is_local_worker_running
from .decorators import async_task, async_local_task
from mojo.helpers.settings import settings

def get_manager():
    from .manager import TaskManager
    return TaskManager(settings.TASK_CHANNELS)

def publish(channel, function, data, expires=1800, user=None):
    man = get_manager()
    return man.publish(function, data, channel=channel, expires=expires, user=user)
