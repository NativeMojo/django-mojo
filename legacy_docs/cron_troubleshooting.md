# Cron Scheduling Troubleshooting Guide

## Overview

The Django-MOJO cron scheduling system allows you to schedule functions to run at specific times using cron-like syntax. This guide covers common issues and solutions.

## Issue: Cron Jobs Not Firing as Expected

### Problem Description

Users reported that various cron schedules were not firing correctly, particularly:
- Jobs scheduled with `*/5` (every 5 minutes) were not running
- Jobs scheduled with ranges like `10-20` were not matching properly
- Complex patterns with steps and ranges were failing silently

### Root Cause

The `matches()` function in `mojo/helpers/cron.py` only supported:
1. Wildcard (`*`) patterns
2. Single values (`5`)
3. Comma-separated values (`0,15,30,45`)

It was missing support for standard cron syntax including:
- **Step values**: `*/5` (every 5 units)
- **Ranges**: `10-20` (values from 10 to 20)
- **Ranges with steps**: `10-50/5` (every 5th value from 10 to 50)

### Solution Applied

The `matches()` function has been enhanced to support full cron syntax:

```python
def matches(cron_value: str, actual_value: int) -> bool:
    """
    Check if a specific time value matches the corresponding cron pattern.
    
    Now supports:
    - '*' for wildcard (matches all values)
    - '5' for specific value
    - '1,3,5' for comma-separated values
    - '1-5' for ranges (inclusive)
    - '*/5' for steps (every 5th value)
    - '10-50/5' for ranges with steps
    """
```

## How the Cron System Works

### 1. Decorator Registration

Functions are registered using the `@schedule` decorator:

```python
from mojo.decorators.cron import schedule

@schedule(minutes="*/5")  # Every 5 minutes
def my_task():
    pass

@schedule(minutes="0", hours="9")  # Daily at 9:00 AM
def daily_task():
    pass
```

### 2. Cron Pattern Execution Flow

1. **App Loading**: The Django project calls `load_app_cron()` to import all `cronjobs.py` modules
2. **Time Matching**: When `run_now()` is called, it:
   - Gets the current time
   - Checks each registered function's schedule against the current time
   - Executes functions that match
3. **Pattern Matching**: Each field (minutes, hours, days, months, weekdays) is checked independently

### 3. Supported Cron Patterns

#### Basic Patterns
- `*` - Matches any value
- `5` - Matches exactly 5
- `1,3,5` - Matches 1, 3, or 5

#### Advanced Patterns (Now Working)
- `*/5` - Every 5 units (0, 5, 10, 15, ...)
- `10-20` - Range from 10 to 20 (inclusive)
- `10-50/5` - Every 5th value between 10 and 50
- `0-5,10,15,20-25` - Complex combinations

## Common Use Cases

### Every X Minutes

```python
@schedule(minutes="*/5")  # Every 5 minutes
@schedule(minutes="*/15")  # Every 15 minutes
@schedule(minutes="*/30")  # Every 30 minutes
```

### Specific Times

```python
@schedule(minutes="0", hours="9")  # 9:00 AM daily
@schedule(minutes="30", hours="14")  # 2:30 PM daily
@schedule(minutes="0,30", hours="*")  # Every hour at :00 and :30
```

### Business Hours

```python
@schedule(minutes="0", hours="9-17")  # Every hour from 9 AM to 5 PM
@schedule(minutes="*/15", hours="8-18", weekdays="0-4")  # Every 15 min, Mon-Fri, 8 AM-6 PM
```

### Specific Days

```python
@schedule(minutes="0", hours="0", days="1")  # Midnight on the 1st of each month
@schedule(minutes="0", hours="9", weekdays="0")  # 9 AM every Monday
@schedule(minutes="0", hours="12", weekdays="5")  # Noon every Friday
```

## Debugging Cron Jobs

### 1. Test Your Patterns

Use the test file `tests/test_helpers/cron.py` to verify your patterns:

```bash
./bin/testit.py -m test_helpers.cron
```

### 2. Manual Testing

Test if your function would run at a specific time:

```python
from mojo.helpers.cron import match_time
import datetime

# Test if job would run at 11:50
test_time = datetime.datetime(2024, 1, 15, 11, 50, 0)
cron_spec = {
    'minutes': '*/5',
    'hours': '*',
    'days': '*',
    'months': '*',
    'weekdays': '*'
}
result = match_time(test_time, cron_spec)
print(f"Would run at 11:50? {result}")  # True (50 is divisible by 5)
```

### 3. Check Registration

Verify your functions are registered:

```python
from mojo.decorators.cron import schedule
from mojo.helpers.cron import load_app_cron

# Load all cronjobs
load_app_cron()

# Check registered functions
if hasattr(schedule, 'scheduled_functions'):
    for spec in schedule.scheduled_functions:
        print(f"Function: {spec['func'].__name__}")
        print(f"  Minutes: {spec['minutes']}")
        print(f"  Hours: {spec['hours']}")
```

## Migration Guide

If you have existing cron jobs that weren't working:

### Before (Not Working)
```python
@schedule(minutes="*/5")  # Didn't work - pattern not supported
def every_five_minutes():
    pass

@schedule(hours="9-17")  # Didn't work - range not supported
def business_hours():
    pass
```

### After (Now Working)
```python
@schedule(minutes="*/5")  # Now works correctly
def every_five_minutes():
    pass

@schedule(hours="9-17")  # Now works correctly
def business_hours():
    pass

# Even complex patterns now work
@schedule(minutes="0-30/5", hours="9-17", weekdays="0-4")
def complex_schedule():
    pass
```

## Important Notes

1. **Weekday Values**: 0=Monday, 1=Tuesday, ..., 6=Sunday
2. **Time Zones**: The system uses server local time by default
3. **Execution**: The Django project must call `run_now()` regularly (typically every minute via system cron or scheduler)
4. **Registration**: Cronjobs must be in a `cronjobs.py` file in your app directory

## Testing Your Fix

After applying the fix, test that patterns work correctly:

```python
from mojo.helpers.cron import matches

# Test step patterns
assert matches('*/5', 0) == True   # ✓ Now works
assert matches('*/5', 5) == True   # ✓ Now works
assert matches('*/5', 10) == True  # ✓ Now works
assert matches('*/5', 3) == False  # ✓ Correctly rejects

# Test ranges
assert matches('10-20', 15) == True   # ✓ Now works
assert matches('10-20', 25) == False  # ✓ Correctly rejects

# Test range with step
assert matches('10-50/5', 20) == True   # ✓ Now works
assert matches('10-50/5', 22) == False  # ✓ Correctly rejects
```

## Conclusion

The cron scheduling system now fully supports standard cron syntax patterns. If you experience issues:

1. Verify your pattern syntax is correct
2. Check that your app's `cronjobs.py` is being loaded
3. Ensure the Django project is calling `run_now()` at the expected times
4. Use the debugging techniques above to isolate the issue

For further assistance, refer to the test suite in `tests/test_helpers/cron.py` which contains comprehensive examples of all supported patterns.
