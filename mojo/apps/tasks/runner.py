from importlib import import_module
from concurrent.futures import ThreadPoolExecutor
from .manager import TaskManager
from mojo.apps.tasks import manager
import os
from mojo.helpers import logit
from mojo.helpers import daemon
from mojo.helpers import paths
from mojo.apps import metrics
import time
import socket
import threading
import json


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
        self.hostname = socket.gethostname()
        self.manager = manager.TaskManager(channels)
        self.channels = channels
        if "broadcast" not in self.channels:
            self.channels.append("broadcast")

        # Add hostname-specific channel for this runner
        self.runner_channel = f"runner_{self.hostname}"
        if self.runner_channel not in self.channels:
            self.channels.append(self.runner_channel)

        self.max_workers = max_workers
        self.executor = None
        self.logger = logit.get_logger("tasks", "tasks.log")
        self.ping_thread = None
        self.ping_interval = 30  # seconds
        self.started_at = time.time()

    def register_runner(self):
        """
        Register this runner as active in the system.
        """
        runner_data = {
            'hostname': self.hostname,
            'started_at': self.started_at,
            'max_workers': self.max_workers,
            'channels': self.channels,
            'last_ping': time.time(),
            'status': 'active'
        }
        self.manager.redis.hset(
            self.manager.get_runners_key(),
            self.hostname,
            json.dumps(runner_data)
        )
        self.logger.info(f"Registered runner {self.hostname}")

    def unregister_runner(self):
        """
        Unregister this runner from the active runners list.
        """
        self.manager.redis.hdel(self.manager.get_runners_key(), self.hostname)
        self.logger.info(f"Unregistered runner {self.hostname}")

    def update_runner_status(self, status_data=None):
        """
        Update the status of this runner.
        """
        if status_data is None:
            status_data = {}

        runner_data = {
            'hostname': self.hostname,
            'last_ping': time.time(),
            'status': 'active',
            'started_at': self.started_at,
            'max_workers': self.max_workers,
            'channels': self.channels,
            **status_data
        }
        self.manager.redis.hset(
            self.manager.get_runners_key(),
            self.hostname,
            json.dumps(runner_data)
        )


    def ping_runners(self):
        """
        Send ping messages to all active runners to check their status.
        """
        active_runners = self.manager.get_active_runners()
        for hostname in active_runners.keys():
            if hostname != self.hostname:  # Don't ping ourselves
                ping_message = {
                    'type': 'ping',
                    'from': self.hostname,
                    'timestamp': time.time()
                }
                runner_channel = f"runner_{hostname}"
                self.manager.redis.publish(
                    self.manager.get_channel_key(runner_channel),
                    json.dumps(ping_message)
                )

    def handle_ping_request(self, message_data):
        """
        Handle incoming ping requests and send response.
        """
        ping_data = json.loads(message_data)
        response = {
            'type': 'ping_response',
            'from': self.hostname,
            'to': ping_data['from'],
            'timestamp': time.time(),
            'status': self.get_runner_status()
        }

        # Send response to the requesting runner's channel
        requester_channel = f"runner_{ping_data['from']}"
        self.manager.redis.publish(
            self.manager.get_channel_key(requester_channel),
            json.dumps(response)
        )

    def handle_ping_response(self, message_data):
        """
        Handle ping responses from other runners.
        """
        response_data = json.loads(message_data)
        self.logger.info(f"Received ping response from {response_data['from']}")
        # Update the runner's status in our active runners list
        self.manager.redis.hset(
            self.manager.get_runners_key(),
            response_data['from'],
            json.dumps(response_data['status'])
        )

    def get_runner_status(self):
        """
        Get the current status of this runner.

        Returns:
            dict: Status information for this runner.
        """
        active_threads = 0
        if self.executor and hasattr(self.executor, '_threads'):
            active_threads = len([t for t in self.executor._threads if t.is_alive()])

        return {
            'hostname': self.hostname,
            'status': 'active',
            'max_workers': self.max_workers,
            'active_threads': active_threads,
            'channels': self.channels,
            'last_ping': time.time(),
            'uptime': time.time() - getattr(self, 'start_time', time.time())
        }

    def start_ping_thread(self):
        """
        Start the background thread that periodically pings other runners.
        """
        def ping_loop():
            while self.running:
                try:
                    self.ping_runners()
                    self.update_runner_status()
                    time.sleep(self.ping_interval)
                except Exception as e:
                    self.logger.error(f"Error in ping loop: {e}")
                    time.sleep(5)

        self.ping_thread = threading.Thread(target=ping_loop, daemon=True)
        self.ping_thread.start()

    def cleanup_stale_runners(self):
        """
        Remove runners that haven't been seen for a while.
        """
        cutoff_time = time.time() - (self.ping_interval * 3)  # 3 missed pings
        active_runners = self.manager.get_active_runners()

        for hostname, runner_data in active_runners.items():
            last_ping = runner_data.get('last_ping', 0)
            if last_ping < cutoff_time:
                self.logger.info(f"Removing stale runner: {hostname}")
                self.manager.redis.hdel(self.manager.get_runners_key(), hostname)

    def reset_running_tasks(self):
        """
        Reset tasks that are stuck in a running state by moving them back to the pending state.
        """
        for channel in self.channels:
            for task_id in self.manager.get_running_ids(channel):
                self.logger.info(f"moving task {task_id} from running to pending")
                self.manager.remove_from_running(task_id, channel)
                self.manager.add_to_pending(task_id, channel)

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
        message_data = message['data'].decode()

        # Check if this is a ping/status message
        try:
            parsed_message = json.loads(message_data)
            if isinstance(parsed_message, dict) and 'type' in parsed_message:
                if parsed_message['type'] == 'ping':
                    self.handle_ping_request(message_data)
                    return
                elif parsed_message['type'] == 'ping_response':
                    self.handle_ping_response(message_data)
                    return
        except (json.JSONDecodeError, TypeError):
            pass

        # If not a ping message, treat as a task
        self.queue_task(message_data)

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
            metrics.record("tasks_expired", category="tasks")
            # try and remove any pending dead tasks
            self.manager.channels = self.channels
            self.manager.take_out_the_dead(local=True)
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
            task_data._thread_id = threading.current_thread().ident
            tdata = task_data.get("data", {})
            if tdata and "args" in tdata and "kwargs" in tdata:
                args = tdata["args"]
                kwargs = tdata["kwargs"]
                # self.logger.info(f"Executing task {task_id} with args {args} and kwargs {kwargs}")
                func(*args, **kwargs)
            else:
                # self.logger.info(f"Executing task {task_id} with no arguments")
                func(task_data)
            task_data.completed_at = time.time()
            task_data.elapsed_time = task_data.completed_at - task_data.started_at
            if "_thread_id" in task_data:
                del task_data["_thread_id"]
            tman.save_task(task_data)
            tman.add_to_completed(task_data)
            metrics.record("tasks_completed", category="tasks")
            self.logger.info(f"Task {task_id} completed after {task_data.elapsed_time} seconds")
        except Exception as e:
            self.logger.exception(f"Error executing task {task_id}: {str(e)}")
            tman.add_to_errors(task_data, str(e))
            metrics.record("tasks_errors", category="tasks")
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


    def _clear_queued_tasks(self):
        import queue
        q = self.executor._work_queue
        removed = 0
        try:
            while True:
                q.get_nowait()
                removed += 1
        except queue.Empty:
            pass
        return removed

    def _wait_for_active_tasks(self, timeout=5.0):
        """
        Waits up to `timeout` seconds for active executor threads to finish.
        Returns True if all threads completed, False if timeout hit.
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            active = self.manager.get_all_running_ids(local=True)
            if len(active) == 0:
                return True
            time.sleep(0.01)
        return False

    def wait_for_all_tasks_to_complete(self, timeout=5):
        """
        Wait for all tasks submitted to the executor to complete with graceful degradation.
        """
        if not self.executor:
            return

        self.logger.info(f"Initiating graceful shutdown with {timeout}s timeout")
        self.executor.shutdown(wait=False)
        self._clear_queued_tasks()
        result = self._wait_for_active_tasks(timeout)
        if not result:
            self.logger.warning("Timeout reached while waiting for active tasks to complete")
        return result

    def start_listening(self):
        """
        Listen for messages on the subscribed channels and handle them as they arrive.
        """
        self.logger.info("starting with channels...", self.channels)
        self.start_time = time.time()
        self.register_runner()
        self.manager.take_out_the_dead(local=True)
        self.reset_running_tasks()
        self.queue_pending_tasks()
        self.start_ping_thread()

        pubsub = self.manager.redis.pubsub()
        channel_keys = {self.manager.get_channel_key(channel): self.handle_message for channel in self.channels}
        pubsub.subscribe(**channel_keys)

        for message in pubsub.listen():
            if not self.running:
                self.logger.info("shutting down, waiting for tasks to complete")
                self.wait_for_all_tasks_to_complete()
                self.unregister_runner()
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
    parser.add_argument("--status", action="store_true", help="Show status of all runners")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose logging")
    return parser, parser.parse_args()


def main():
    from mojo.helpers.settings import settings
    parser, args = get_args()
    daemon = TaskEngine(settings.TASK_CHANNELS)

    if args.status:
        runners = daemon.manager.get_active_runners()
        if runners:
            print("Active TaskEngine Runners:")
            for hostname, data in runners.items():
                print(f"  {hostname}: {data.get('status', 'unknown')} "
                      f"(last ping: {time.time() - data.get('last_ping', 0):.1f}s ago)")
        else:
            print("No active runners found")
    elif args.start:
        daemon.start()
    elif args.stop:
        daemon.stop()
    elif args.foreground:
        print("Running in foreground mode...")
        daemon.run()
    else:
        parser.print_help()



def kill_thread(thread):
    import ctypes
    if not thread.is_alive():
        return False

    tid = thread.ident
    if tid is None:
        return False

    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_long(tid), ctypes.py_object(SystemExit)
    )
    if res > 1:
        # Undo if multiple threads were affected
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(tid), 0)
        return False
    return True
