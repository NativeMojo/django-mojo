import queue
import threading
from django.db import close_old_connections, models
from mojo.helpers import logit

# 1. A thread-safe, in-memory queue
local_task_queue = queue.Queue()

logger = logit.get_logger("tasks_local", "tasks_local.log")

# 2. The public function to add tasks to the queue
def publish_local(function, *args, **kwargs):
    """
    Publishes a function and its arguments to the local, in-memory task queue.

    This function is thread-safe.

    Raises:
        TypeError: If any argument is a Django model instance.
    """
    # --- Safety Check --- #
    for arg in args:
        if isinstance(arg, models.Model):
            raise TypeError(
                f"Cannot pass Django model instance {arg.__class__.__name__} to publish_local. "
                f"Pass its primary key (e.g., {arg.pk}) instead."
            )
    for key, value in kwargs.items():
        if isinstance(value, models.Model):
            raise TypeError(
                f"Cannot pass Django model instance {value.__class__.__name__} (for kwarg '{key}') to publish_local. "
                f"Pass its primary key instead."
            )
    # --- End Safety Check ---

    local_task_queue.put((function, args, kwargs))

# 3. The background worker that executes tasks
def _worker():
    """
    A long-running worker that pulls tasks from the queue and executes them.

    This function is intended to be run in a single background thread.
    """
    while True:
        function, args, kwargs = local_task_queue.get()
        try:
            function(*args, **kwargs)
        except Exception as e:
            logger.exception(f"Error executing task {function.__name__}")
        finally:
            # CRITICAL: Close the database connection for this thread
            # to prevent stale connection errors.
            close_old_connections()
            # Mark the task as done for queue management
            local_task_queue.task_done()

# 4. The function to start the worker thread
_worker_thread = None

def start_worker_thread():
    """
    Starts the background worker thread.
    This should only be called once when the Django app starts.
    """
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return

    _worker_thread = threading.Thread(target=_worker, daemon=True)
    _worker_thread.start()
    print("[local_queue] Background worker thread started.")

def is_local_worker_running():
    """Check if the local task worker thread is running."""
    return _worker_thread is not None and _worker_thread.is_alive()
