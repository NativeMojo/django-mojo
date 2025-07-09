"""
Integration tests for Django Mojo Task system.

This module contains integration tests that test the full task workflow,
including task decorators, execution, and higher-level functionality.
For low-level TaskManager unit tests, see test_tasks/manager.py.
"""
from testit import helpers as th
import time
import json
from unittest.mock import Mock, patch
from objict import nobjict


@th.django_unit_setup()
def setup_tasks_cleanup(opts):
    from mojo.apps.tasks.manager import TaskManager
    manager = TaskManager([
        "test", "test_channel", "bg_tasks", "custom_channel",
        "high_priority", "low_priority", "background", "cleanup_test",
        "concurrent_test", "serialization_test", "integration_test"
    ])
    manager.take_out_the_dead()
    manager.clear_local_queues()
    manager.remove_all_channels()

# ==============================================================================
# HIGH-LEVEL INTEGRATION TESTS
# ==============================================================================

@th.django_unit_test()
def test_publish_task(opts):
    """Test high-level task publishing through mojo.apps.tasks interface"""
    from mojo.apps import tasks

    # Test basic task publishing
    task_id = tasks.publish(
        channel="test_channel",
        function="mojo.apps.tasks.tq_handlers.run_quick_task",
        data={"test": "data"}
    )

    assert task_id is not None, "Task ID should not be None"
    assert isinstance(task_id, str), f"Task ID should be string, got {type(task_id)}"

    # Verify task was saved
    manager = tasks.get_manager()
    task_data = manager.get_task(task_id)
    assert task_data is not None, f"Task data should not be None for task_id {task_id}"
    assert task_data.function == "mojo.apps.tasks.tq_handlers.run_quick_task", f"Function mismatch: {task_data.function}"
    assert task_data.data == {"test": "data"}, f"Data mismatch: {task_data.data}"
    assert task_data.channel == "test_channel", f"Channel mismatch: {task_data.channel}"


@th.django_unit_test()
def test_task_lifecycle_states(opts):
    """Test task state transitions"""
    from mojo.apps.tasks.manager import TaskManager

    manager = TaskManager(["test"])
    channel = "test"

    # Create a test task
    task = nobjict(
        id="test_task_123",
        function="test.function",
        data={"test": True},
        channel=channel
    )

    # Save task
    manager.save_task(task)

    # Test adding to pending
    manager.add_to_pending(task.id, channel)
    pending_ids = manager.get_pending_ids(channel)
    assert task.id in pending_ids, f"Task {task.id} should be in pending list: {pending_ids}"

    # Test moving to running
    manager.remove_from_pending(task.id, channel)
    manager.add_to_running(task.id, channel)
    running_ids = manager.get_running_ids(channel)
    assert task.id in running_ids, f"Task {task.id} should be in running list: {running_ids}"
    pending_ids = manager.get_pending_ids(channel)
    assert task.id not in pending_ids, f"Task {task.id} should not be in pending list after moving to running: {pending_ids}"

    # Test moving to completed
    manager.remove_from_running(task.id, channel)
    manager.add_to_completed(task)
    completed_ids = manager.get_completed_ids(channel)
    assert task.id in completed_ids, f"Task {task.id} should be in completed list: {completed_ids}"
    running_ids = manager.get_running_ids(channel)
    assert task.id not in running_ids, f"Task {task.id} should not be in running list after completion: {running_ids}"


@th.django_unit_test()
def test_task_error_handling(opts):
    """Test task error state management"""
    from mojo.apps.tasks.manager import TaskManager

    manager = TaskManager(["test"])
    channel = "test"

    task = nobjict(
        id="error_task_123",
        function="test.error.function",
        data={"test": True},
        channel=channel
    )

    manager.save_task(task)

    # Test adding to errors
    error_message = "Test error occurred"
    manager.add_to_errors(task, error_message)

    error_ids = manager.get_error_ids(channel)
    assert task.id in error_ids, f"Task {task.id} should be in error list: {error_ids}"

    # Verify error details are saved
    updated_task = manager.get_task(task.id)
    assert updated_task.error == error_message, f"Error message mismatch: expected '{error_message}', got '{updated_task.error}'"
    assert updated_task.status == "error", f"Status should be 'error', got '{updated_task.status}'"


