"""
Job context passed to handlers during execution.
"""
from typing import Any, Dict, Optional
from mojo.helpers import logit


class JobContext:
    """
    Context object passed to job handlers during execution.

    Provides access to job information and control methods without
    exposing the ORM model directly.
    """

    def __init__(self, job_id: str, channel: str, payload: Dict[str, Any],
                 redis_adapter=None, redis_keys=None):
        """
        Initialize job context.

        Args:
            job_id: Unique job identifier
            channel: Channel the job is running on
            payload: Job payload/arguments
            redis_adapter: Redis adapter instance
            redis_keys: JobKeys instance for key generation
        """
        self.job_id = job_id
        self.channel = channel
        self.payload = payload or {}
        self._redis = redis_adapter
        self._keys = redis_keys
        self._metadata = {}
        self._model = None  # Lazy loaded

    def should_cancel(self) -> bool:
        """
        Check if job cancellation has been requested.

        Returns:
            True if job should stop execution
        """
        if not self._redis or not self._keys:
            return False

        try:
            # Check Redis for cancel flag
            cancel_flag = self._redis.hget(
                self._keys.job(self.job_id),
                'cancel_requested'
            )
            return cancel_flag == '1'
        except Exception as e:
            logit.warn(f"Failed to check cancel status for job {self.job_id}: {e}")
            # On error, check database as fallback
            try:
                model = self.get_model()
                return model.cancel_requested if model else False
            except Exception:
                return False

    def set_metadata(self, **kwargs):
        """
        Set custom metadata that will be saved with the job.

        Args:
            **kwargs: Key-value pairs to add to metadata
        """
        self._metadata.update(kwargs)

        # Try to update in Redis immediately for visibility
        if self._redis and self._keys:
            try:
                import json
                current = self._redis.hget(
                    self._keys.job(self.job_id),
                    'metadata'
                )
                if current:
                    current_meta = json.loads(current)
                    current_meta.update(self._metadata)
                else:
                    current_meta = self._metadata

                self._redis.hset(
                    self._keys.job(self.job_id),
                    {'metadata': json.dumps(current_meta)}
                )
            except Exception as e:
                logit.warn(f"Failed to update metadata in Redis for job {self.job_id}: {e}")

    def get_metadata(self) -> Dict[str, Any]:
        """
        Get the current metadata.

        Returns:
            Current metadata dict
        """
        return self._metadata.copy()

    def get_model(self):
        """
        Lazy fetch the Job model from database.

        Returns:
            Job model instance or None if not found
        """
        if self._model is None:
            try:
                from mojo.apps.jobs.models import Job
                self._model = Job.objects.get(id=self.job_id)
            except Job.DoesNotExist:
                logit.error(f"Job {self.job_id} not found in database")
                return None
            except Exception as e:
                logit.error(f"Failed to fetch job {self.job_id} from database: {e}")
                return None

        return self._model

    def log(self, message: str, level: str = 'info'):
        """
        Log a message with job context.

        Args:
            message: Message to log
            level: Log level (info, warn, error, debug)
        """
        prefixed = f"[Job {self.job_id}@{self.channel}] {message}"

        if level == 'info':
            logit.info(prefixed)
        elif level == 'warn':
            logit.warn(prefixed)
        elif level == 'error':
            logit.error(prefixed)
        elif level == 'debug':
            logit.debug(prefixed)
        else:
            logit.info(prefixed)

    def __str__(self):
        return f"JobContext({self.job_id}@{self.channel})"

    def __repr__(self):
        return (f"JobContext(job_id='{self.job_id}', channel='{self.channel}', "
                f"payload={self.payload})")
