from mojo.helpers.settings import settings


def get_manager():
    from .manager import TaskManager
    return TaskManager(settings.TASKIT_CHANNELS)


def publish(channel, function, data, expires=1800):
    man = get_manager()
    return man.publish(function, data, channel=channel, expires=expires)
