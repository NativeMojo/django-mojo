"""
Comprehensive unit tests for Django Mojo TaskManager class.

This module contains detailed unit tests for the TaskManager class methods,
focusing on low-level functionality including:
- Task storage and retrieval operations
- Queue management (pending, running, completed, errors)
- Key generation and Redis operations
- Channel management
- Task lifecycle and state transitions
- Status reporting and metrics
- Cleanup and maintenance operations

For integration tests and higher-level functionality, see test_tasks/basic.py.
"""
from testit import helpers as th
import time
from unittest.mock import patch


# ==============================================================================
# SETUP AND BASIC FUNCTIONALITY TESTS
# ==============================================================================

@th.django_unit_setup()
def setup_tasks_cleanup(opts):
    from mojo.apps.tasks.manager import TaskManager
    manager = TaskManager([
        "single_channel", "test_channel", "ch1", "ch2", "ch3",
        "storage_test", "expiration_test", "pending_test", "running_test",
        "completed_test", "error_test", "transition_test", "cancel_test",
        "removal_test", "publish_test", "channel_a", "channel_b", "channel_c",
        "status_test", "runner_test", "cleanup_test"
    ])
    manager.clear_local_queues()
    manager.remove_all_channels()
    manager.take_out_the_dead()

@th.django_unit_test()
def test_task_manager_initialization(opts):
    """Test TaskManager initialization with different channel configurations"""
    from mojo.apps.tasks.manager import TaskManager

    # Test with single channel
    manager = TaskManager(["single_channel"])
    assert manager.channels == ["single_channel"], "Single channel initialization failed"
    assert manager.prefix == "mojo:tasks", "Default prefix should be 'mojo:tasks'"

    # Test with multiple channels
    channels = ["ch1", "ch2", "ch3"]
    manager = TaskManager(channels)
    assert manager.channels == channels, "Multiple channels initialization failed"

    # Test with empty channels
    manager = TaskManager([])
    assert manager.channels == [], "Empty channels initialization failed"

    # Test with custom prefix
    manager = TaskManager(["test"], prefix="custom:prefix")
    assert manager.prefix == "custom:prefix", "Custom prefix initialization failed"


@th.django_unit_test()
def test_key_generation_methods(opts):
    """Test all key generation methods"""
    from mojo.apps.tasks.manager import TaskManager

    manager = TaskManager(["test_channel"])

    # Test channel-specific keys
    assert manager.get_completed_key("test") == "mojo:tasks:d:test", "Completed key generation failed"
    assert manager.get_pending_key("test") == "mojo:tasks:p:test", "Pending key generation failed"
    assert manager.get_error_key("test") == "mojo:tasks:e:test", "Error key generation failed"
    assert manager.get_running_key("test") == "mojo:tasks:r:test", "Running key generation failed"

    # Test task-specific keys
    assert manager.get_task_key("task123") == "mojo:tasks:t:task123", "Task key generation failed"
    assert manager.get_channel_key("broadcast") == "mojo:tasks:c:broadcast", "Channel key generation failed"

    # Test global keys
    assert manager.get_runners_key() == "mojo:tasks:runners", "Runners key generation failed"
    assert manager.get_global_pending_key() == "mojo:tasks:pending", "Global pending key generation failed"
    assert manager.get_channels_key() == "mojo:tasks:channels", "Channels key generation failed"

    # Test with special characters in names
    assert manager.get_task_key("task-with-dashes") == "mojo:tasks:t:task-with-dashes", "Task key with dashes failed"
    assert manager.get_channel_key("channel_with_underscores") == "mojo:tasks:c:channel_with_underscores", "Channel key with underscores failed"


# ==============================================================================
# TASK STORAGE AND RETRIEVAL TESTS
# ==============================================================================

