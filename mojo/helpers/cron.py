import datetime
import importlib
from typing import Callable, List, Dict, Any
from django.apps import apps
from mojo.decorators.cron import schedule

def run_now() -> None:
    """
    Execute the scheduled functions that match the current time.

    Retrieves the list of functions scheduled to run at the current
    date and time, and executes each of them.
    """
    functions_to_run = find_scheduled_functions()
    for func in functions_to_run:
        func()

def find_scheduled_functions() -> List[Callable]:
    """
    Find all functions that are scheduled to run at the current time.

    Returns:
        List[Callable]: A list of callable functions that match the
        current date and time according to their cron specifications.
    """
    if not hasattr(schedule, 'scheduled_functions'):
        return []

    now = datetime.datetime.now()
    matching_funcs = []

    for cron_spec in schedule.scheduled_functions:
        if match_time(now, cron_spec):
            matching_funcs.append(cron_spec['func'])

    return matching_funcs

def match_time(current_time: datetime.datetime, cron_spec: Dict[str, Any]) -> bool:
    """
    Determine if a given time matches a cron-like specification.

    Args:
        current_time (datetime.datetime): The current date and time.
        cron_spec (Dict[str, Any]): A dictionary containing the cron
        specifications to match against.

    Returns:
        bool: True if the current time matches the cron specification,
        False otherwise.
    """
    cron_field = {
        'minutes': current_time.minute,
        'hours': current_time.hour,
        'days': current_time.day,
        'months': current_time.month,
        'weekdays': current_time.weekday()
    }

    for field, time_value in cron_field.items():
        if not matches(cron_spec[field], time_value):
            return False
    return True

def matches(cron_value: str, actual_value: int) -> bool:
    """
    Check if a specific time value matches the corresponding cron pattern.

    Args:
        cron_value (str): The cron pattern to match. Supports:
            - '*' for wildcard (matches all values)
            - '5' for specific value
            - '1,3,5' for comma-separated values
            - '1-5' for ranges (inclusive)
            - '*/5' for steps (every 5th value)
            - '10-50/5' for ranges with steps
        actual_value (int): The actual time value to compare.

    Returns:
        bool: True if the actual value matches the cron pattern,
        False otherwise.
    """
    if cron_value == '*':
        return True

    # Split by comma to handle multiple patterns
    for pattern in cron_value.split(','):
        pattern = pattern.strip()

        # Handle step values (e.g., */5, 10-50/5)
        if '/' in pattern:
            range_part, step = pattern.split('/', 1)
            try:
                step_value = int(step)
            except ValueError:
                continue

            # Handle */step (wildcard with step)
            if range_part == '*':
                if actual_value % step_value == 0:
                    return True
            # Handle range/step (e.g., 10-50/5)
            elif '-' in range_part:
                start, end = range_part.split('-', 1)
                try:
                    start_val = int(start)
                    end_val = int(end)
                    if start_val <= actual_value <= end_val and \
                       (actual_value - start_val) % step_value == 0:
                        return True
                except ValueError:
                    continue

        # Handle ranges (e.g., 1-5)
        elif '-' in pattern:
            start, end = pattern.split('-', 1)
            try:
                start_val = int(start)
                end_val = int(end)
                if start_val <= actual_value <= end_val:
                    return True
            except ValueError:
                continue

        # Handle single values
        else:
            try:
                if int(pattern) == actual_value:
                    return True
            except ValueError:
                continue

    return False

def load_app_cron() -> None:
    """
    Load cronjob modules from all Django apps.

    Iterates through each registered Django app and attempts to import
    the APP_NAME.cronjobs module if it exists. This ensures that any
    cron-decorated functions are properly registered with the scheduler.
    """
    for app_config in apps.get_app_configs():
        app_name = app_config.name
        cronjobs_module = f"{app_name}.cronjobs"
        try:
            importlib.import_module(cronjobs_module)
        except ImportError:
            # App doesn't have a cronjobs module, continue to next app
            continue
