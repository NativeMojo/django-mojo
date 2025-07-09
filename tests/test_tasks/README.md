# Django Mojo Task System Tests

This directory contains comprehensive tests for the Django Mojo task system, organized into two main test files with distinct purposes.

## Test File Organization

### `manager.py` - TaskManager Unit Tests
**Purpose**: Comprehensive unit tests for the `TaskManager` class methods and low-level functionality.

**Coverage**:
- Task storage and retrieval operations
- Queue management (pending, running, completed, errors)
- Key generation and Redis operations  
- Channel management
- Task lifecycle and state transitions
- Status reporting and metrics
- Cleanup and maintenance operations

**Test Categories**:
- **Setup and Basic Functionality**: Initialization, key generation
- **Task Storage and Retrieval**: Save/get tasks, expiration handling
- **Queue Operations**: Pending, running, completed, error queue management
- **Task Lifecycle Management**: Cancellation, removal, state transitions
- **Status and Reporting**: Status reporting, metrics collection
- **Cleanup and Maintenance**: Dead task cleanup, bulk operations

### `basic.py` - Integration Tests
**Purpose**: Integration tests for the full task workflow and higher-level functionality.

**Coverage**:
- High-level task publishing through `mojo.apps.tasks` interface
- Task lifecycle states and transitions
- Error handling and recovery
- Async task decorators
- Task execution with arguments
- Runner registration and ping system
- Task engine message handling
- Full workflow integration tests
- Concurrent operations
- Data serialization
- Metrics integration

**Test Categories**:
- **High-Level Integration Tests**: Publishing, lifecycle, error handling
- **Task Runner and Engine Tests**: Registration, ping system, execution
- **Advanced Features**: Decorators, serialization, metrics, concurrency

## Reorganization Summary

### Removed Duplicates
The following duplicate tests were removed from `basic.py` as they were redundant with more comprehensive versions in `manager.py`:

1. **`test_task_manager_initialization`** - Removed basic version, kept comprehensive version in `manager.py`
2. **`test_task_manager_key_generation`** - Removed basic version, kept comprehensive version (`test_key_generation_methods`) in `manager.py`
3. **`test_task_expiration`** - Removed basic version, kept comprehensive version (`test_task_expiration_handling`) in `manager.py`
4. **`test_task_cancellation`** - Removed basic version, kept comprehensive version in `manager.py`
5. **`test_get_all_tasks_methods`** - Removed basic version, kept comprehensive version (`test_get_all_methods`) in `manager.py`
6. **`test_task_status_reporting`** - Removed basic version, kept comprehensive version (`test_status_reporting`) in `manager.py`

### Retained Unique Tests
All unique tests were retained in their appropriate files:

- **`manager.py`**: Comprehensive unit tests for TaskManager methods
- **`basic.py`**: Integration tests, task decorators, runner tests, and full workflow tests

## Running the Tests

```bash
# Run all task tests
python manage.py test tests.test_tasks

# Run only TaskManager unit tests
python manage.py test tests.test_tasks.manager

# Run only integration tests
python manage.py test tests.test_tasks.basic
```

## Test Philosophy

- **Unit Tests (`manager.py`)**: Focus on testing individual methods and components in isolation
- **Integration Tests (`basic.py`)**: Focus on testing the system as a whole, including interactions between components
- **No Duplication**: Each test case should exist in only one place to maintain clarity and reduce maintenance overhead
- **Clear Categorization**: Tests are organized into logical sections with clear headers for easy navigation

## Contributing

When adding new tests:
1. **Unit tests** for individual TaskManager methods should go in `manager.py`
2. **Integration tests** for full workflows should go in `basic.py`
3. Ensure no duplicate test cases are created
4. Follow the existing categorization structure
5. Include clear docstrings explaining what each test validates