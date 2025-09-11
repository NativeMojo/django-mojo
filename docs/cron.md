# MOJO Cron Scheduler

## Overview

MOJO Cron Scheduler is a lightweight scheduling utility built to run periodic tasks in Python using cron syntax. It allows developers to easily schedule functions based on time intervals similar to Linux cron job specifications. This project comprises a `schedule` decorator for marking functions for scheduling and a simple execution engine to run tasks at specified intervals.

## Features

- **Easy Scheduling**: Use cron-like syntax to schedule Python functions with a simple decorator.
- **Flexibility**: Define schedules for functions with minute, hour, day, month, and weekday granularity.
- **Simplicity**: Minimal setup and clear function annotation.
- **Execution**: Run scheduled functions at times that match their cron specifications.

## Components

### `schedule` Decorator

The `schedule` decorator allows you to specify a cron-like schedule for when a function should be executed.

#### Syntax

```python
@schedule(minutes="*", hours="*", days="*", months="*", weekdays="*")
def my_scheduled_function():
    # Your code here
```

#### Parameters

- `minutes`: Cron pattern for minutes (0-59). Default is `*`, meaning every minute.
- `hours`: Cron pattern for hours (0-23). Default is `*`, meaning every hour.
- `days`: Cron pattern for days of the month (1-31). Default is `*`, meaning every day.
- `months`: Cron pattern for months (1-12). Default is `*`, meaning every month.
- `weekdays`: Cron pattern for days of the week (0-6, where 0 is Sunday). Default is `*`, meaning every weekday.

#### Returns

- The original function, with its cron specification recorded for later execution.

### Usage Examples

#### Every Minute

```python
@schedule()
def task_every_minute():
    print("This task runs every minute.")
```

Equivalent to setting all cron parameters to `*`.

#### Every Hour on the 30th Minute

```python
@schedule(minutes="30")
def task_half_past_every_hour():
    print("This task runs every hour on the 30th minute.")
```

Runs at 00:30, 01:30, ..., 23:30.

#### Daily at 9:00 AM

```python
@schedule(hours="9", minutes="0")
def task_daily_morning():
    print("This task runs every day at 9:00 AM.")
```

#### Every Monday and Friday at 12:00 Noon

```python
@schedule(weekdays="1,4", hours="12", minutes="0")
def task_manfri_noon():
    print("This task runs every Monday and Friday at 12:00 PM.")
```

### Execution Helper

#### `run_now`

This helper function executes all scheduled functions that match the current time.

```python
from mojo.helpers.cron import run_now

run_now()
```

Call `run_now()` when you want to check and run any scheduled tasks if they match the current time.

### Utility Functions

#### `find_scheduled_functions`

Finds all functions scheduled to run at the current time.

```python
from mojo.helpers.cron import find_scheduled_functions

functions_ready = find_scheduled_functions()
for func in functions_ready:
    func()
```

#### `match_time`

Checks if a given time matches a cron-like specification.

```python
from mojo.helpers.cron import match_time
import datetime

current_time = datetime.datetime.now()
cron_spec = {'minutes': '30', 'hours': '9', 'days': '*', 'months': '*', 'weekdays': '*'}

if match_time(current_time, cron_spec):
    print("Current time matches the cron specification.")
```

## App-Based Cronjobs

### Loading Cronjobs from Django Apps

MOJO Cron Scheduler supports automatic discovery of cronjobs from Django apps. Each Django app can define its cronjobs in a dedicated module.

#### `load_app_cron`

This function automatically discovers and imports cronjob modules from all registered Django apps.

```python
from mojo.helpers.cron import load_app_cron

# Load all app cronjobs
load_app_cron()
```

#### App Cronjob Structure

To add cronjobs to a Django app:

1. Create a `cronjobs.py` file in your app directory
2. Define your scheduled functions using the `@schedule` decorator

```
myapp/
├── __init__.py
├── models.py
├── views.py
└── cronjobs.py  # Define your scheduled tasks here
```

#### Example App Cronjobs

**myapp/cronjobs.py**:
```python
from mojo.decorators.cron import schedule

@schedule(hours="2", minutes="0")
def cleanup_old_data():
    """Run daily at 2:00 AM to clean up old data."""
    print("Cleaning up old data...")
    # Your cleanup logic here

@schedule(minutes="*/15")
def send_notifications():
    """Run every 15 minutes to send pending notifications."""
    print("Sending notifications...")
    # Your notification logic here
```

#### Integration with Django

To ensure all app cronjobs are loaded when your Django application starts, call `load_app_cron()` in your application startup code (e.g., in your main `urls.py` or app configuration).

```python
from mojo.helpers.cron import load_app_cron

# Load all cronjobs from Django apps
load_app_cron()
```

### Example Workflow

1. Define scheduled tasks using `@schedule` decorator in your Python script or app's `cronjobs.py` module.
2. Call `load_app_cron()` to discover and import all app cronjobs.
3. Periodically call `run_now()` (e.g., set up a loop or use a dedicated scheduler).
4. `run_now()` will execute tasks whose times match the current time.

### Conclusion

The MOJO Cron Scheduler provides a simple and effective way to schedule and execute Python functions based on cron-like time definitions. Use the `schedule` decorator to define tasks and integrate `run_now()` into your application main loop for execution.
