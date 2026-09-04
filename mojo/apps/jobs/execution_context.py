"""Immutable identity for the function currently executed by JobEngine."""

import contextlib
import contextvars
import re
import uuid


_CURRENT = contextvars.ContextVar("mojo_job_execution", default=None)
_TOKEN = re.compile(r"^[A-Za-z0-9_.:@/-]{1,255}$")
_STARTED = re.compile(r"^[0-9T:.+\-Z]{1,96}$")


def current():
    value = _CURRENT.get()
    if value is None:
        return None
    result = dict(value)
    result.pop("_runner_started", None)
    return result


def current_runner_incarnation():
    """Return immutable runner-heartbeat identity for this execution."""
    value = _CURRENT.get()
    if value is None:
        return None
    started = value.get("_runner_started")
    if not isinstance(started, str) or not started:
        return None
    return {"runner_id": value["runner"], "started": started}


def _field(value, label, maximum=255):
    value = str(value or "")
    if len(value) > maximum or not _TOKEN.fullmatch(value):
        raise ValueError(f"job execution {label} is invalid")
    return value


@contextlib.contextmanager
def execution(job_id, function, attempt, channel, runner, broadcast=False,
              runner_started=None):
    if _CURRENT.get() is not None:
        raise RuntimeError("nested job execution context is forbidden")
    if (not isinstance(attempt, int) or isinstance(attempt, bool) or
            not 0 <= attempt <= 1000000):
        raise ValueError("job execution attempt is invalid")
    if not isinstance(broadcast, bool):
        raise ValueError("job execution broadcast flag is invalid")
    value = {
        "execution_id": uuid.uuid4().hex,
        "job_id": _field(job_id, "job id"),
        "function": _field(function, "function"),
        "attempt": attempt,
        "channel": _field(channel, "channel", 128),
        "runner": _field(runner, "runner", 128),
        "broadcast": broadcast,
    }
    if runner_started is not None:
        if not isinstance(runner_started, str) or not _STARTED.fullmatch(
                runner_started):
            raise ValueError("job execution runner start is invalid")
        value["_runner_started"] = runner_started
    token = _CURRENT.set(value)
    try:
        yield dict(value)
    finally:
        _CURRENT.reset(token)