@th.django_unit_test()
def test_async_task_decorator(opts):
    """Test the async_task decorator functionality"""
    from mojo.apps.tasks import async_task

    # Test decorator with default settings
    @async_task()
    def test_function(arg1, arg2, kwarg1=None):
        return f"executed: {arg1}, {arg2}, {kwarg1}"

    # Test direct execution (with _from_task_queue flag)
    result = test_function("a", "b", kwarg1="c", _from_task_queue=True)
    assert result == "executed: a, b, c", f"Direct execution result mismatch: {result}"

    # Test task publishing (without flag)
    with patch('mojo.apps.tasks.publish') as mock_publish:
        mock_publish.return_value = "task_123"
        result = test_function("x", "y", kwarg1="z")

        assert result is True, f"Task publishing should return True, got {result}"
        mock_publish.assert_called_once()

        # Verify the call parameters
        call_args = mock_publish.call_args
        # print(call_args[1])
        assert call_args[1]["channel"] == "bg_tasks", f"Channel mismatch: {call_args[1]}"
        assert call_args[1]['expires'] == 1800, f"Expires mismatch: {call_args[1]['expires']}"
        assert 'test_function' in call_args[1]['function'], f"Function name not found in: {call_args[1]['function']}"


@th.django_unit_test()
def test_async_task_decorator_custom_channel(opts):
    """Test async_task decorator with custom channel"""
    from mojo.apps.tasks import async_task

    @async_task(channel="custom_channel", expires=3600)
    def custom_task(data):
        return f"custom: {data}"

    with patch('mojo.apps.tasks.publish') as mock_publish:
        mock_publish.return_value = "task_456"
        result = custom_task("test_data")

        assert result is True, f"Custom task should return True, got {result}"
        call_args = mock_publish.call_args
        assert call_args[1]['channel'] == "custom_channel", f"Channel mismatch: {call_args[1]['channel']}"
        assert call_args[1]['expires'] == 3600, f"Expires mismatch: {call_args[1]['expires']}"


@th.django_unit_test()
def test_task_execution_with_args_kwargs(opts):
    """Test task execution with various argument patterns"""
    from mojo.apps.tasks.runner import TaskEngine
    import uuid

    # Mock TaskEngine for testing
    engine = TaskEngine(["test"], max_workers=1)

    # Test task with args and kwargs
    task_id = str(uuid.uuid4())
    task = nobjict(
        id=task_id,
        function="mojo.apps.tasks.tq_handlers.run_args_kwargs_task",
        data={
            "args": ["arg1", "arg2"],
            "kwargs": {"kw1": "value1", "kw2": "value2"}
        },
        channel="test"
    )

    with patch.object(engine.manager, 'get_task', return_value=task):
        with patch.object(engine.manager, 'remove_from_pending'):
            with patch.object(engine.manager, 'add_to_running'):
                with patch.object(engine.manager, 'add_to_completed'):
                    with patch.object(engine.manager, 'remove_from_running'):
                        # Execute the task
                        engine.on_run_task(task_id)


# ==============================================================================
# TASK RUNNER AND ENGINE TESTS
# ==============================================================================

