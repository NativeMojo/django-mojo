"""
Integration tests for Django Mojo TaskEngine.

This module contains integration tests that test the TaskEngine functionality,
including runner management, ping system, message handling, and task execution.
"""
from testit import helpers as th
import time
import json
from unittest.mock import patch
from objict import nobjict


@th.django_unit_setup()
def setup_task_engine_cleanup(opts):
    """Setup and cleanup for TaskEngine tests."""
    from mojo.apps.tasks.manager import TaskManager
    from mojo.apps.tasks.runner import TaskEngine

    # Clean up any existing test data
    manager = TaskManager([
        "test", "test_channel", "broadcast", "runner_test",
        "high_priority", "low_priority", "background"
    ])

    # Clear all test channels
    for channel in ["test", "test_channel", "broadcast", "runner_test", "high_priority", "low_priority", "background"]:
        manager.clear_channel(channel)

    manager.clear_runners(ping_age=5)

@th.django_unit_test()
def test_task_engine_initialization(setup_task_engine_cleanup):
    """Test TaskEngine initialization with different parameters."""
    from mojo.apps.tasks.runner import TaskEngine

    # Test default initialization
    engine = TaskEngine()
    assert engine.channels == ["broadcast", f"runner_{engine.hostname}"], f"Expected channels to be ['broadcast', 'runner_{engine.hostname}'], got {engine.channels}"
    assert engine.max_workers == 5, f"Expected max_workers to be 5, got {engine.max_workers}"
    assert engine.hostname is not None, "Expected hostname to be set, got None"
    assert engine.runner_channel == f"runner_{engine.hostname}", f"Expected runner_channel to be 'runner_{engine.hostname}', got {engine.runner_channel}"
    assert engine.runner_channel in engine.channels, f"Expected runner_channel '{engine.runner_channel}' to be in channels {engine.channels}"

    # Test custom initialization
    custom_channels = ["custom1", "custom2"]
    engine = TaskEngine(channels=custom_channels, max_workers=10)
    assert "broadcast" in engine.channels, f"Expected 'broadcast' to be in channels {engine.channels}"
    assert "custom1" in engine.channels, f"Expected 'custom1' to be in channels {engine.channels}"
    assert "custom2" in engine.channels, f"Expected 'custom2' to be in channels {engine.channels}"
    assert engine.max_workers == 10, f"Expected max_workers to be 10, got {engine.max_workers}"
    assert engine.runner_channel in engine.channels, f"Expected runner_channel '{engine.runner_channel}' to be in channels {engine.channels}"


@th.django_unit_test
def test_runner_registration_and_status(setup_task_engine_cleanup):
    """Test runner registration, status updates, and unregistration."""
    from mojo.apps.tasks.runner import TaskEngine

    engine = TaskEngine(channels=["test"])

    # Test runner registration
    engine.register_runner()

    # Verify runner was registered
    active_runners = engine.manager.get_active_runners()
    assert engine.hostname in active_runners, f"Expected hostname '{engine.hostname}' to be in active runners {list(active_runners.keys())}"

    runner_data = active_runners[engine.hostname]
    assert runner_data['status'] == 'active', f"Expected runner status to be 'active', got '{runner_data['status']}'"
    assert runner_data['max_workers'] == 5, f"Expected max_workers to be 5, got {runner_data['max_workers']}"
    assert 'test' in runner_data['channels'], f"Expected 'test' to be in runner channels {runner_data['channels']}"
    assert 'broadcast' in runner_data['channels'], f"Expected 'broadcast' to be in runner channels {runner_data['channels']}"

    # Test status update
    engine.update_runner_status({'custom_field': 'test_value'})

    updated_runners = engine.manager.get_active_runners()
    updated_data = updated_runners[engine.hostname]
    assert updated_data['custom_field'] == 'test_value', f"Expected custom_field to be 'test_value', got '{updated_data.get('custom_field')}'"
    assert updated_data['status'] == 'active', f"Expected status to remain 'active', got '{updated_data['status']}'"

    # Test unregistration
    engine.unregister_runner()

    remaining_runners = engine.manager.get_active_runners()
    assert engine.hostname not in remaining_runners, f"Expected hostname '{engine.hostname}' to be removed from active runners {list(remaining_runners.keys())}"