@th.django_unit_test()
def test_task_storage_and_retrieval(opts):
    """Test task save and get operations"""
    from mojo.apps.tasks.manager import TaskManager
    from mojo.apps.tasks.task import Task

    manager = TaskManager(["storage_test"])

    # Create test task
    task = Task(
        id="storage_test_task",
        function="test.function",
        data={"key": "value", "number": 42},
        channel="storage_test"
    )

    # Test saving task with default expiration
    manager.save_task(task)

    # Test retrieving task
    retrieved_task = manager.get_task(task.id)
    assert retrieved_task is not None, "Task should be retrievable after saving"
    assert retrieved_task.id == task.id, "Retrieved task ID should match original"
    assert retrieved_task.function == task.function, "Retrieved task function should match original"
    assert retrieved_task.data == task.data, "Retrieved task data should match original"
    assert retrieved_task.channel == task.channel, "Retrieved task channel should match original"

    # Test saving task with custom expiration
    task2 = Task(id="task2", function="test.func2", data={}, channel="storage_test")
    manager.save_task(task2, expires=3600)

    retrieved_task2 = manager.get_task(task2.id)
    assert retrieved_task2 is not None, "Task with custom expiration should be retrievable"
    assert retrieved_task2.id == task2.id, "Retrieved task2 ID should match original"

    # Test retrieving non-existent task
    non_existent = manager.get_task("does_not_exist")
    assert non_existent is None, "Non-existent task should return None"


@th.django_unit_test()
def test_task_expiration_handling(opts):
    """Test task expiration and key TTL functionality"""
    from mojo.apps.tasks.manager import TaskManager
    from mojo.apps.tasks.task import Task

    manager = TaskManager(["expiration_test"])

    # Create task with short expiration
    current_time = time.time()
    task = Task(
        id="expiring_task",
        function="test.function",
        data={},
        channel="expiration_test",
        expires=current_time + 1  # Expires in 1 second
    )

    manager.save_task(task, expires=2)  # Redis key expires in 2 seconds

    # Task should be available immediately
    retrieved = manager.get_task(task.id)
    assert retrieved is not None, "Task should be available immediately after saving"

    # Test get_key_expiration
    ttl = manager.get_key_expiration(task.id)
    assert ttl is not None, "TTL should be available for existing task"
    assert ttl > 0, "TTL should be positive for non-expired task"

    # Test with non-existent task
    non_existent_ttl = manager.get_key_expiration("non_existent")
    assert non_existent_ttl is None, "TTL should be None for non-existent task"


@th.django_unit_test()
def test_channel_management(opts):
    """Test channel management operations"""
    from mojo.apps.tasks.manager import TaskManager

    manager = TaskManager(["test_channel"])

    # Test adding channels
    manager.add_channel("new_channel")
    channels = manager.get_all_channels()
    assert "new_channel" in channels, "New channel should be added to channel list"

    # Test adding multiple channels
    manager.add_channel("channel1")
    manager.add_channel("channel2")
    channels = manager.get_all_channels()
    assert "channel1" in channels, "Channel1 should be in channel list"
    assert "channel2" in channels, "Channel2 should be in channel list"

    # Test removing a channel
    manager.remove_channel("channel1")
    channels = manager.get_all_channels()
    assert "channel1" not in channels, "Channel1 should be removed from channel list"
    assert "channel2" in channels, "Channel2 should remain in channel list"

    # Test removing all channels
    manager.remove_all_channels()
    channels = manager.get_all_channels()
    assert len(channels) == 0, "All channels should be removed"


# ==============================================================================
# QUEUE OPERATIONS TESTS
# ==============================================================================

