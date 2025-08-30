from testit import helpers as th
import time
from unittest.mock import Mock, patch

# A simple list to act as a mock "in-memory database" or state tracker for local tasks.
# This allows us to verify that the local tasks were executed.
local_task_results = []

def sample_local_task(arg1, kwarg1=None):
    """A simple function to be used as a local task for testing."""
    local_task_results.append({'arg1': arg1, 'kwarg1': kwarg1})

# ==============================================================================
# LOCAL IN-MEMORY QUEUE TESTS
# ==============================================================================

@th.django_unit_test()
def test_publish_local_task_success(opts):
    """Test successful publishing and execution of a local task."""
    from mojo.apps import tasks
    local_task_results.clear()

    # The worker should be started automatically by the AppConfig
    assert tasks.is_local_worker_running(), "Local task worker thread is not running."

    # Publish a task to the local queue
    tasks.publish_local(sample_local_task, "test_arg", kwarg1="test_kwarg")

    # Give the worker thread a moment to process the task
    time.sleep(0.1)

    # Verify the task was executed
    assert len(local_task_results) == 1, f"Expected 1 result, got {len(local_task_results)}"
    result = local_task_results[0]
    assert result['arg1'] == "test_arg", f"Expected arg1 to be 'test_arg', got {result['arg1']}"
    assert result['kwarg1'] == "test_kwarg", f"Expected kwarg1 to be 'test_kwarg', got {result['kwarg1']}"

@th.django_unit_test()
def test_publish_local_task_model_instance_error(opts):
    """Test that publish_local raises TypeError when passed a Django model instance."""
    from mojo.apps import tasks
    from django.contrib.auth.models import User
    local_task_results.clear()

    # Create a dummy user instance
    user = User(pk=1, username="testuser")

    # Test with positional args
    try:
        tasks.publish_local(sample_local_task, user)
        assert False, "Expected TypeError was not raised for model instance in args"
    except TypeError as e:
        assert "Cannot pass Django model instance" in str(e), f"Unexpected TypeError message: {e}"

    # Test with keyword args
    try:
        tasks.publish_local(sample_local_task, "arg1", kwarg1=user)
        assert False, "Expected TypeError was not raised for model instance in kwargs"
    except TypeError as e:
        assert "Cannot pass Django model instance" in str(e), f"Unexpected TypeError message: {e}"

    # Verify that no task was executed
    assert len(local_task_results) == 0, "No task should have been executed"