@th.django_unit_test()
def test_runner_status_information(setup_task_engine_cleanup):
    """Test get_runner_status method."""
    from mojo.apps.tasks.runner import TaskEngine
    from concurrent.futures import ThreadPoolExecutor

    engine = TaskEngine(channels=["test"], max_workers=3)
    engine.executor = ThreadPoolExecutor(max_workers=3)

    status = engine.get_runner_status()

    assert status['hostname'] == engine.hostname, f"Expected hostname to be '{engine.hostname}', got '{status['hostname']}'"
    assert status['status'] == 'active', f"Expected status to be 'active', got '{status['status']}'"
    assert status['max_workers'] == 3, f"Expected max_workers to be 3, got {status['max_workers']}"
    assert status['channels'] == engine.channels, f"Expected channels to be {engine.channels}, got {status['channels']}"
    assert 'last_ping' in status, f"Expected 'last_ping' to be in status, got keys: {list(status.keys())}"
    assert 'uptime' in status, f"Expected 'uptime' to be in status, got keys: {list(status.keys())}"
    assert 'active_threads' in status, f"Expected 'active_threads' to be in status, got keys: {list(status.keys())}"

    engine.executor.shutdown(wait=False)


@th.django_unit_test
def test_ping_system_functionality(setup_task_engine_cleanup):
    """Test the ping system between runners."""
    from mojo.apps.tasks.runner import TaskEngine

    engine = TaskEngine(channels=["test"])

    # Mock another runner in the active runners list
    mock_runner_data = {
        'hostname': 'test_runner',
        'status': 'active',
        'last_ping': time.time(),
        'channels': ['test']
    }

    engine.manager.redis.hset(
        engine.manager.get_runners_key(),
        'test_runner',
        json.dumps(mock_runner_data)
    )

    # Test ping_runners method
    with patch.object(engine.manager.redis, 'publish') as mock_publish:
        engine.ping_runners()

        # Should have sent a ping to the test_runner
        mock_publish.assert_called_once()
        call_args = mock_publish.call_args
        channel_key = call_args[0][0]
        message = json.loads(call_args[0][1])

        expected_channel = engine.manager.get_channel_key('runner_test_runner')
        assert channel_key == expected_channel, f"Expected ping to be sent to channel '{expected_channel}', got '{channel_key}'"
        assert message['type'] == 'ping', f"Expected message type to be 'ping', got '{message['type']}'"
        assert message['from'] == engine.hostname, f"Expected ping from '{engine.hostname}', got '{message['from']}'"


@th.django_unit_test
def test_ping_request_handling(setup_task_engine_cleanup):
    """Test handling of incoming ping requests."""
    from mojo.apps.tasks.runner import TaskEngine

    engine = TaskEngine(channels=["test"])

    # Create a mock ping request
    ping_request = {
        'type': 'ping',
        'from': 'test_requester',
        'timestamp': time.time()
    }

    with patch.object(engine.manager.redis, 'publish') as mock_publish:
        engine.handle_ping_request(json.dumps(ping_request))

        # Should have sent a ping response
        mock_publish.assert_called_once()
        call_args = mock_publish.call_args
        channel_key = call_args[0][0]
        response = json.loads(call_args[0][1])

        expected_channel = engine.manager.get_channel_key('runner_test_requester')
        assert channel_key == expected_channel, f"Expected ping response to be sent to channel '{expected_channel}', got '{channel_key}'"
        assert response['type'] == 'ping_response', f"Expected response type to be 'ping_response', got '{response['type']}'"
        assert response['from'] == engine.hostname, f"Expected response from '{engine.hostname}', got '{response['from']}'"
        assert response['to'] == 'test_requester', f"Expected response to be addressed to 'test_requester', got '{response['to']}'"
        assert 'status' in response, f"Expected 'status' to be in response, got keys: {list(response.keys())}"


