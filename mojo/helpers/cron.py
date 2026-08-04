import datetime
import importlib
import json
import time
import uuid
from typing import Callable, List, Dict, Any
from django.apps import apps
from mojo.decorators.cron import schedule
from mojo.helpers import logit


CRON_HEARTBEAT_PREFIX = "mojo:cron:heartbeat"
CRON_HEARTBEAT_INDEX = f"{CRON_HEARTBEAT_PREFIX}:runs"
CRON_HEARTBEAT_TTL = 86400


def _heartbeat_client():
    from mojo.helpers.redis import get_connection
    return get_connection()


def _heartbeat_key(run_id):
    return f"{CRON_HEARTBEAT_PREFIX}:run:{run_id}"


def _write_heartbeat(run_id, payload, started_timestamp, redis_client=None):
    """Write one expiring run record; heartbeat failures never stop cron."""
    try:
        client = redis_client or _heartbeat_client()
        pipe = client.pipeline(transaction=True)
        pipe.set(
            _heartbeat_key(run_id), json.dumps(payload, sort_keys=True),
            ex=CRON_HEARTBEAT_TTL,
        )
        pipe.zadd(CRON_HEARTBEAT_INDEX, {run_id: started_timestamp})
        pipe.zremrangebyscore(CRON_HEARTBEAT_INDEX, 0, time.time() - CRON_HEARTBEAT_TTL)
        pipe.expire(CRON_HEARTBEAT_INDEX, CRON_HEARTBEAT_TTL)
        pipe.execute()
    except Exception as exc:
        logit.warning(f"cron heartbeat write failed: {exc}")


def get_cron_heartbeats(limit=10, redis_client=None):
    """Return the newest durable cron run records without hiding Redis errors."""
    client = redis_client or _heartbeat_client()
    run_ids = client.zrevrange(CRON_HEARTBEAT_INDEX, 0, max(0, limit - 1))
    records = []
    for run_id in run_ids:
        if isinstance(run_id, bytes):
            run_id = run_id.decode("utf-8")
        raw = client.get(_heartbeat_key(run_id))
        if not raw:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        record = json.loads(raw)
        if record.get("run_id") == run_id:
            records.append(record)
    return records

def run_now() -> None:
    """
    Execute the scheduled functions that match the current time.

    Retrieves the list of functions scheduled to run at the current
    date and time, and executes each of them.
    """
    functions_to_run = find_scheduled_functions()
    run_id = uuid.uuid4().hex
    started = datetime.datetime.now(datetime.timezone.utc)
    started_timestamp = started.timestamp()
    payload = {
        "version": 1,
        "run_id": run_id,
        "state": "started",
        "started_at": started.isoformat(),
        "matched_count": len(functions_to_run),
        "completed_count": 0,
        "failed_count": 0,
    }
    _write_heartbeat(run_id, dict(payload), started_timestamp)
    completed = 0
    try:
        for func in functions_to_run:
            func()
            completed += 1
    except Exception as exc:
        payload.update({
            "state": "failed",
            "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "completed_count": completed,
            "failed_count": 1,
            "failure": type(exc).__name__,
        })
        _write_heartbeat(run_id, dict(payload), started_timestamp)
        raise
    payload.update({
        "state": "completed",
        "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "completed_count": completed,
    })
    _write_heartbeat(run_id, dict(payload), started_timestamp)

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
    if isinstance(cron_value, str):
        patterns = cron_value.split(',')
    else:
        patterns = cron_value

    # Split by comma to handle multiple patterns
    for pattern in patterns:
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