@th.django_unit_test()
def test_pending_queue_operations(opts):
    """Test pending queue management"""
    from mojo.apps.tasks.manager import TaskManager
    from mojo.apps.tasks.task import Task

    manager = TaskManager(["pending_test"])
    channel = "pending_test"

    # Create test tasks
    task1 = Task(id="pending_1", function="func1", data={}, channel=channel)
    task2 = Task(id="pending_2", function="func2", data={}, channel=channel)

    manager.save_task(task1)
    manager.save_task(task2)

    # Test adding to pending (note: parameters are task_id, channel)
    manager.add_to_pending(task1.id, channel)
    manager.add_to_pending(task2.id, channel)

    # Test getting pending IDs
    pending_ids = manager.get_pending_ids(channel)
    assert task1.id in pending_ids, "Task1 should be in pending queue"
    assert task2.id in pending_ids, "Task2 should be in pending queue"

    # Test getting pending tasks
    pending_tasks = manager.get_pending(channel)
    pending_task_ids = [task.id for task in pending_tasks]
    assert task1.id in pending_task_ids, "Task1 should be in pending tasks list"
    assert task2.id in pending_task_ids, "Task2 should be in pending tasks list"

    # Test removing from pending
    manager.remove_from_pending(task1.id, channel)
    updated_pending = manager.get_pending_ids(channel)
    assert task1.id not in updated_pending, "Task1 should be removed from pending queue"
    assert task2.id in updated_pending, "Task2 should remain in pending queue"

    # Test with default channel
    manager.add_to_pending(task1.id)  # Should use default channel
    default_pending = manager.get_pending_ids()
    assert task1.id in default_pending, "Task1 should be in default pending queue"


@th.django_unit_test()
def test_running_queue_operations(opts):
    """Test running queue management"""
    from mojo.apps.tasks.manager import TaskManager
    from mojo.apps.tasks.task import Task

    manager = TaskManager(["running_test"])
    channel = "running_test"

    task = Task(id="running_task", function="func", data={}, channel=channel)
    manager.save_task(task)

    # Test adding to running
    manager.add_to_running(task.id, channel)

    # Test getting running IDs
    running_ids = manager.get_running_ids(channel)
    assert task.id in running_ids, "Task should be in running queue"

    # Test getting running tasks
    running_tasks = manager.get_running(channel)
    assert len(running_tasks) == 1, "Should have exactly one running task"
    assert running_tasks[0].id == task.id, "Running task ID should match original"

    # Test removing from running
    manager.remove_from_running(task.id, channel)
    updated_running = manager.get_running_ids(channel)
    assert task.id not in updated_running, "Task should be removed from running queue"

    # Test with default channel
    manager.add_to_running(task.id)  # Should use default channel
    default_running = manager.get_running_ids()
    assert task.id in default_running, "Task should be in default running queue"


@th.django_unit_test()
def test_completed_queue_operations(opts):
    """Test completed queue management"""
    from mojo.apps.tasks.manager import TaskManager
    from mojo.apps.tasks.task import Task

    manager = TaskManager(["completed_test"])
    channel = "completed_test"

    task = Task(id="completed_task", function="func", data={}, channel=channel)
    task.completed_at = time.time()
    manager.save_task(task)

    # Test adding to completed (takes task_data object)
    manager.add_to_completed(task)

    # Test getting completed IDs
    completed_ids = manager.get_completed_ids(channel)
    assert task.id in completed_ids, "Task should be in completed queue"

    # Test getting completed tasks
    completed_tasks = manager.get_completed(channel)
    assert len(completed_tasks) == 1, "Should have exactly one completed task"
    assert completed_tasks[0].id == task.id, "Completed task ID should match original"
    assert completed_tasks[0].status == "completed", "Completed task status should be 'completed'"

    # Test removing from completed
    manager.remove_from_completed(task.id, channel)
    updated_completed = manager.get_completed_ids(channel)
    assert task.id not in updated_completed, "Task should be removed from completed queue"


@th.django_unit_test()
def test_error_queue_operations(opts):
    """Test error queue management"""
    from mojo.apps.tasks.manager import TaskManager
    from mojo.apps.tasks.task import Task

    manager = TaskManager(["error_test"])
    channel = "error_test"

    task = Task(id="error_task", function="func", data={}, channel=channel)
    manager.save_task(task)

    # Test adding to errors (takes task_data and error_message)
    error_message = "Test error occurred"
    manager.add_to_errors(task, error_message)

    # Test getting error IDs
    error_ids = manager.get_error_ids(channel)
    assert task.id in error_ids, "Task should be in error queue"

    # Test getting error tasks
    error_tasks = manager.get_errors(channel)
    assert len(error_tasks) == 1, "Should have exactly one error task"
    assert error_tasks[0].id == task.id, "Error task ID should match original"
    assert error_tasks[0].error == error_message, "Error message should match"
    assert error_tasks[0].status == "error", "Error task status should be 'error'"

    # Test removing from errors
    manager.remove_from_errors(task.id, channel)
    updated_errors = manager.get_error_ids(channel)
    assert task.id not in updated_errors, "Task should be removed from error queue"