@th.django_unit_test
def test_ping_response_handling(setup_task_engine_cleanup):
    """Test handling of ping responses."""
    from mojo.apps.tasks.runner import TaskEngine

    engine = TaskEngine(channels=["test"])

    # Create a mock ping response
    ping_response = {
        'type': 'ping_response',
        'from': 'test_responder',
        'to': engine.hostname,
        'timestamp': time.time(),
        'status': {
            'hostname': 'test_responder',
            'status': 'active',
            'max_workers': 5
        }
    }

    with patch.object(engine.manager.redis, 'hset') as mock_hset:
        engine.handle_ping_response(json.dumps(ping_response))

        # Should have updated the runner's status
        mock_hset.assert_called_once()
        call_args = mock_hset.call_args
        expected_key = engine.manager.get_runners_key()
        assert call_args[0][0] == expected_key, f"Expected hset to be called with key '{expected_key}', got '{call_args[0][0]}'"
        assert call_args[0][1] == 'test_responder', f"Expected hset to be called with field 'test_responder', got '{call_args[0][1]}'"


@th.django_unit_test
def test_message_handling_routing(setup_task_engine_cleanup):
    """Test message handling and routing logic."""
    from mojo.apps.tasks.runner import TaskEngine

    engine = TaskEngine(channels=["test"])

    # Test ping message handling
    ping_message = {
        'type': 'ping',
        'from': 'test_sender',
        'timestamp': time.time()
    }

    with patch.object(engine, 'handle_ping_request') as mock_ping_handler:
        message = {
            'data': json.dumps(ping_message).encode(),
            'type': 'message'
        }
        engine.handle_message(message)
        mock_ping_handler.assert_called_once(), "Expected handle_ping_request to be called once for ping message"

    # Test ping response message handling
    ping_response = {
        'type': 'ping_response',
        'from': 'test_sender',
        'timestamp': time.time()
    }

    with patch.object(engine, 'handle_ping_response') as mock_response_handler:
        message = {
            'data': json.dumps(ping_response).encode(),
            'type': 'message'
        }
        engine.handle_message(message)
        mock_response_handler.assert_called_once(), "Expected handle_ping_response to be called once for ping response message"

    # Test task message handling
    with patch.object(engine, 'queue_task') as mock_queue_task:
        message = {
            'data': 'test_task_id'.encode(),
            'type': 'message'
        }
        engine.handle_message(message)
        mock_queue_task.assert_called_once_with('test_task_id'), "Expected queue_task to be called once with 'test_task_id'"


@th.django_unit_test
def test_task_execution_workflow(setup_task_engine_cleanup):
    """Test the complete task execution workflow."""
    from mojo.apps.tasks.runner import TaskEngine
    from mojo.apps.tasks.manager import TaskManager
    from mojo.apps.tasks.task import Task

    engine = TaskEngine(channels=["test"])

    # Create a test task
    test_task = Task(
        id="test_task_123",
        function="tests.test_tasks.runner.dummy_test_function",
        channel="test",
        data={"args": ["test_arg"], "kwargs": {"test_kwarg": "test_value"}}
    )

    # Save the task
    manager = TaskManager(["test"])
    manager.save_task(test_task)
    manager.add_to_pending("test_task_123", "test")

    # Mock the function to test
    with patch('tests.test_tasks.runner.dummy_test_function') as mock_function:
        engine.on_run_task("test_task_123")

        # Verify function was called with correct arguments
        mock_function.assert_called_once_with("test_arg", test_kwarg="test_value"), "Expected dummy_test_function to be called with correct arguments"

        # Verify task was moved through proper states
        assert "test_task_123" not in manager.get_pending_ids("test"), f"Expected task 'test_task_123' to be removed from pending queue"
        assert "test_task_123" not in manager.get_running_ids("test"), f"Expected task 'test_task_123' to be removed from running queue"
        assert "test_task_123" in manager.get_completed_ids("test"), f"Expected task 'test_task_123' to be in completed queue"


@th.django_unit_test
def test_task_execution_error_handling(setup_task_engine_cleanup):
    """Test error handling during task execution."""
    from mojo.apps.tasks.runner import TaskEngine
    from mojo.apps.tasks.manager import TaskManager
    from mojo.apps.tasks.task import Task

    engine = TaskEngine(channels=["test"])

    # Create a test task that will fail
    test_task = Task(
        id="test_task_error",
        function="tests.test_tasks.runner.failing_test_function",
        channel="test",
        data={"args": [], "kwargs": {}}
    )

    # Save the task
    manager = TaskManager(["test"])
    manager.save_task(test_task)
    manager.add_to_pending("test_task_error", "test")

    # Mock the function to raise an error
    with patch('tests.test_tasks.runner.failing_test_function') as mock_function:
        mock_function.side_effect = Exception("Test error")

        engine.on_run_task("test_task_error")

        # Verify task was moved to errors
        assert "test_task_error" not in manager.get_pending_ids("test"), f"Expected task 'test_task_error' to be removed from pending queue"
        assert "test_task_error" not in manager.get_running_ids("test"), f"Expected task 'test_task_error' to be removed from running queue"
        assert "test_task_error" in manager.get_error_ids("test"), f"Expected task 'test_task_error' to be in error queue"


