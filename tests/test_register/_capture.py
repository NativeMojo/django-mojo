"""Capture handlers for register-extensibility tests.

Both processes (test runner + server) can import this module via the same
dotted path. Handlers persist invocation data to a JSON file in the system
temp dir so the test process can read what the server-side handler captured.

Module name starts with underscore so the testit runner skips it during
test discovery (see testit/runner.py file-discovery filter).
"""
import json
import os
import tempfile

from mojo import errors as merrors


CAPTURE_FILE = os.path.join(tempfile.gettempdir(), "django_mojo_register_capture.json")


def clear_capture():
    """Test setup helper — wipe the capture file before each scenario."""
    try:
        os.remove(CAPTURE_FILE)
    except FileNotFoundError:
        pass


def read_capture():
    """Test assertion helper — return the dict of captured calls."""
    if not os.path.exists(CAPTURE_FILE):
        return {}
    try:
        with open(CAPTURE_FILE) as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def _append(kind, data):
    state = read_capture()
    state.setdefault(kind, []).append(data)
    with open(CAPTURE_FILE, "w") as fh:
        json.dump(state, fh)


# ---------------------------------------------------------------------------
# Capture handlers — write call data to disk for the test process to read
# ---------------------------------------------------------------------------

def capture_validator(*, email, group, request, extra, **rest):
    """PRE_REGISTER_VALIDATOR capture. Records the kwargs we received.

    Asserting `password` is NOT present is the security-regression guard.
    """
    received = sorted(["email", "group", "request", "extra"] + list(rest.keys()))
    _append("validator", {
        "email": email,
        "group_uuid": str(group.uuid) if group is not None else None,
        "extra": extra or {},
        "kwargs_keys": received,
    })


def capture_register(*, user, request, group, source, extra, **rest):
    """USER_REGISTERED_HANDLER capture."""
    _append("register", {
        "user_id": user.pk,
        "user_email": str(user.email),
        "group_id": group.pk if group is not None else None,
        "group_uuid": str(group.uuid) if group is not None else None,
        "source": source,
        "extra": extra or {},
        "kwargs_keys": sorted(["user", "request", "group", "source", "extra"] + list(rest.keys())),
    })


def capture_login(*, user, request, source, is_new_user, **rest):
    """USER_LOGIN_HANDLER capture."""
    _append("login", {
        "user_id": user.pk,
        "user_email": str(user.email),
        "source": source,
        "is_new_user": is_new_user,
        "kwargs_keys": sorted(["user", "request", "source", "is_new_user"] + list(rest.keys())),
    })


# ---------------------------------------------------------------------------
# Bad handlers — used to verify error contracts
# ---------------------------------------------------------------------------

def reject_validator(*, email, group, request, extra, **rest):
    """Always rejects via ValueException (turns into 400)."""
    raise merrors.ValueException("rejected by test validator")


def raising_register(*, user, request, group, source, extra, **rest):
    """Register handler that always raises — must roll back the user row."""
    raise RuntimeError("test register handler raised")


def raising_login(*, user, request, source, is_new_user, **rest):
    """Login handler that always raises — must be swallowed; login must succeed."""
    raise RuntimeError("test login handler raised")
