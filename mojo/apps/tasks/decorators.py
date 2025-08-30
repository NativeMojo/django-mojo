import functools
from .local_queue import publish_local

def async_task(channel="bg_tasks", expires=1800):
    """
    Decorator to publish a function call to the Redis-based task queue.

    When the decorated function is called, its execution is deferred by publishing
    it as a task to the specified channel.

    Args:
        channel (str): The task channel to publish to.
        expires (int): Task expiration in seconds.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Import tasks locally to prevent circular dependency
            from mojo.apps import tasks

            # If '_from_task_queue' is True, it means the function is being executed
            # by a task runner, so we run the original function.
            if kwargs.pop('_from_task_queue', False):
                return func(*args, **kwargs)

            # Otherwise, we publish the function call as a new task.
            function_string = f"{func.__module__}.{func.__name__}"
            data = {
                'args': list(args),
                'kwargs': {**kwargs, '_from_task_queue': True}  # Add flag for runner
            }
            tasks.publish(channel=channel, function=function_string, data=data, expires=expires)
            return True
        return wrapper
    return decorator

def async_local_task():
    """
    Decorator to publish a function call to the local, in-memory task queue.

    When the decorated function is called, it's added to a thread-safe
    in-memory queue and executed by a background worker thread. This is for
    lightweight, non-persistent tasks.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Publish directly using the imported function, avoiding the circular import.
            publish_local(func, *args, **kwargs)
            return True
        return wrapper
    return decorator
