"""Handlers for the ACCOUNT_CLOSURE_HANDLER delegation tests.

`run_account_closure` resolves handlers by dotted path, so these must live in a
real importable module rather than being defined inside a test function.

The delegation tests call `run_account_closure` in-process, so a module-level
record is all the reporting these need. In-process is not a shortcut — it is
forced by the fix for the setting itself: ACCOUNT_CLOSURE_HANDLER is read with
`settings.get_static`, i.e. the settings FILE only, so a test cannot plant it in
the DB for the separate asgi_local server process to pick up. That property is
the point, and `test_closure_handler_is_file_only` pins it.

Module name starts with underscore so the testit runner skips it during test
discovery (see testit/runner.py file-discovery filter).
"""

MARKER_USERNAME = "closure-marker-{pk}"

# Every handler invocation, in order. Tests reset() before each run.
CALLS = []


def reset():
    CALLS.clear()


def _record(user):
    """Snapshot what the handler can see. Proves it ran while the identity and
    the group memberships were still intact — i.e. before pii_anonymize()."""
    from mojo.apps.account.models.member import GroupMember
    CALLS.append({
        "user_id": user.pk,
        "username": user.username,
        "email": str(user.email),
        "is_active": user.is_active,
        "memberships": GroupMember.objects.filter(user=user).count(),
    })


def capture_and_anonymize(user):
    """A well-behaved handler: the product's own purge, then anonymize last."""
    _record(user)
    user.pii_anonymize()


def capture_without_anonymize(user):
    """Returns without closing the account — an incomplete closure, which the
    framework must treat as a failure rather than reporting success."""
    _record(user)


def anonymize_then_mark(user):
    """Anonymizes, then stamps a recognisable username. If the framework ran its
    own pii_anonymize() after the handler returned, the marker would be gone."""
    _record(user)
    user.pii_anonymize()
    user.username = MARKER_USERNAME.format(pk=user.pk)
    user.save(update_fields=["username", "modified"])


def raising(user):
    """Fails closed, with an exception message that must never escape."""
    _record(user)
    raise RuntimeError(f"closure exploded while purging {user.email}")
