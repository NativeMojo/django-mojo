from mojo.helpers.settings import settings
from mojo.helpers import modules
import functools


def get_manager():
    from .manager import TaskManager
    return TaskManager(settings.TASK_CHANNELS)


def publish(channel, function, data, expires=1800):
    man = get_manager()
    return man.publish(function, data, channel=channel, expires=expires)


def async_task(channel="bg_tasks", expires=1800):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Check if this is being called from the task queue
            # If '_from_task_queue' is in kwargs and True, execute directly without publishing
            from_task_queue = kwargs.pop('_from_task_queue', False)

            if from_task_queue:
                # Execute the original function directly when called from task queue
                from mojo.helpers import logit
                logit.get_logger("debug", "debug.log").info(f"executing directly {args} {kwargs}")
                return func(*args, **kwargs)
            else:
                # Generate function string as "<module_name>.<function_name>"
                function_string = f"{func.__module__}.{func.__name__}"

                # Generate data from args and kwargs
                data = {
                    'args': list(args),
                    'kwargs': {**kwargs, '_from_task_queue': True}  # Add flag for task queue
                }

                # Publish the task
                publish(channel=channel, function=function_string, data=data, expires=expires)

                return True
        return wrapper
    return decorator