@th.django_unit_test()
def test_runner_registration(opts):
    """Test TaskEngine runner registration"""
    from mojo.apps.tasks.runner import TaskEngine
    import socket

    with patch('socket.gethostname', return_value='test-host'):
        engine = TaskEngine(["test"], max_workers=2)

        with patch.object(engine.manager.redis, 'hset') as mock_hset:
            engine.register_runner()

            # Verify registration call
            mock_hset.assert_called_once()
            call_args = mock_hset.call_args
            assert call_args[0][0] == engine.manager.get_runners_key(), f"Redis key mismatch: {call_args[0][0]}"
            assert call_args[0][1] == 'test-host', f"Hostname mismatch: {call_args[0][1]}"

            # Verify runner data
            runner_data = json.loads(call_args[0][2])
            assert runner_data['hostname'] == 'test-host', f"Runner hostname mismatch: {runner_data['hostname']}"
            assert runner_data['max_workers'] == 2, f"Runner max_workers mismatch: {runner_data['max_workers']}"
            assert runner_data['status'] == 'active', f"Runner status mismatch: {runner_data['status']}"


@th.django_unit_test()
def test_runner_ping_system(opts):
    """Test runner ping/heartbeat system"""
    from mojo.apps.tasks.runner import TaskEngine
    import json

    with patch('socket.gethostname', return_value='test-host'):
        engine = TaskEngine(["test"])

        # Test ping request handling
        ping_message = {
            'type': 'ping',
            'from': 'other-host',
            'timestamp': time.time()
        }

        with patch.object(engine.manager.redis, 'publish') as mock_publish:
            engine.handle_ping_request(json.dumps(ping_message))

            # Verify response was sent
            mock_publish.assert_called_once()
            call_args = mock_publish.call_args

            response_data = json.loads(call_args[0][1])
            assert response_data['type'] == 'ping_response', f"Response type mismatch: {response_data['type']}"
            assert response_data['from'] == 'test-host', f"Response from mismatch: {response_data['from']}"
            assert response_data['to'] == 'other-host', f"Response to mismatch: {response_data['to']}"


@th.django_unit_test()
def test_task_engine_message_handling(opts):
    """Test TaskEngine message handling"""
    from mojo.apps.tasks.runner import TaskEngine
    import json

    engine = TaskEngine(["test"])

    # Test regular task message
    with patch.object(engine, 'queue_task') as mock_queue:
        message = {
            'type': 'message',
            'data': b'task_123'
        }
        engine.handle_message(message)
        mock_queue.assert_called_once_with('task_123')

    # Test ping message
    with patch.object(engine, 'handle_ping_request') as mock_ping:
        ping_data = {'type': 'ping', 'from': 'test'}
        message = {
            'type': 'message',
            'data': json.dumps(ping_data).encode()
        }
        engine.handle_message(message)
        mock_ping.assert_called_once()


@th.django_unit_test()
def test_task_execution_success(opts):
    """Test successful task execution"""
    from mojo.apps.tasks.runner import TaskEngine
    from mojo.apps.tasks.manager import TaskManager

    # Create a task that should succeed
    task = nobjict(
        id="success_task",
        function="mojo.apps.tasks.tq_handlers.run_quick_task",
        data={"test": "success"},
        channel="test"
    )

    # Mock the task manager methods
    with patch.object(TaskManager, 'get_task', return_value=task):
        with patch.object(TaskManager, 'remove_from_pending') as mock_remove_pending:
            with patch.object(TaskManager, 'add_to_running') as mock_add_running:
                with patch.object(TaskManager, 'add_to_completed') as mock_add_completed:
                    with patch.object(TaskManager, 'remove_from_running') as mock_remove_running:
                        with patch.object(TaskManager, 'save_task') as mock_save_task:

                            engine = TaskEngine(["test"])
                            engine.on_run_task("success_task")

                            # Verify state transitions
                            mock_remove_pending.assert_called_once()
                            mock_add_running.assert_called_once()
                            mock_add_completed.assert_called_once()
                            mock_remove_running.assert_called_once()
                            mock_save_task.assert_called_once()


