"""
Redis adapter for the jobs system.
Imports the framework-level RedisAdapter with backward compatibility.
"""
# Import from framework
from mojo.helpers.redis import RedisAdapter, get_adapter as get_framework_adapter, reset_adapter


# Maintain backward compatibility for jobs module
def get_adapter() -> RedisAdapter:
    """
    Get the default Redis adapter instance for jobs.

    Returns:
        RedisAdapter instance from framework
    """
    return get_framework_adapter()


# Expose reset function for testing
def reset_adapter():
    """Reset the default adapter (useful for testing)."""
    from mojo.helpers.redis import reset_adapter as framework_reset
    framework_reset()
