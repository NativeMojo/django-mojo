# Taskit

Taskit is a task management and processing library that uses Redis to handle task states across multiple channels. It provides a flexible and efficient way to publish, execute, and track tasks. Taskit is built with Django and is intended to be used in projects where distributed or asynchronous task execution is required.

## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
  - [Publishing Tasks](#publishing-tasks)
  - [Monitoring Tasks](#monitoring-tasks)
  - [Advanced Usage](#advanced-usage)
- [API Endpoints](#api-endpoints)
- [Command Line Interface](#command-line-interface)
- [License](#license)

## Features

- Asynchronous task management using Redis-backed storage.
- Support for multiple channels to organize and prioritize tasks.
- Task lifecycle management including pending, running, completed, and error states.
- Built-in REST API for monitoring task statuses.
- Thread pool executor for concurrent task execution.


## Configuration

Ensure your Django project is set up to use Taskit by configuring the necessary settings and Redis connection. Update your `settings.py` file:

```python
# settings.py

TASKIT_CHANNELS = ['channel1', 'channel2', 'broadcast']
REDIS_HOST = 'localhost'
REDIS_PORT = 6379
```

## Usage

### Publishing Tasks

To publish a new task, use the `TaskManager` instance provided by Taskit:

```python
from taskit.manager import TaskManager

channels = ['channel1', 'channel2']
task_manager = TaskManager(channels)

def my_task_function(task_data):
    data = task_data.data
    print(f"Processing data: {data}")

task_data = {'key': 'value'}
task_id = task_manager.publish('module.my_task_function', task_data, channel='channel1')

print(f"Task {task_id} published to channel1")
```

### Monitoring Tasks

Taskit includes a REST API for checking the status of tasks:

- `GET /status` - Summary of all tasks across all channels.
- `GET /pending` - List of pending tasks.
- `GET /completed` - List of completed tasks.
- `GET /running` - List of tasks currently running.
- `GET /errors` - List of tasks that encountered errors.

### Advanced Usage

Use the `TaskEngine` to start the task processing engine. This engine will listen for tasks on the specified channels and execute them using a thread pool:

```python
from taskit.runner import TaskEngine

engine = TaskEngine(['channel1', 'channel2'], max_workers=10)
engine.run()
```

You can also leverage the daemon capabilities by using CLI commands like `--start` and `--stop` to manage the task engine:

```sh
python manage.py taskit --start
```

## API Endpoints

Taskit provides several Django REST API endpoints for managing tasks:

- **Status:** `GET /status/` - Get the overall status of the task system.
- **Pending Tasks:** `GET /pending/` - Retrieve all pending tasks.
- **Completed Tasks:** `GET /completed/` - Retrieve all completed tasks.
- **Running Tasks:** `GET /running/` - Retrieve all running tasks.
- **Error Tasks:** `GET /errors/` - Retrieve tasks that encountered errors.

## Command Line Interface

Taskit also offers CLI support for managing the task engine:

```sh
python manage.py taskit --help
```

Options:
- `--start`: Start the task engine daemon.
- `--stop`: Stop the task engine daemon.
- `-f, --foreground`: Run the task engine in the foreground.
- `-v, --verbose`: Enable verbose logging for more detailed output.

## License

This project is licensed under the MIT License. See the LICENSE file for details.

---

This README file provides a comprehensive overview and instructions for using Taskit. Please feel free to ask further questions or raise issues in the GitHub repository.