@th.django_unit_test
def test_task_queue_functionality(setup_task_engine_cleanup):
    """Test task queuing functionality."""
    from mojo.apps.tasks.runner import TaskEngine
    from concurrent.futures import ThreadPoolExecutor

    engine = TaskEngine(channels=["test"])
    engine.executor = ThreadPoolExecutor(max_workers=2)

    with patch.object(engine, 'on_run_task') as mock_run_task:
        engine.queue_task("test_task_id")

        # Give executor time to process
        time.sleep(0.1)

        # Verify task was submitted for execution
        mock_run_task.assert_called_once_with("test_task_id"), "Expected on_run_task to be called once with 'test_task_id'"

    engine.executor.shutdown(wait=False)


@th.django_unit_test
def test_reset_running_tasks(setup_task_engine_cleanup):
    """Test reset_running_tasks functionality."""
    from mojo.apps.tasks.runner import TaskEngine
    from mojo.apps.tasks.manager import TaskManager

    engine = TaskEngine(channels=["test"])
    manager = TaskManager(["test"])

    # Add some tasks to running state
    manager.add_to_running("running_task_1", "test")
    manager.add_to_running("running_task_2", "test")

    # Verify tasks are in running state
    assert "running_task_1" in manager.get_running_ids("test"), "Expected 'running_task_1' to be in running queue before reset"
    assert "running_task_2" in manager.get_running_ids("test"), "Expected 'running_task_2' to be in running queue before reset"

    # Reset running tasks
    engine.reset_running_tasks()

    # Verify tasks were moved to pending
    assert "running_task_1" not in manager.get_running_ids("test"), "Expected 'running_task_1' to be removed from running queue after reset"
    assert "running_task_2" not in manager.get_running_ids("test"), "Expected 'running_task_2' to be removed from running queue after reset"
    assert "running_task_1" in manager.get_pending_ids("test"), "Expected 'running_task_1' to be moved to pending queue after reset"
    assert "running_task_2" in manager.get_pending_ids("test"), "Expected 'running_task_2' to be moved to pending queue after reset"


@th.django_unit_test
def test_queue_pending_tasks(setup_task_engine_cleanup):
    """Test queuing of pending tasks."""
    from mojo.apps.tasks.runner import TaskEngine
    from mojo.apps.tasks.manager import TaskManager

    engine = TaskEngine(channels=["test"])
    manager = TaskManager(["test"])

    # Clear all test channels
    manager.clear_channel("test")


    assert manager.get_pending_ids("test") == [], "Expected no pending tasks"

    # Add some tasks to pending state
    manager.save_task(nobjict(id="pending_task_1", channel="test", data={"key": "value"}))
    manager.save_task(nobjict(id="pending_task_2", channel="test", data={"key": "value"}))

    manager.add_to_pending("pending_task_1", "test")
    manager.add_to_pending("pending_task_2", "test")

    with patch.object(engine, 'queue_task') as mock_queue_task:
        engine.queue_pending_tasks()

        # Verify all pending tasks were queued
        assert mock_queue_task.call_count == 2, f"Expected queue_task to be called 2 times, got {mock_queue_task.call_count}"
        mock_queue_task.assert_any_call("pending_task_1"), "Expected queue_task to be called with 'pending_task_1'"
        mock_queue_task.assert_any_call("pending_task_2"), "Expected queue_task to be called with 'pending_task_2'"


