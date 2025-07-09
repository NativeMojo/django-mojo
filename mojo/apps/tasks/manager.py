from posix import lockf
from venv import create
from mojo.helpers import redis
from objict import objict
import uuid
import time


class TaskManager:
    def __init__(self, channels, prefix="mojo:tasks"):
        """
        Initialize the TaskManager with Redis connection, channels, and a key prefix.

        :param channels: List of channels for task management.
        :param prefix: Prefix for Redis keys. Default is "taskit".
        """
        self.redis = redis.get_connection()
        self.channels = channels
        self.prefix = prefix

    def take_out_the_dead(self, local=False):
        # Get channels from Redis instead of using self.channels
        channels = self.channels if local else self.get_all_channels()
        for channel in channels:
            # this will remove all expired tasks by default
            self.get_pending(channel)
            self.get_running(channel)

    def get_completed_key(self, channel):
        """
        Get the Redis key for completed tasks in a channel.

        :param channel: Channel name.
        :return: Redis key for completed tasks.
        """
        return f"{self.prefix}:d:{channel}"

    def get_pending_key(self, channel):
        """
        Get the Redis key for pending tasks in a channel.

        :param channel: Channel name.
        :return: Redis key for pending tasks.
        """
        return f"{self.prefix}:p:{channel}"

    def get_global_pending_key(self):
        """
        Get the Redis key for pending tasks in a channel.

        :param channel: Channel name.
        :return: Redis key for pending tasks.
        """
        return f"{self.prefix}:pending"

    def get_channels_key(self):
        """
        Get the Redis key for tracking all channels.

        :return: Redis key for channels set.
        """
        return f"{self.prefix}:channels"

    def get_error_key(self, channel):
        """
        Get the Redis key for tasks with errors in a channel.

        :param channel: Channel name.
        :return: Redis key for tasks with errors.
        """
        return f"{self.prefix}:e:{channel}"

    def get_running_key(self, channel):
        """
        Get the Redis key for running tasks in a channel.

        :param channel: Channel name.
        :return: Redis key for running tasks.
        """
        return f"{self.prefix}:r:{channel}"

    def get_task_key(self, task_id):
        """
        Get the Redis key for a specific task.

        :param task_id: Task ID.
        :return: Redis key for the task.
        """
        return f"{self.prefix}:t:{task_id}"

    def get_channel_key(self, channel):
        """
        Get the Redis key for a channel.

        :param channel: Channel name.
        :return: Redis key for the channel.
        """
        return f"{self.prefix}:c:{channel}"

    def get_runners_key(self):
        return f"{self.prefix}:runners"

    def add_channel(self, channel):
        """
        Add a channel to the global channels set.

        :param channel: Channel name.
        """
        self.redis.sadd(self.get_channels_key(), channel)

    def remove_channel(self, channel):
        """
        Remove a channel from the global channels set.

        :param channel: Channel name.
        """
        self.redis.srem(self.get_channels_key(), channel)

    def remove_all_channels(self):
        """
        Remove all channels from the global channels set.
        """
        self.redis.delete(self.get_channels_key())

    def get_all_channels(self):
        """
        Get all channels from the global channels set.

        :return: List of channel names.
        """
        channels = self.redis.smembers(self.get_channels_key())
        return [channel.decode('utf-8') for channel in channels]

    def get_task(self, task_id):
        """
        Retrieve a task from Redis using its task ID.

        :param task_id: Task ID.
        :return: Task data as an objict, or None if not found.
        """
        task_data_raw = self.redis.get(self.get_task_key(task_id))
        if not task_data_raw:
            return None
        return objict.from_json(task_data_raw, ignore_errors=True)

    def save_task(self, task_data, expires=1800):
        """
        Save a task to Redis with an expiration time.

        :param task_data: Task data as an objict.
        :param expires: Expiration time in seconds. Default is 1800.
        """
        self.redis.set(self.get_task_key(task_data.id), task_data.to_json(as_string=True), ex=expires)

    def get_key_expiration(self, task_id):
        """
        Get the expiration time of a task in Redis.

        :param task_id: Task ID.
        :return: Time to live for the task key in seconds, or None if the key does not exist.
        """
        ttl = self.redis.ttl(self.get_task_key(task_id))
        return ttl if ttl != -2 else None

    def add_to_pending(self, task_id, channel="default"):
        """
        Add a task ID to the pending set in Redis for a channel.

        :param task_id: Task ID.
        :param channel: Channel name. Default is "default".
        :return: True if operation is successful.
        """
        # Add channel to global channels set
        self.add_channel(channel)
        self.redis.sadd(self.get_pending_key(channel), task_id)
        return True

    def add_to_running(self, task_id, channel="default"):
        """
        Add a task ID to the running set in Redis for a channel.

        :param task_id: Task ID.
        :param channel: Channel name. Default is "default".
        :return: True if operation is successful.
        """
        self.redis.sadd(self.get_running_key(channel), task_id)
        return True

    def add_to_errors(self, task_data, error_message):
        """
        Add a task to the error set in Redis with an error message.

        :param task_data: Task data as an objict.
        :param error_message: Error message string.
        :return: True if operation is successful.
        """
        task_data.status = "error"
        task_data.error = error_message
        self.save_task(task_data, expires=86400)
        self.redis.sadd(self.get_error_key(task_data.channel), task_data.id)
        return True

    def add_to_completed(self, task_data):
        """
        Add a task to the completed set in Redis.

        :param task_data: Task data as an objict.
        :return: True if operation is successful.
        """
        task_data.status = "completed"
        # save completed tasks for 24 hours
        self.save_task(task_data, expires=86400)
        self.redis.sadd(self.get_completed_key(task_data.channel), task_data.id)
        return True

    def remove_from_running(self, task_id, channel="default"):
        """
        Remove a task ID from the running set in Redis for a channel.

        :param task_id: Task ID.
        :param channel: Channel name. Default is "default".
        :return: True if operation is successful.
        """
        self.redis.srem(self.get_running_key(channel), task_id)
        return True

    def remove_from_pending(self, task_id, channel="default"):
        """
        Remove a task ID from the pending set in Redis for a channel.

        :param task_id: Task ID.
        :param channel: Channel name. Default is "default".
        :return: True if operation is successful.
        """
        self.redis.srem(self.get_pending_key(channel), task_id)
        return True

    def remove_from_completed(self, task_id, channel="default"):
        """
        Remove a task ID from the completed set in Redis for a channel.

        :param task_id: Task ID.
        :param channel: Channel name. Default is "default".
        :return: True if operation is successful.
        """
        self.redis.srem(self.get_completed_key(channel), task_id)
        return True

    def remove_from_errors(self, task_id, channel="default"):
        """
        Remove a task ID from the error set in Redis for a channel.

        :param task_id: Task ID.
        :param channel: Channel name. Default is "default".
        :return: True if operation is successful.
        """
        self.redis.srem(self.get_error_key(channel), task_id)
        return True

    def remove_task(self, task_id):
        """
        Remove a task from all sets and delete it from Redis.

        :param task_id: Task ID.
        :return: True if task was found and removed, otherwise False.
        """
        task_data = self.get_task(task_id)
        if task_data:
            self.remove_from_running(task_data.id, task_data.channel)
            self.remove_from_pending(task_data.id, task_data.channel)
            self.redis.delete(self.get_task_key(task_id))
            return True
        return False

    def cancel_task(self, task_id):
        """
        Cancel a task by removing it from running and pending sets and deleting it.

        :param task_id: Task ID.
        """
        task_data_raw = self.redis.get(self.get_task_key(task_id))
        task_data = objict.from_json(task_data_raw, ignore_errors=True)
        if task_data:
            task_data.status = "cancelled"
            self.remove_from_pending(task_data.id, task_data.channel)
            self.save_task(task_data)
            return True
        return False

    def get_running_ids(self, channel="default"):
        """
        Get all running task IDs from Redis for a channel.

        :param channel: Channel name. Default is "default".
        :return: List of running task IDs.
        """
        return [task_id.decode('utf-8') for task_id in self.redis.smembers(self.get_running_key(channel))]

    def get_pending_ids(self, channel="default"):
        """
        Get all pending task IDs from Redis for a channel.

        :param channel: Channel name. Default is "default".
        :return: List of pending task IDs.
        """
        return [task_id.decode('utf-8') for task_id in self.redis.smembers(self.get_pending_key(channel))]

    def get_completed_ids(self, channel="default"):
        """
        Get all pending task IDs from Redis for a channel.

        :param channel: Channel name. Default is "default".
        :return: List of pending task IDs.
        """
        return [task_id.decode('utf-8') for task_id in self.redis.smembers(self.get_completed_key(channel))]

    def get_error_ids(self, channel="default"):
        """
        Get all error task IDs from Redis for a channel.

        :param channel: Channel name. Default is "default".
        :return: List of error task IDs.
        """
        return [task_id.decode('utf-8') for task_id in self.redis.smembers(self.get_error_key(channel))]

    def get_pending(self, channel="default"):
        """
        Get all pending tasks as objicts for a channel.

        :param channel: Channel name. Default is "default".
        :return: List of pending task objicts.
        """
        pending_tasks = []
        for task_id in self.get_pending_ids(channel):
            task = self.get_task(task_id)
            if task:
                pending_tasks.append(task)
            else:
                self.remove_from_pending(task_id, channel)
        return pending_tasks

    def get_running(self, channel="default"):
        """
        Get all running tasks as objicts for a channel.

        :param channel: Channel name. Default is "default".
        :return: List of running task objicts.
        """
        running_tasks = []
        for task_id in self.get_running_ids(channel):
            task = self.get_task(task_id)
            if task:
                running_tasks.append(task)
            else:
                self.remove_from_running(task_id, channel)
        return running_tasks

    def get_completed(self, channel="default"):
        """
        Get all completed tasks as objicts for a channel.

        :param channel: Channel name. Default is "default".
        :return: List of completed task objicts.
        """
        completed_tasks = []
        for task_id in self.get_completed_ids(channel):
            task = self.get_task(task_id)
            if task:
                completed_tasks.append(task)
            else:
                self.remove_from_completed(task_id, channel)
        return completed_tasks

    def get_errors(self, channel="default"):
        """
        Get all error tasks as objicts for a channel.

        :param channel: Channel name. Default is "default".
        :return: List of error task objicts.
        """
        error_tasks = []
        for task_id in self.get_error_ids(channel):
            task = self.get_task(task_id)
            if task:
                error_tasks.append(task)
            else:
                self.remove_from_errors(task_id, channel)
        return error_tasks

    def publish(self, function, data, channel="default", expires=1800):
        """
        Publish a new task to a channel, save it, and add to pending tasks.

        :param function: Function associated with the task.
        :param data: Data to be processed by the task.
        :param channel: Channel name. Default is "default".
        :param expires: Expiration time in seconds. Default is 1800.
        :return: Task ID of the published task.
        """
        from mojo.apps import metrics

        task_data = objict(channel=channel, function=function, data=objict.from_dict(data))
        task_data.id = str(uuid.uuid4()).replace('-', '')
        task_data.created = time.time()
        task_data.expires = time.time() + expires
        task_data.status = "pending"
        self.add_to_pending(task_data.id, channel)
        self.save_task(task_data, expires)
        self.redis.publish(self.get_channel_key(channel), task_data.id)
        metrics.record("tasks_pub", category="tasks")
        metrics.record(f"tasks_pub_{channel}", category=f"tasks_{channel}")
        return task_data.id

    def get_all_pending_ids(self, local=False):
        """
        Get all pending task IDs across all channels.

        :return: List of all pending task IDs.
        """
        pending_ids = []
        channels = self.channels if local else self.get_all_channels()
        for channel in channels:
            pending_ids.extend(self.get_pending_ids(channel))
        return pending_ids

    def get_all_running_ids(self, local=False):
        """
        Get all running task IDs across all channels.

        :return: List of all running task IDs.
        """
        running_ids = []
        channels = self.channels if local else self.get_all_channels()
        for channel in channels:
            running_ids.extend(self.get_running_ids(channel))
        return running_ids

    def get_all_completed_ids(self, local=False):
        """
        Get all completed task IDs across all channels.

        :return: List of all completed task IDs.
        """
        completed_ids = []
        channels = self.channels if local else self.get_all_channels()
        for channel in channels:
            completed_ids.extend(self.get_completed_ids(channel))
        return completed_ids

    def get_all_error_ids(self, local=False):
        """
        Get all error task IDs across all channels.

        :return: List of all error task IDs.
        """
        error_ids = []
        channels = self.channels if local else self.get_all_channels()
        for channel in channels:
            error_ids.extend(self.get_error_ids(channel))
        return error_ids

    def get_all_pending(self, include_data=False, local=False):
        """
        Get all pending tasks as objects.

        :return: List of pending task objects.
        """
        pending_tasks = []
        for task_id in self.get_all_pending_ids(local=local):
            task = self.get_task(task_id)
            if task:
                if not include_data:
                    del task.data
                pending_tasks.append(task)
            else:
                self.remove_from_pending(task_id)
        return pending_tasks

    def get_all_running(self, include_data=False, local=False):
        """
        Get all running tasks as objects.

        :return: List of running task objects.
        """
        running_tasks = []
        for task_id in self.get_all_running_ids(local=local):
            task = self.get_task(task_id)
            if task:
                if not include_data:
                    del task.data
                running_tasks.append(task)
            else:
                self.remove_from_running(task_id)
        return running_tasks

    def get_all_completed(self, include_data=False, local=False):
        """
        Get all completed tasks as objects.

        :return: List of completed task objects.
        """
        completed_tasks = []
        for task_id in self.get_all_completed_ids(local=local):
            task = self.get_task(task_id)
            if task:
                if not include_data:
                    del task.data
                completed_tasks.append(task)
            else:
                self.remove_from_completed(task_id)
        # Sort tasks by the created timestamp in descending order
        completed_tasks.sort(key=lambda x: x.created, reverse=True)
        return completed_tasks

    def get_all_errors(self, local=False):
        """
        Get all error tasks as objects.

        :return: List of error task objects.
        """
        error_tasks = []
        for task_id in self.get_all_error_ids(local=local):
            task = self.get_task(task_id)
            if task:
                error_tasks.append(task)
            else:
                self.remove_from_errors(task_id)
        # Sort tasks by the created timestamp in descending order
        error_tasks.sort(key=lambda x: x.created, reverse=True)
        return error_tasks

    def get_channel_status(self, channel):
        status = objict()
        status.pending = len(self.get_pending_ids(channel))
        status.running = len(self.get_running_ids(channel))
        status.completed = len(self.get_completed_ids(channel))
        status.errors = len(self.get_error_ids(channel))
        return status

    def get_status(self, simple=False, local=False):
            """
            Get the status of tasks across all channels, including pending and running tasks.

            :return: Status object containing counts of pending and running tasks per channel.
            """
            status = objict(pending=0, running=0, completed=0, errors=0)
            if not simple:
                status.channels = objict()
            channels = self.channels if local else self.get_all_channels()
            # Use channels from Redis instead of self.channels
            for channel in channels:
                cstatus = self.get_channel_status(channel)
                status.pending += cstatus.pending
                status.running += cstatus.running
                status.completed += cstatus.completed
                status.errors += cstatus.errors
                if not simple:
                    status.channels[channel] = cstatus
            status["runners"] = self.get_active_runners()
            return status

    def get_all_runners(self):
        """
        Get all runners.

        Returns:
            dict: Dictionary of runners with their status.
        """
        runners = {}
        raw_runners = self.redis.hgetall(self.get_runners_key())
        for hostname, data in raw_runners.items():
            try:
                runner = objict.from_json(data.decode())
                runner["ping_age"] = time.time() - runner["last_ping"]
                if runner["ping_age"] > 30:
                    runner["status"] = "timeout"
                runners[hostname.decode()] = runner
            except Exception:
                continue
        return runners

    def get_active_runners(self):
        """
        Get all active runners.

        Returns:
            dict: Dictionary of active runners with their status.
        """
        return self.get_all_runners()

    def remove_runner(self, hostname):
        """
        Remove a runner from the list of active runners.

        Args:
            hostname (str): The hostname of the runner to remove.
        """
        self.redis.hdel(self.get_runners_key(), hostname)

    def clear_runners(self, ping_age=30):
        raw_runners = self.get_all_runners()
        for runner in raw_runners.values():
            try:
                runner["ping_age"] = time.time() - runner["last_ping"]
                if runner["ping_age"] > ping_age:
                    self.remove_runner(runner["hostname"])
            except Exception:
                continue

    def clear_running_tasks(self):
        """
        Reset tasks that are stuck in a running state by moving them back to the pending state.
        """
        for channel in self.channels:
            for task_id in self.get_running_ids(channel):
                # self.logger.info(f"moving task {task_id} from running to pending")
                self.remove_from_running(task_id, channel)
                self.add_to_pending(task_id, channel)

    def clear_pending_tasks(self):
        """
        Reset tasks that are stuck in a pending state by moving them back to the pending state.
        """
        for channel in self.channels:
            for task_id in self.get_pending_ids(channel):
                # self.logger.info(f"moving task {task_id} from running to pending")
                self.remove_from_pending(task_id, channel)

    def clear_channel(self, channel):
        for task_id in self.get_running_ids(channel):
            self.remove_from_running(task_id, channel)
        for task_id in self.get_pending_ids(channel):
            self.remove_from_pending(task_id, channel)
        for task_id in self.get_completed_ids(channel):
            self.remove_from_completed(task_id, channel)
        for task_id in self.get_error_ids(channel):
            self.remove_from_errors(task_id, channel)

    def clear_local_queues(self):
        """
        Reset tasks that are stuck in a pending state by moving them back to the pending state.
        """
        for channel in self.channels:
            self.clear_channel(channel)