@th.django_unit_test()
def test_task_execution_error(opts):
    """Test task execution with error"""
    from mojo.apps.tasks.runner import TaskEngine
    from mojo.apps.tasks.manager import TaskManager

    # Create a task that should fail
    task = nobjict(
        id="error_task",
        function="mojo.apps.tasks.tq_handlers.run_error_task",
        data={"test": "error"},
        channel="test"
    )

    # Mock the task manager methods
    with patch.object(TaskManager, 'get_task', return_value=task):
        with patch.object(TaskManager, 'remove_from_pending'):
            with patch.object(TaskManager, 'add_to_running'):
                with patch.object(TaskManager, 'add_to_errors') as mock_add_errors:
                    with patch.object(TaskManager, 'remove_from_running'):

                        engine = TaskEngine(["test"])
                        engine.on_run_task("error_task")

                        # Verify error handling
                        mock_add_errors.assert_called_once()


@th.django_unit_test()
def test_multiple_channels(opts):
    """Test task management across multiple channels"""
    from mojo.apps.tasks.manager import TaskManager

    channels = ["high_priority", "low_priority", "background"]
    manager = TaskManager(channels)

    # Create tasks for each channel
    tasks = {}
    for i, channel in enumerate(channels):
        task = nobjict(
            id=f"task_{channel}_{i}",
            function="test.function",
            data={"channel": channel},
            channel=channel
        )
        manager.save_task(task)
        manager.add_to_pending(task.id, channel)
        tasks[channel] = task.id

    # Verify tasks are in correct channels
    for channel in channels:
        pending_ids = manager.get_pending_ids(channel)
        assert tasks[channel] in pending_ids, f"Task {tasks[channel]} should be in pending for channel {channel}: {pending_ids}"

        # Verify task is not in other channels
        for other_channel in channels:
            if other_channel != channel:
                other_pending = manager.get_pending_ids(other_channel)
                assert tasks[channel] not in other_pending, f"Task {tasks[channel]} should not be in pending for channel {other_channel}: {other_pending}"


@th.django_unit_test()
def test_task_cleanup_operations(opts):
    """Test task cleanup and maintenance operations"""
    from mojo.apps.tasks.manager import TaskManager

    manager = TaskManager(["cleanup_test"])
    channel = "cleanup_test"

    # Create multiple tasks
    task_ids = []
    for i in range(5):
        task = nobjict(
            id=f"cleanup_task_{i}",
            function="test.function",
            data={},
            channel=channel
        )
        manager.save_task(task)
        task_ids.append(task.id)

    # Add to various states
    manager.add_to_pending(task_ids[0], channel)
    manager.add_to_pending(task_ids[1], channel)
    manager.add_to_running(task_ids[2], channel)
    task3 = manager.get_task(task_ids[3])
    manager.add_to_errors(task3, "Error")

    # Test removing tasks
    for task_id in task_ids:
        success = manager.remove_task(task_id)
        assert success, f"Task removal should succeed for task {task_id}"

        # Verify task is removed from storage
        retrieved_task = manager.get_task(task_id)
        assert retrieved_task is None, f"Task {task_id} should be removed from storage, got {retrieved_task}"


@th.django_unit_test()
def test_concurrent_task_operations(opts):
    """Test thread-safe task operations"""
    from mojo.apps.tasks.manager import TaskManager
    import threading
    import uuid

    manager = TaskManager(["concurrent_test"])
    channel = "concurrent_test"
    results = []
    errors = []

    def create_and_process_task(index):
        try:
            task_id = str(uuid.uuid4())
            task = nobjict(
                id=task_id,
                function="test.function",
                data={"index": index},
                channel=channel
            )

            # Save and add to pending
            manager.save_task(task)
            manager.add_to_pending(task_id, channel)

            # Move through states
            manager.remove_from_pending(task_id, channel)
            manager.add_to_running(task_id, channel)
            manager.remove_from_running(task_id, channel)
            manager.add_to_completed(task)

            results.append(task_id)

        except Exception as e:
            errors.append(str(e))

    # Run concurrent operations
    threads = []
    for i in range(10):
        thread = threading.Thread(target=create_and_process_task, args=(i,))
        threads.append(thread)
        thread.start()

    # Wait for completion
    for thread in threads:
        thread.join()

    # Verify results
    assert len(errors) == 0, f"Errors occurred: {errors}"
    assert len(results) == 10, f"Expected 10 results, got {len(results)}: {results}"

    # Verify all tasks completed
    completed_ids = manager.get_completed_ids(channel)
    for task_id in results:
        assert task_id in completed_ids, f"Task {task_id} should be in completed list: {completed_ids}"


