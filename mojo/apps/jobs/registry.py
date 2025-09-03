"""
Job registry for function registration and lookup.
"""
import functools
import inspect
from typing import Any, Callable, Dict, Optional, Union
from threading import RLock

from mojo.helpers import logit


class JobRegistry:
    """
    Thread-safe registry for job functions.

    Jobs are registered by name and can be looked up for execution.
    """

    def __init__(self):
        """Initialize the registry."""
        self._functions: Dict[str, Callable] = {}
        self._metadata: Dict[str, Dict[str, Any]] = {}
        self._lock = RLock()

    def register(self, func: Callable, name: Optional[str] = None,
                 channel: str = "default", broadcast: bool = False,
                 **defaults) -> Callable:
        """
        Register a function as a job handler.

        Args:
            func: The function to register
            name: Registration name (defaults to func.__name__)
            channel: Default channel for this job
            broadcast: Whether this is a broadcast job by default
            **defaults: Default options for job execution

        Returns:
            The original function (for decorator use)
        """
        if not callable(func):
            raise ValueError(f"Cannot register non-callable: {func}")

        # Generate registration name
        if name is None:
            module = func.__module__
            if module and module != '__main__':
                name = f"{module}.{func.__name__}"
            else:
                name = func.__name__

        with self._lock:
            if name in self._functions:
                logit.warn(f"Overwriting existing job registration: {name}")

            self._functions[name] = func
            self._metadata[name] = {
                'channel': channel,
                'broadcast': broadcast,
                'defaults': defaults,
                'module': func.__module__,
                'qualname': func.__qualname__,
                'doc': inspect.getdoc(func)
            }

            logit.info(f"Registered job: {name} -> {func.__module__}.{func.__qualname__}")

        # Store registration name on function for reference
        func._job_name = name
        func._job_metadata = self._metadata[name]

        return func

    def unregister(self, name: str) -> bool:
        """
        Unregister a job function.

        Args:
            name: Registration name

        Returns:
            True if unregistered, False if not found
        """
        with self._lock:
            if name in self._functions:
                del self._functions[name]
                del self._metadata[name]
                logit.info(f"Unregistered job: {name}")
                return True
            return False

    def get(self, name: str) -> Optional[Callable]:
        """
        Get a registered function by name.

        Args:
            name: Registration name

        Returns:
            The registered function or None
        """
        with self._lock:
            return self._functions.get(name)

    def get_metadata(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Get metadata for a registered function.

        Args:
            name: Registration name

        Returns:
            Metadata dict or None
        """
        with self._lock:
            return self._metadata.get(name)

    def list(self) -> Dict[str, Dict[str, Any]]:
        """
        List all registered jobs with metadata.

        Returns:
            Dict of name -> metadata
        """
        with self._lock:
            return {
                name: {
                    'channel': meta['channel'],
                    'broadcast': meta['broadcast'],
                    'module': meta['module'],
                    'qualname': meta['qualname'],
                    'doc': meta['doc']
                }
                for name, meta in self._metadata.items()
            }

    def clear(self):
        """Clear all registrations (useful for testing)."""
        with self._lock:
            self._functions.clear()
            self._metadata.clear()
            logit.info("Cleared job registry")


class LocalJobRegistry(JobRegistry):
    """
    Registry for local (in-process) jobs.

    Inherits from JobRegistry but maintains separate registration.
    """

    def register(self, func: Callable, name: Optional[str] = None,
                 **defaults) -> Callable:
        """
        Register a function as a local job handler.

        Args:
            func: The function to register
            name: Registration name (defaults to func.__name__)
            **defaults: Default options for job execution

        Returns:
            The original function (for decorator use)
        """
        # Local jobs don't have channel or broadcast
        return super().register(
            func, name,
            channel='local',
            broadcast=False,
            **defaults
        )


# Global registry instances
_job_registry = JobRegistry()
_local_registry = LocalJobRegistry()


def async_job(channel: str = "default", broadcast: bool = False,
              name: Optional[str] = None, **defaults):
    """
    Decorator to register a function as an async job.

    Args:
        channel: Default channel for this job
        broadcast: Whether this is a broadcast job
        name: Custom registration name (defaults to qualified function name)
        **defaults: Default job options (max_retries, backoff_base, etc.)

    Example:
        @async_job(channel="emails", max_retries=5)
        def send_email(ctx, to, subject, body):
            # Job implementation
            pass

    The decorated function should accept a JobContext as first argument.
    """
    def decorator(func: Callable) -> Callable:
        # Register the function
        _job_registry.register(func, name, channel, broadcast, **defaults)

        # Create wrapper that can be called directly (bypasses job system)
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Direct call - create a mock context if needed
            if args and hasattr(args[0], 'job_id'):
                # First arg looks like a context
                return func(*args, **kwargs)
            else:
                # No context provided, create a minimal one
                from mojo.apps.jobs.context import JobContext
                ctx = JobContext(
                    job_id='direct-call',
                    channel=channel,
                    payload={'args': args, 'kwargs': kwargs}
                )
                return func(ctx, *args, **kwargs)

        # Attach publish helper
        def publish(payload: Dict[str, Any] = None, **options):
            """Publish this job with given payload and options."""
            from mojo.apps.jobs import publish as _publish

            # Merge defaults with provided options
            job_options = {**defaults, **options}
            if 'channel' not in job_options:
                job_options['channel'] = channel
            if 'broadcast' not in job_options:
                job_options['broadcast'] = broadcast

            return _publish(
                func=func._job_name,
                payload=payload or {},
                **job_options
            )

        wrapper.publish = publish
        wrapper._job_name = func._job_name
        wrapper._job_metadata = func._job_metadata

        return wrapper

    return decorator


def local_async_job(name: Optional[str] = None, **defaults):
    """
    Decorator to register a function as a local (in-process) async job.

    Args:
        name: Custom registration name
        **defaults: Default job options

    Example:
        @local_async_job()
        def quick_task(data):
            # Job implementation
            pass

    Local jobs run in a background thread without Redis/persistence.
    They don't support retries, delays, or distributed execution.
    """
    def decorator(func: Callable) -> Callable:
        # Register in local registry
        _local_registry.register(func, name, **defaults)

        # Create wrapper
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Direct call
            return func(*args, **kwargs)

        # Attach publish helper
        def publish_local(*args, **kwargs):
            """Publish this job to local queue."""
            from mojo.apps.jobs import publish_local as _publish_local
            return _publish_local(func._job_name, *args, **kwargs)

        wrapper.publish_local = publish_local
        wrapper._job_name = func._job_name
        wrapper._job_metadata = func._job_metadata

        return wrapper

    return decorator


def get_job_function(name: str) -> Optional[Callable]:
    """
    Get a registered job function by name.

    Args:
        name: Job registration name

    Returns:
        The job function or None
    """
    return _job_registry.get(name)


def get_local_function(name: str) -> Optional[Callable]:
    """
    Get a registered local job function by name.

    Args:
        name: Job registration name

    Returns:
        The local job function or None
    """
    return _local_registry.get(name)


def list_jobs() -> Dict[str, Dict[str, Any]]:
    """
    List all registered jobs.

    Returns:
        Dict of job name -> metadata
    """
    return _job_registry.list()


def list_local_jobs() -> Dict[str, Dict[str, Any]]:
    """
    List all registered local jobs.

    Returns:
        Dict of job name -> metadata
    """
    return _local_registry.list()


def clear_registries():
    """Clear all job registrations (useful for testing)."""
    _job_registry.clear()
    _local_registry.clear()