@th.django_unit_test()
def test_task_state_transitions(opts):
    """Test complete task state transitions"""
    from mojo.apps.tasks.manager import TaskManager
    from mojo.apps.tasks.task import Task

    manager = TaskManager(["transition_test"])
    channel = "transition_test"

    task = Task(id="transition_task", function="func", data={}, channel=channel)
    manager.save_task(task)

    # Initial state: add to pending
    manager.add_to_pending(task.id, channel)
    assert task.id in manager.get_pending_ids(channel), "Task should be in pending queue initially"
    assert task.id not in manager.get_running_ids(channel), "Task should not be in running queue initially"

    # Transition: pending -> running
    manager.remove_from_pending(task.id, channel)
    manager.add_to_running(task.id, channel)
    assert task.id not in manager.get_pending_ids(channel), "Task should be removed from pending queue"
    assert task.id in manager.get_running_ids(channel), "Task should be in running queue"

    # Transition: running -> completed
    manager.remove_from_running(task.id, channel)
    task.mark_as_completed()
    manager.add_to_completed(task)
    assert task.id not in manager.get_running_ids(channel), "Task should be removed from running queue"
    assert task.id in manager.get_completed_ids(channel), "Task should be in completed queue"


# ==============================================================================
# TASK LIFECYCLE MANAGEMENT TESTS
# ==============================================================================

@th.django_unit_test()
def test_task_cancellation(opts):
    """Test task cancellation functionality"""
    from mojo.apps.tasks.manager import TaskManager
    from mojo.apps.tasks.task import Task

    manager = TaskManager(["cancel_test"])
    channel = "cancel_test"

    # Test cancelling pending task
    pending_task = Task(id="pending_cancel", function="func", data={}, channel=channel)
    manager.save_task(pending_task)
    manager.add_to_pending(pending_task.id, channel)

    success = manager.cancel_task(pending_task.id)
    assert success is True, "Task cancellation should succeed"

    # Task should be removed from pending
    assert pending_task.id not in manager.get_pending_ids(channel), "Cancelled task should be removed from pending queue"

    # Task status should be cancelled (note: manager uses "cancelled" not "cancelled")
    cancelled_task = manager.get_task(pending_task.id)
    assert cancelled_task.status == "cancelled", "Cancelled task status should be 'cancelled'"

    # Test cancelling non-existent task
    success = manager.cancel_task("non_existent")
    assert success is False, "Cancelling non-existent task should return False"


@th.django_unit_test()
def test_task_removal(opts):
    """Test complete task removal"""
    from mojo.apps.tasks.manager import TaskManager
    from mojo.apps.tasks.task import Task

    manager = TaskManager(["removal_test"])
    channel = "removal_test"

    task = Task(id="removal_task", function="func", data={}, channel=channel)
    manager.save_task(task)
    manager.add_to_pending(task.id, channel)

    # Remove task completely
    success = manager.remove_task(task.id)
    assert success is True, "Task removal should succeed"

    # Task should be gone from storage
    assert manager.get_task(task.id) is None, "Removed task should not be retrievable from storage"

    # Task should be gone from queues
    assert task.id not in manager.get_pending_ids(channel), "Removed task should not be in pending queue"

    # Test removing non-existent task
    success = manager.remove_task("non_existent")
    assert success is False, "Removing non-existent task should return False"


