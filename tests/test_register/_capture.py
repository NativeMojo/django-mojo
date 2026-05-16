"""Capture handlers for register-extensibility tests — parallel-safe.

Each test passes a unique `X-Mojo-Test-Capture-Id` header. The capture handler
reads that id and writes invocation data to a per-test file. Tests read their
own file. No global state, fully parallel-safe.

Module name starts with underscore so the testit runner skips it during
test discovery (see testit/runner.py file-discovery filter).
"""
import json
import os
import tempfile
import uuid as _uuid

from mojo import errors as merrors


_CAPTURE_DIR = os.path.join(tempfile.gettempdir(), "django_mojo_register_captures")
os.makedirs(_CAPTURE_DIR, exist_ok=True)


def new_capture_id():
    """Return a fresh capture id; pass via X-Mojo-Test-Capture-Id header."""
    return _uuid.uuid4().hex


def _file_for(capture_id):
    return os.path.join(_CAPTURE_DIR, f"capture_{capture_id}.json")


def read_capture(capture_id):
    """Return the dict of captured calls for this id (empty if none)."""
    path = _file_for(capture_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def clear_capture(capture_id):
    """Remove the capture file for this id."""
    try:
        os.remove(_file_for(capture_id))
    except FileNotFoundError:
        pass


def _append(request, kind, data):
    """Server-side: append data under the test's capture id (from header)."""
    capture_id = None
    if request is not None:
        capture_id = request.META.get("HTTP_X_MOJO_TEST_CAPTURE_ID")
    if not capture_id:
        return  # No id — silently skip (not under test, or test didn't set header)
    path = _file_for(capture_id)
    state = {}
    if os.path.exists(path):
        try:
            with open(path) as fh:
                state = json.load(fh)
        except (json.JSONDecodeError, OSError):
            state = {}
    state.setdefault(kind, []).append(data)
    with open(path, "w") as fh:
        json.dump(state, fh)


# ---------------------------------------------------------------------------
# Capture handlers
# ---------------------------------------------------------------------------

def capture_validator(*, email, group, request, extra, **rest):
    received = sorted(["email", "group", "request", "extra"] + list(rest.keys()))
    password_via_request = None
    try:
        password_via_request = request.DATA.get("password")
    except Exception:
        password_via_request = "__request_data_inaccessible__"
    _append(request, "validator", {
        "email": email,
        "group_uuid": str(group.uuid) if group is not None else None,
        "extra": extra or {},
        "kwargs_keys": received,
        "password_via_request": password_via_request,
    })


def capture_register(*, user, request, group, source, extra, **rest):
    _append(request, "register", {
        "user_id": user.pk,
        "user_email": str(user.email),
        "group_id": group.pk if group is not None else None,
        "group_uuid": str(group.uuid) if group is not None else None,
        "source": source,
        "extra": extra or {},
        "kwargs_keys": sorted(["user", "request", "group", "source", "extra"] + list(rest.keys())),
    })


def capture_login(*, user, request, source, is_new_user, **rest):
    _append(request, "login", {
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
    raise merrors.ValueException("rejected by test validator")


def raising_register(*, user, request, group, source, extra, **rest):
    raise RuntimeError("test register handler raised")


def raising_login(*, user, request, source, is_new_user, **rest):
    raise RuntimeError("test login handler raised")
