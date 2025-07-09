from objict import objict
import time


class Task(objict):
    """
    Task model for the Django Mojo task system.

    This class represents a task that can be queued, executed, and tracked
    through various states (pending, running, completed, error, cancelled).
    """

    def __init__(self, id=None, function=None, data=None, channel="default",
                 expires=None, created=None, status="pending", error=None,
                 completed_at=None, **kwargs):
        """
        Initialize a new Task instance.

        Args:
            id (str): Unique identifier for the task
            function (str): Function name to be executed
            data (dict): Data to be passed to the function
            channel (str): Channel name for task routing
            expires (float): Expiration timestamp
            created (float): Creation timestamp
            status (str): Current task status
            error (str): Error message if task failed
            completed_at (float): Completion timestamp
            **kwargs: Additional attributes
        """
        super().__init__(**kwargs)

        self.id = id
        self.function = function
        self.data = data or {}
        self.channel = channel
        self.expires = expires
        self.created = created or time.time()
        self.status = status
        self.error = error
        self.completed_at = completed_at

    def is_expired(self):
        """
        Check if the task has expired.

        Returns:
            bool: True if task has expired, False otherwise
        """
        if self.expires is None:
            return False
        return time.time() > self.expires

    def is_pending(self):
        """Check if task is in pending state."""
        return self.status == "pending"

    def is_running(self):
        """Check if task is in running state."""
        return self.status == "running"

    def is_completed(self):
        """Check if task is in completed state."""
        return self.status == "completed"

    def is_error(self):
        """Check if task is in error state."""
        return self.status == "error"

    def is_cancelled(self):
        """Check if task is in cancelled state."""
        return self.status == "cancelled"

    def mark_as_running(self):
        """Mark task as running."""
        self.status = "running"

    def mark_as_completed(self):
        """Mark task as completed."""
        self.status = "completed"
        self.completed_at = time.time()

    def mark_as_error(self, error_message):
        """Mark task as error with error message."""
        self.status = "error"
        self.error = error_message

    def mark_as_cancelled(self):
        """Mark task as cancelled."""
        self.status = "cancelled"

    def __str__(self):
        """String representation of the task."""
        return f"Task({self.id}, {self.function}, {self.status})"

    def __repr__(self):
        """Detailed string representation of the task."""
        return (f"Task(id='{self.id}', function='{self.function}', "
                f"status='{self.status}', channel='{self.channel}')")