@th.django_unit_test()
def test_task_publishing(opts):
    """Test task publishing functionality"""
    from mojo.apps.tasks.manager import TaskManager

    manager = TaskManager(["publish_test"])

    # Mock the publish method to avoid actual Redis publishing
    with patch.object(manager.redis, 'publish') as mock_publish:
        # Test basic publishing
        task_id = manager.publish(
            function="test.function",
            data={"test": "data"},
            channel="publish_test"
        )

        assert task_id is not None, "Published task should return task ID"
        assert isinstance(task_id, str), "Task ID should be a string"

        # Verify task was created and added to pending
        task = manager.get_task(task_id)
        assert task is not None, "Published task should be retrievable"
        assert task.function == "test.function", "Published task function should match"
        assert task.data == {"test": "data"}, "Published task data should match"
        assert task.channel == "publish_test", "Published task channel should match"
        assert task.status == "pending", "Published task status should be 'pending'"

        pending_ids = manager.get_pending_ids("publish_test")
        assert task_id in pending_ids, "Published task should be in pending queue"

        # Test publishing with custom expiration
        task_id_2 = manager.publish(
            function="test.function2",
            data={},
            channel="publish_test",
            expires=3600
        )

        task_2 = manager.get_task(task_id_2)
        assert task_2.expires > time.time(), "Published task with custom expiration should have future expiration"

        # Test publishing to default channel
        task_id_3 = manager.publish(
            function="test.function3",
            data={}
        )

        task_3 = manager.get_task(task_id_3)
        assert task_3.channel == "default", "Task published without channel should use default channel"

        # Verify Redis publish was called
        assert mock_publish.call_count == 3, "Redis publish should be called 3 times"


@th.django_unit_test()
def test_cross_channel_operations(opts):
    """Test operations across multiple channels"""
    from mojo.apps.tasks.manager import TaskManager
    from mojo.apps.tasks.task import Task

    channels = ["channel_a", "channel_b", "channel_c"]
    manager = TaskManager(channels)

    # Create tasks for each channel
    tasks = {}
    for channel in channels:
        task = Task(id=f"task_{channel}", function="func", data={}, channel=channel)
        manager.save_task(task)
        manager.add_to_pending(task.id, channel)
        tasks[channel] = task.id

    # Test get_all_pending_ids
    all_pending = manager.get_all_pending_ids(local=True)
    for task_id in tasks.values():
        assert task_id in all_pending, f"Task {task_id} should be in all pending list"

    # Move some tasks to different states
    manager.remove_from_pending(tasks["channel_a"], "channel_a")
    manager.add_to_running(tasks["channel_a"], "channel_a")

    task_b = manager.get_task(tasks["channel_b"])
    manager.remove_from_pending(tasks["channel_b"], "channel_b")
    manager.add_to_completed(task_b)

    # Test cross-channel status
    all_running = manager.get_all_running_ids(local=True)
    assert tasks["channel_a"] in all_running, "Channel A task should be in all running list"
    assert tasks["channel_b"] not in all_running, "Channel B task should not be in all running list"

    all_completed = manager.get_all_completed_ids(local=True)
    assert tasks["channel_b"] in all_completed, "Channel B task should be in all completed list"
    assert tasks["channel_a"] not in all_completed, "Channel A task should not be in all completed list"

    # Test get_all_* methods
    all_pending_tasks = manager.get_all_pending(local=True)
    assert len(all_pending_tasks) == 1, "Should have exactly one pending task remaining"  # Only channel_c task remains pending

    all_running_tasks = manager.get_all_running(local=True)
    assert len(all_running_tasks) == 1, "Should have exactly one running task"  # Only channel_a task is running

    all_completed_tasks = manager.get_all_completed(local=True)
    assert len(all_completed_tasks) == 1, "Should have exactly one completed task"  # Only channel_b task is completed


# ==============================================================================
# STATUS AND REPORTING TESTS
# ==============================================================================