@th.django_unit_test()
def test_task_data_serialization(opts):
    """Test task data serialization and deserialization"""
    from mojo.apps.tasks.manager import TaskManager

    manager = TaskManager(["serialization_test"])

    # Test with complex data structures
    complex_data = {
        "string": "test",
        "number": 42,
        "float": 3.14,
        "boolean": True,
        "list": [1, 2, 3, "four"],
        "dict": {"nested": {"deep": "value"}},
        "null": None
    }

    task = nobjict(
        id="serialization_task",
        function="test.function",
        data=complex_data,
        channel="serialization_test"
    )

    # Save and retrieve
    manager.save_task(task)
    retrieved_task = manager.get_task(task.id)

    assert retrieved_task is not None, f"Retrieved task should not be None for task {task.id}"
    assert retrieved_task.data == complex_data, f"Data serialization mismatch: expected {complex_data}, got {retrieved_task.data}"
    assert retrieved_task.function == task.function, f"Function mismatch: expected {task.function}, got {retrieved_task.function}"
    assert retrieved_task.channel == task.channel, f"Channel mismatch: expected {task.channel}, got {retrieved_task.channel}"


@th.django_unit_test()
def test_task_metrics_integration(opts):
    """Test task metrics recording"""
    from mojo.apps.tasks.runner import TaskEngine
    from mojo.apps.tasks.manager import TaskManager
    from mojo.apps import metrics

    # Mock metrics recording
    with patch.object(metrics, 'record') as mock_record:

        # Test successful task completion
        task = nobjict(
            id="metrics_task",
            function="mojo.apps.tasks.tq_handlers.run_quick_task",
            data={},
            channel="test"
        )

        with patch.object(TaskManager, 'get_task', return_value=task):
            with patch.object(TaskManager, 'remove_from_pending'):
                with patch.object(TaskManager, 'add_to_running'):
                    with patch.object(TaskManager, 'add_to_completed'):
                        with patch.object(TaskManager, 'remove_from_running'):
                            with patch.object(TaskManager, 'save_task'):

                                engine = TaskEngine(["test"])
                                engine.on_run_task("metrics_task")

                                # Verify metrics were recorded
                                mock_record.assert_called_with("tasks_completed", category="tasks")


@th.django_unit_test()
def test_full_integration_workflow(opts):
    """Integration test of complete task workflow"""
    from mojo.apps import tasks
    from mojo.apps.tasks.runner import TaskEngine
    from mojo.apps.tasks.manager import TaskManager
    import time

    # Publish a task
    task_id = tasks.publish(
        channel="integration_test",
        function="mojo.apps.tasks.tq_handlers.run_quick_task",
        data={"integration": "test"}
    )

    assert task_id is not None, f"Task ID should not be None, got {task_id}"

    # Verify task is pending
    manager = tasks.get_manager()
    pending_ids = manager.get_pending_ids("integration_test")
    assert task_id in pending_ids, f"Task {task_id} should be in pending list: {pending_ids}"

    # Create engine and process the task
    engine = TaskEngine(["integration_test"], max_workers=1)

    # Mock the execution to avoid actual threading complexity in tests
    with patch.object(engine, 'executor') as mock_executor:
        mock_executor.submit = Mock()
        # Simulate task processing
        engine.queue_task(task_id)
        # Verify task was queued
        mock_executor.submit.assert_called_once_with(engine.on_run_task, task_id)
