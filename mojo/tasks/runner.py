from importlib import import_module
from concurrent.futures import ThreadPoolExecutor
from .manager import TaskManager
from mojo.tasks import manager
import os
from mojo.helpers import logit
from mojo.helpers import daemon
from mojo.helpers import paths
import time


class TaskEngine(daemon.Daemon):
    """
    The TaskEngine is responsible for managing and executing tasks across different channels.
    It leverages a thread pool to execute tasks concurrently and uses a task manager to maintain task states.
    """
    def __init__(self, channels=["broadcast"], max_workers=5):
        """
        Initialize the TaskEngine.

        Args:
            channels (list): A list of channel names where tasks are queued.
            max_workers (int, optional): The maximum number of threads available for task execution. Defaults to 5.
        """
        super().__init__("taskit", os.path.join(paths.VAR_ROOT, "taskit"))
        self.manager = manager.TaskManager(channels)
        self.channels = channels
        if "broadcast" not in self.channels:
            self.channels.append("broadcast")
        self.max_workers = max_workers
        self.executor = None
        self.logger = logit.get_logger("taskit", "taskit.log")

    def reset_running_tasks(self):
        """
        Reset tasks that are stuck in a running state by moving them back to the pending state.
        """
        for channel in self.channels:
            for task_id in self.manager.get_running_ids(channel):
                self.logger.info(f"moving task {task_id} from running to pending")
                self.manager.remove_from_running(channel, task_id)
                self.manager.add_to_pending(channel, task_id)

    def queue_pending_tasks(self):
        """
        Queue all the pending tasks for execution.
        """
        for channel in self.channels:
            for task_id in self.manager.get_pending_ids(channel):
                self.queue_task(task_id)

    def handle_message(self, message):
        """
        Handle incoming messages from the channels, decoding task identifiers and queuing them for execution.

        Args:
            message (dict): A dictionary with message data containing task information.
        """
        self.queue_task(message['data'].decode())

    def on_run_task(self, task_id):
        """
        Execute a task based on its identifier by locating the relevant function and executing it.

        Args:
            task_id (str): The identifier of the task to be executed.
        """
        # this is a keep it thread safe with the redis connection
        tman = TaskManager([])
        task_data = tman.get_task(task_id)
        if not task_data:
            # this task has expired or no longer exists
            self.logger.info(f"Task {task_id} has expired or no longer exists")
            tman.remove_from_pending(task_id)
            return
        self.logger.info(f"Executing task {task_id}")
        function_path = task_data.get('function')
        module_name, func_name = function_path.rsplit('.', 1)
        module = import_module(module_name)
        func = getattr(module, func_name)
        self.manager.remove_from_pending(task_id, task_data.channel)
        self.manager.add_to_running(task_id, task_data.channel)

        try:
            task_data.started_at = time.time()
            func(task_data)
            task_data.completed_at = time.time()
            task_data.elapsed_time = task_data.completed_at - task_data.started_at
            tman.save_task(task_data)
            tman.add_to_completed(task_data)
            self.logger.info(f"Task {task_id} completed after {task_data.elapsed_time} seconds")
        except Exception as e:
            self.logger.error(f"Error executing task {task_id}: {str(e)}")
            tman.add_to_errors(task_data, str(e))
        finally:
            tman.remove_from_running(task_id, task_data.channel)

    def queue_task(self, task_id):
        """
        Submit a task for execution in the thread pool.

        Args:
            task_id (str): The identifier of the task to be queued.
        """
        self.logger.info(f"adding task {task_id}")
        self.executor.submit(self.on_run_task, task_id)

    def wait_for_all_tasks_to_complete(self, timeout=30):
        """
        Wait for all tasks submitted to the executor to complete.
        """
        self.executor.shutdown(wait=True, timeout=timeout)
        # Check if there are still active threads
        active_threads = [thread for thread in self.executor._threads if thread.is_alive()]
        if active_threads:
            self.logger.warning(f"shutdown issue, {len(active_threads)} tasks exceeded timeout")
            self.executor.shutdown(wait=False)  # Stop accepting new tasks

    def start_listening(self):
        """
        Listen for messages on the subscribed channels and handle them as they arrive.
        """
        self.logger.info("starting with channels...", self.channels)
        self.reset_running_tasks()
        self.queue_pending_tasks()
        pubsub = self.manager.redis.pubsub()
        channel_keys = {self.manager.get_channel_key(channel): self.handle_message for channel in self.channels}
        pubsub.subscribe(**channel_keys)
        for message in pubsub.listen():
            if not self.running:
                self.logger.info("shutting down, waiting for tasks to complete")
                self.wait_for_all_tasks_to_complete()
                self.logger.info("shutdown complete")
                return
            if message['type'] != 'message':
                continue
            self.handle_message(message)

    def run(self):
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers)
        self.start_listening()


# HELPERS FOR RUNNING VIA CLI
def get_args():
    """
    Setup the argument parser for command-line interface.

    Returns:
        Namespace: Parsed command-line arguments.
    """
    import argparse
    parser = argparse.ArgumentParser(description="TaskEngine Background Service")
    parser.add_argument("--start", action="store_true", help="Start the daemon")
    parser.add_argument("--stop", action="store_true", help="Stop the daemon")
    parser.add_argument("--foreground", "-f", action="store_true", help="Run in foreground mode")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose logging")
    return parser, parser.parse_args()


def main():
    from mojo.helpers.settings import settings
    parser, args = get_args()
    daemon = TaskEngine(settings.TASKIT_CHANNELS)
    if args.start:
        daemon.start()
    elif args.stop:
        daemon.stop()
    elif args.foreground:
        print("Running in foreground mode...")
        daemon.run()
    else:
        parser.print_help()