@th.django_unit_test
def test_cleanup_stale_runners(setup_task_engine_cleanup):
    """Test cleanup of stale runners."""
    from mojo.apps.tasks.runner import TaskEngine

    engine = TaskEngine(channels=["test"])

    # Add a current runner
    current_runner = {
        'hostname': 'current_runner',
        'status': 'active',
        'last_ping': time.time()
    }

    # Add a stale runner
    stale_runner = {
        'hostname': 'stale_runner',
        'status': 'active',
        'last_ping': time.time() - 200  # Old timestamp
    }

    engine.manager.redis.hset(
        engine.manager.get_runners_key(),
        'current_runner',
        json.dumps(current_runner)
    )

    engine.manager.redis.hset(
        engine.manager.get_runners_key(),
        'stale_runner',
        json.dumps(stale_runner)
    )

    # Run cleanup
    engine.cleanup_stale_runners()

    # Verify stale runner was removed
    active_runners = engine.manager.get_active_runners()
    assert 'current_runner' in active_runners, f"Expected 'current_runner' to remain in active runners {list(active_runners.keys())}"
    assert 'stale_runner' not in active_runners, f"Expected 'stale_runner' to be removed from active runners {list(active_runners.keys())}"


@th.django_unit_test
def test_ping_thread_management(setup_task_engine_cleanup):
    """Test ping thread start and management."""
    from mojo.apps.tasks.runner import TaskEngine

    engine = TaskEngine(channels=["test"])
    engine.running = True

    # Mock the ping methods to avoid actual network calls
    with patch.object(engine, 'ping_runners') as mock_ping_runners, \
         patch.object(engine, 'update_runner_status') as mock_update_status:

        engine.start_ping_thread()

        # Verify ping thread was created
        assert engine.ping_thread is not None, "Expected ping_thread to be created"
        assert engine.ping_thread.is_alive(), "Expected ping_thread to be alive"

        # Give thread time to run one iteration
        time.sleep(0.1)

        # Stop the thread
        engine.running = False
        engine.ping_thread.join(timeout=1)

        # Verify methods were called
        mock_ping_runners.assert_called(), "Expected ping_runners to be called"
        mock_update_status.assert_called(), "Expected update_runner_status to be called"


@th.django_unit_test
def test_executor_shutdown(setup_task_engine_cleanup):
    """Test executor shutdown functionality."""
    from mojo.apps.tasks.runner import TaskEngine
    from concurrent.futures import ThreadPoolExecutor

    engine = TaskEngine(channels=["test"])
    engine.executor = ThreadPoolExecutor(max_workers=2)

    # Submit a quick task
    future = engine.executor.submit(lambda: time.sleep(0.1))

    result = engine.wait_for_all_tasks_to_complete(timeout=4)

    assert result, "Expected all tasks to complete"

    # Verify executor is shutdown
    assert engine.executor._shutdown, "Expected executor to be shutdown"


@th.django_unit_test
def test_multi_channel_support(setup_task_engine_cleanup):
    """Test TaskEngine with multiple channels."""
    from mojo.apps.tasks.runner import TaskEngine
    from mojo.apps.tasks.manager import TaskManager

    channels = ["channel1", "channel2", "channel3"]
    engine = TaskEngine(channels=channels)

    # Verify all channels are included plus broadcast and runner channel
    assert "channel1" in engine.channels, f"Expected 'channel1' to be in channels {engine.channels}"
    assert "channel2" in engine.channels, f"Expected 'channel2' to be in channels {engine.channels}"
    assert "channel3" in engine.channels, f"Expected 'channel3' to be in channels {engine.channels}"
    assert "broadcast" in engine.channels, f"Expected 'broadcast' to be in channels {engine.channels}"
    assert engine.runner_channel in engine.channels, f"Expected runner_channel '{engine.runner_channel}' to be in channels {engine.channels}"

    # Test reset_running_tasks across multiple channels
    manager = TaskManager(channels)

    # Add tasks to different channels
    manager.add_to_running("task1", "channel1")
    manager.add_to_running("task2", "channel2")
    manager.add_to_running("task3", "channel3")

    engine.reset_running_tasks()

    # Verify tasks were moved to pending in all channels
    assert "task1" in manager.get_pending_ids("channel1"), "Expected 'task1' to be moved to pending in channel1"
    assert "task2" in manager.get_pending_ids("channel2"), "Expected 'task2' to be moved to pending in channel2"
    assert "task3" in manager.get_pending_ids("channel3"), "Expected 'task3' to be moved to pending in channel3"


# Helper functions for testing
def dummy_test_function(*args, **kwargs):
    """Dummy function for testing task execution."""
    pass


def failing_test_function(*args, **kwargs):
    """Function that always fails for testing error handling."""
    raise Exception("Test error")