@th.django_unit_test()
def test_status_reporting(opts):
    """Test comprehensive status reporting"""
    from mojo.apps.tasks.manager import TaskManager
    from mojo.apps.tasks.task import Task

    manager = TaskManager(["status_test"])
    channel = "status_test"

    # Create tasks in different states
    tasks = []
    for i in range(10):
        task = Task(id=f"status_task_{i}", function="func", data={}, channel=channel)
        manager.save_task(task)
        tasks.append(task)

    channel_status = manager.get_channel_status(channel)
    assert channel_status.pending == 0, "Channel status should show 0 pending tasks"
    assert channel_status.running == 0, "Channel status should show 0 running tasks"
    assert channel_status.completed == 0, "Channel status should show 0 completed tasks"
    assert channel_status.errors == 0, "Channel status should show 0 error tasks"

    # Distribute across states
    for i in range(3):
        manager.add_to_pending(tasks[i].id, channel)

    for i in range(3, 5):
        manager.add_to_running(tasks[i].id, channel)

    for i in range(5, 8):
        manager.add_to_completed(tasks[i])

    for i in range(8, 10):
        manager.add_to_errors(tasks[i], "Error")

    # Get channel status
    channel_status = manager.get_channel_status(channel)
    assert channel_status.pending == 3, "Channel status should show 3 pending tasks"
    assert channel_status.running == 2, "Channel status should show 2 running tasks"
    assert channel_status.completed == 3, "Channel status should show 3 completed tasks"
    assert channel_status.errors == 2, "Channel status should show 2 error tasks"

    # Get overall status
    status = manager.get_status(local=True)
    assert status.pending == 3, "Overall status should show 3 pending tasks"
    assert status.running == 2, "Overall status should show 2 running tasks"
    assert status.completed == 3, "Overall status should show 3 completed tasks"
    assert status.errors == 2, "Overall status should show 2 error tasks"
    assert "runners" in status, "Overall status should include runners"
    assert "channels" in status, "Overall status should include channels"
    assert channel in status.channels, "Overall status should include the test channel"

    # Test simple status
    simple_status = manager.get_status(simple=True, local=True)
    assert simple_status.pending == 3, "Simple status should show 3 pending tasks"
    assert simple_status.running == 2, "Simple status should show 2 running tasks"
    assert simple_status.completed == 3, "Simple status should show 3 completed tasks"
    assert simple_status.errors == 2, "Simple status should show 2 error tasks"
    assert "runners" in simple_status, "Simple status should include runners"
    assert "channels" not in simple_status, "Simple status should not include channels"


@th.django_unit_test()
def test_runner_management(opts):
    """Test active runner management"""
    from mojo.apps.tasks.manager import TaskManager
    import json

    manager = TaskManager(["runner_test"])

    # Mock redis operations for runner management
    mock_runners_data = {
        b"runner1": json.dumps({
            "hostname": "runner1",
            "status": "active",
            "last_ping": time.time(),
            "max_workers": 5
        }).encode(),
        b"runner2": json.dumps({
            "hostname": "runner2",
            "status": "active",
            "last_ping": time.time() - 100,  # Old ping
            "max_workers": 3
        }).encode()
    }

    with patch.object(manager.redis, 'hgetall', return_value=mock_runners_data):
        active_runners = manager.get_active_runners()

        assert "runner1" in active_runners, "Runner1 should be in active runners"
        assert "runner2" in active_runners, "Runner2 should be in active runners"
        assert active_runners["runner1"]["hostname"] == "runner1", "Runner1 hostname should match"
        assert active_runners["runner2"]["max_workers"] == 3, "Runner2 max_workers should be 3"
        assert active_runners["runner2"]["status"] == "timeout", "Runner2 status should be timeout due to old ping"
        assert "ping_age" in active_runners["runner1"], "Runner1 should have ping_age"
        assert "ping_age" in active_runners["runner2"], "Runner2 should have ping_age"


# ==============================================================================
# CLEANUP AND MAINTENANCE TESTS
# ==============================================================================

