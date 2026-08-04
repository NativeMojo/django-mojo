"""Closure handlers for the ACCOUNT_CLOSURE_HANDLER delegation tests.

`account/deactivate/confirm` runs in the asgi_local server process, so a handler
under test must be importable there by dotted path and must report back out of
process. Each handler writes a JSON record keyed by the user's pk — the handler
contract passes only the user, and every test uses its own disposable user, so
the pk is a sufficient key with no header plumbing.

ACCOUNT_CLOSURE_HANDLER is a GLOBAL setting: while one test has it installed, an
unrelated deactivation running concurrently would reach these handlers too. Every
handler therefore falls through to plain `pii_anonymize()` — the exact unset
behavior — for any user that is not one of ours.

Module name starts with underscore so the testit runner skips it during test
discovery (see testit/runner.py file-discovery filter).
"""
import json
import os
import tempfile

# Usernames the delegation tests create. Only these get the interesting behavior.
TEST_USERNAME_MARKER = "closure_delegation_"

_CAPTURE_DIR = os.path.join(tempfile.gettempdir(), "django_mojo_closure_captures")
os.makedirs(_CAPTURE_DIR, exist_ok=True)


def _file_for(user_id):
    return os.path.join(_CAPTURE_DIR, f"closure_{user_id}.json")


def read_capture(user_id):
    """Return what the handler saw for this user (empty dict if never called)."""
    path = _file_for(user_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def clear_capture(user_id):
    try:
        os.remove(_file_for(user_id))
    except FileNotFoundError:
        pass


def _is_under_test(user):
    return TEST_USERNAME_MARKER in (user.username or "")


def _record(user):
    """Snapshot what the handler can see. Proves it ran while the identity and
    the group memberships were still intact — i.e. before pii_anonymize()."""
    from mojo.apps.account.models.member import GroupMember
    with open(_file_for(user.pk), "w") as fh:
        json.dump({
            "called": True,
            "username": user.username,
            "email": str(user.email),
            "is_active": user.is_active,
            "memberships": GroupMember.objects.filter(user=user).count(),
        }, fh)


def capture_and_anonymize(user):
    """A well-behaved handler: do the product's own purge, anonymize last."""
    if not _is_under_test(user):
        user.pii_anonymize()
        return
    _record(user)
    user.pii_anonymize()


def capture_without_anonymize(user):
    """Records and returns without anonymizing — proves the framework does not
    anonymize behind a configured handler."""
    if not _is_under_test(user):
        user.pii_anonymize()
        return
    _record(user)


def raising(user):
    """Fails closed, with an exception message that must never reach the caller."""
    if not _is_under_test(user):
        user.pii_anonymize()
        return
    _record(user)
    raise RuntimeError(f"closure exploded while purging {user.email}")
