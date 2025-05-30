from venv import create
from mojo.helpers import redis
from objict import objict
import uuid
import time


class TaskManager:
    def __init__(self, channels, prefix="taskit"):
        """
        Initialize the TaskManager with Redis connection, channels, and a key prefix.

        :param channels: List of channels for task management.
        :param prefix: Prefix for Redis keys. Default is "taskit".
        """
        self.redis = redis.get_connection()
        self.channels = channels
        self.prefix = prefix

    def take_out_the_dead(self):
        for channel in self.channels:
            # this will remove all expired tasks by default
            self.get_pending()
            self.get_running()

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

    def get_task(self, task_id):
        """
        Retrieve a task from Redis using its task ID.

        :param task_id: Task ID.
        :return: Task data as an objict, or None if not found.
        """
        task_data_raw = self.redis.get(self.get_task_key(task_id))
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
            self.redis.srem(self.get_running_key(task_data.channel), task_data.id)
            self.redis.srem(self.get_pending_key(task_data.channel), task_data.id)
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
            self.redis.srem(self.get_running_key(task_data.channel), task_data.id)
            self.redis.srem(self.get_pending_key(task_data.channel), task_data.id)
            self.redis.delete(self.get_task_key(task_id))

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
        task_data = objict(channel=channel, function=function, data=objict.from_dict(data))
        task_data.id = str(uuid.uuid4()).replace('-', '')
        task_data.created = time.time()
        task_data.expires = time.time() + expires
        task_data.status = "pending"
        self.add_to_pending(task_data.id, channel)
        self.save_task(task_data, expires)
        self.redis.publish(self.get_channel_key(channel), task_data.id)
        # metrics.record("taskit:p:global", category="taskit")
        # metrics.record(f"taskit:p:{channel}", category="taskit_channels")
        return task_data.id

    def get_all_pending_ids(self):
        """
        Get all pending task IDs across all channels.

        :return: List of all pending task IDs.
        """
        pending_ids = []
        for channel in self.channels:
            pending_ids.extend(self.get_pending_ids(channel))
        return pending_ids

    def get_all_running_ids(self):
        """
        Get all running task IDs across all channels.

        :return: List of all running task IDs.
        """
        running_ids = []
        for channel in self.channels:
            running_ids.extend(self.get_running_ids(channel))
        return running_ids

    def get_all_completed_ids(self):
        """
        Get all completed task IDs across all channels.

        :return: List of all completed task IDs.
        """
        completed_ids = []
        for channel in self.channels:
            completed_ids.extend(self.get_completed_ids(channel))
        return completed_ids

    def get_all_error_ids(self):
        """
        Get all error task IDs across all channels.

        :return: List of all error task IDs.
        """
        error_ids = []
        for channel in self.channels:
            error_ids.extend(self.get_error_ids(channel))
        return error_ids

    def get_all_pending(self, include_data=False):
        """
        Get all pending tasks as objects.

        :return: List of pending task objects.
        """
        pending_tasks = []
        for task_id in self.get_all_pending_ids():
            task = self.get_task(task_id)
            if task:
                if not include_data:
                    del task.data
                pending_tasks.append(task)
            else:
                self.remove_from_pending(task_id)
        return pending_tasks

    def get_all_running(self, include_data=False):
        """
        Get all running tasks as objects.

        :return: List of running task objects.
        """
        running_tasks = []
        for task_id in self.get_all_running_ids():
            task = self.get_task(task_id)
            if task:
                if not include_data:
                    del task.data
                running_tasks.append(task)
            else:
                self.remove_from_running(task_id)
        return running_tasks

    def get_all_completed(self, include_data=False):
        """
        Get all completed tasks as objects.

        :return: List of completed task objects.
        """
        completed_tasks = []
        for task_id in self.get_all_completed_ids():
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

    def get_all_errors(self):
        """
        Get all error tasks as objects.

        :return: List of error task objects.
        """
        error_tasks = []
        for task_id in self.get_all_error_ids():
            task = self.get_task(task_id)
            if task:
                error_tasks.append(task)
            else:
                self.remove_from_errors(task_id)
        # Sort tasks by the created timestamp in descending order
        error_tasks.sort(key=lambda x: x.created, reverse=True)
        return error_tasks

    def get_status(self, simple=False):
            """
            Get the status of tasks across all channels, including pending and running tasks.

            :return: Status object containing counts of pending and running tasks per channel.
            """
            status = objict(pending=0, running=0, completed=0, errors=0, channels=objict())
            for channel in self.channels:
                pending = self.get_pending_ids(channel)
                running = self.get_running_ids(channel)
                completed = self.get_completed_ids(channel)
                errors = self.get_error_ids(channel)
                status.pending += len(pending)
                status.running += len(running)
                status.completed += len(completed)
                status.errors += len(errors)
                if not simple:
                    cstats = objict()
                    cstats.pending = len(pending)
                    cstats.running = len(running)
                    cstats.completed = len(completed)
                    cstats.errors = len(errors)
                    status.channels[channel] = cstats
            return status