@th.django_unit_test()
def test_dead_task_cleanup(opts):
    """Test dead task cleanup functionality"""
    from mojo.apps.tasks.manager import TaskManager
    from mojo.apps.tasks.task import Task

    manager = TaskManager(["cleanup_test"])
    channel = "cleanup_test"

    # Add test channel
    manager.add_channel(channel)

    # Create some tasks
    task1 = Task(id="task1", function="func", data={}, channel=channel)
    task2 = Task(id="task2", function="func", data={}, channel=channel)

    manager.save_task(task1)
    manager.save_task(task2)
    manager.add_to_pending(task1.id, channel)
    manager.add_to_running(task2.id, channel)

    # Mock get_pending and get_running to simulate cleanup
    with patch.object(manager, 'get_pending', return_value=[task1]) as mock_get_pending:
        with patch.object(manager, 'get_running', return_value=[task2]) as mock_get_running:
            manager.take_out_the_dead()

            # Verify that cleanup methods were called for each channel
            mock_get_pending.assert_called(), "get_pending should be called during cleanup"
            mock_get_running.assert_called(), "get_running should be called during cleanup"


@th.django_unit_test()
def test_get_all_methods(opts):
    """Test all get_all_* methods"""
    from mojo.apps.tasks.manager import TaskManager
    from mojo.apps.tasks.task import Task

    manager = TaskManager(["ch1", "ch2"])

    # Create tasks across channels
    tasks = []
    for i, channel in enumerate(["ch1", "ch2"]):
        for j in range(3):
            task = Task(id=f"task_{channel}_{j}", function="func", data={}, channel=channel)
            manager.save_task(task)
            tasks.append(task)

    # Add to different states
    manager.add_to_pending(tasks[0].id, "ch1")
    manager.add_to_pending(tasks[1].id, "ch1")
    manager.add_to_running(tasks[2].id, "ch1")

    manager.add_to_pending(tasks[3].id, "ch2")
    manager.add_to_completed(tasks[4])
    manager.add_to_errors(tasks[5], "Error")

    # Test get_all_*_ids methods
    all_pending_ids = manager.get_all_pending_ids(local=True)
    assert len(all_pending_ids) == 3, "Should have 3 pending tasks across all channels"
    assert tasks[0].id in all_pending_ids, "Task 0 should be in all pending IDs"
    assert tasks[1].id in all_pending_ids, "Task 1 should be in all pending IDs"
    assert tasks[3].id in all_pending_ids, "Task 3 should be in all pending IDs"

    all_running_ids = manager.get_all_running_ids(local=True)
    assert len(all_running_ids) == 1, "Should have 1 running task across all channels"
    assert tasks[2].id in all_running_ids, "Task 2 should be in all running IDs"

    all_completed_ids = manager.get_all_completed_ids(local=True)
    assert len(all_completed_ids) == 1, "Should have 1 completed task across all channels"
    assert tasks[4].id in all_completed_ids, "Task 4 should be in all completed IDs"

    all_error_ids = manager.get_all_error_ids(local=True)
    assert len(all_error_ids) == 1, "Should have 1 error task across all channels"
    assert tasks[5].id in all_error_ids, "Task 5 should be in all error IDs"

    # Test get_all_* methods (with task objects)
    all_pending = manager.get_all_pending(local=True)
    assert len(all_pending) == 3, "Should have 3 pending task objects"

    all_running = manager.get_all_running(local=True)
    assert len(all_running) == 1, "Should have 1 running task object"

    all_completed = manager.get_all_completed(local=True)
    assert len(all_completed) == 1, "Should have 1 completed task object"

    all_errors = manager.get_all_errors(local=True)
    assert len(all_errors) == 1, "Should have 1 error task object"

    # Test include_data parameter
    all_pending_with_data = manager.get_all_pending(include_data=True, local=True)
    assert len(all_pending_with_data) == 3, "Should have 3 pending tasks with data"
    for task in all_pending_with_data:
        assert hasattr(task, 'data'), "Task should have data attribute when include_data=True"

    all_pending_without_data = manager.get_all_pending(include_data=False, local=True)
    assert len(all_pending_without_data) == 3, "Should have 3 pending tasks without data"
    for task in all_pending_without_data:
        assert 'data' not in task, "Task should not have data attribute when include_data=False"
