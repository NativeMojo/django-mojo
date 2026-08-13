"""Immutable identity for the function currently executed by JobEngine."""

import contextlib
import contextvars
import re
import uuid


_CURRENT = contextvars.ContextVar("mojo_job_execution", default=None)
_TOKEN = re.compile(r"^[A-Za-z0-9_.:@/-]{1,255}$")


def current():
    value = _CURRENT.get()
    return dict(value) if value is not None else None


def _field(value, label, maximum=255):
    value = str(value or "")
    if len(value) > maximum or not _TOKEN.fullmatch(value):
        raise ValueError(f"job execution {label} is invalid")
    return value


@contextlib.contextmanager
def execution(job_id, function, attempt, channel, runner, broadcast=False):
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
    token = _CURRENT.set(value)
    try:
        yield dict(value)
    finally:
        _CURRENT.reset(token)
