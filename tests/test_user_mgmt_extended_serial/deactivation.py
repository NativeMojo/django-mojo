"""
ACCOUNT_CLOSURE_HANDLER delegation for the self-service deactivation flow.

Moved out of tests/test_user_mgmt/deactivation.py (maestro item #1839): these
tests override django.conf.settings (ACCOUNT_CLOSURE_HANDLER) in-process via
setattr/delattr, which is unsafe under the parallel default tier. The token,
request-endpoint, and confirm-endpoint coverage stays in the source module.

These drive run_account_closure() in-process. That is forced, not lazy: the
setting is read with settings.get_static (file only), so there is no DB row a
test could plant for the separate asgi_local server process to read. The unset
path — the no-regression proof — is covered end-to-end by the confirm tests
in the source module, which all run over HTTP.

The handler doubles (`tests.test_user_mgmt._closure_handlers`) stay in the
source package: the dotted handler paths below must keep resolving to the
module the closure service imports.
"""
import contextlib
import uuid as _uuid

from testit import helpers as th
from testit.helpers import assert_true, assert_eq
from tests.test_user_mgmt import _closure_handlers

CLOSURE_SETTING = "ACCOUNT_CLOSURE_HANDLER"
HANDLER_OK = "tests.test_user_mgmt._closure_handlers.capture_and_anonymize"
HANDLER_NO_ANON = "tests.test_user_mgmt._closure_handlers.capture_without_anonymize"
HANDLER_MARK = "tests.test_user_mgmt._closure_handlers.anonymize_then_mark"
HANDLER_RAISES = "tests.test_user_mgmt._closure_handlers.raising"
HANDLER_MISSING = "tests.test_user_mgmt._closure_handlers.no_such_handler"

TEST_PWORD = "deact##mojo99"

_SENTINEL = object()


def _save_conf(name):
    from django.conf import settings as dj_settings
    return getattr(dj_settings, name, _SENTINEL)


def _restore_conf(name, orig):
    from django.conf import settings as dj_settings
    if orig is _SENTINEL:
        if hasattr(dj_settings, name):
            delattr(dj_settings, name)
    else:
        setattr(dj_settings, name, orig)


@contextlib.contextmanager
def _closure_handler(path):
    """Point ACCOUNT_CLOSURE_HANDLER at `path` in django.conf for one test."""
    from django.conf import settings as dj_settings
    orig = _save_conf(CLOSURE_SETTING)
    _closure_handlers.reset()
    setattr(dj_settings, CLOSURE_SETTING, path)
    try:
        yield
    finally:
        _restore_conf(CLOSURE_SETTING, orig)


def _make_closure_user(with_membership=False):
    """A disposable user for one delegation test."""
    from mojo.apps.account.models import User
    from mojo.apps.account.models.group import Group
    from mojo.apps.account.models.member import GroupMember

    suffix = _uuid.uuid4().hex[:8]
    username = f"closure_delegation_{suffix}"
    user = User(username=username, email=f"{username}@example.com")
    user.save()
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save_password(TEST_PWORD)
    user.save()

    if with_membership:
        group = Group.objects.create(name=f"Closure Test Group {suffix}")
        GroupMember.objects.create(user=user, group=group)
    return user


def _run_closure(user):
    from mojo.apps.account.services import closure
    closure.run_account_closure(user)


def _capture_incidents(user):
    """Record report_incident calls made on this user instance.

    Asserted here instead of by querying Event rows. These tests run in the test
    process, where a concurrently-running module can have the incident reporter
    patched out from under them (and reporter._create_event_dict lets an ambient
    authenticated request override an explicit uid). What the contract actually
    requires is that run_account_closure REPORTS the right thing with nothing
    leaky in it — which is exactly what this observes. That the reporter then
    files a real Event is the incident app's own contract, covered by its tests
    and by the account:deactivated assertions in the source module.
    """
    recorded = []

    def _recorder(details, event_type="info", **kwargs):
        recorded.append({"details": details, "event_type": event_type})

    user.report_incident = _recorder
    return recorded


def _expect_closure_failure(user, what):
    """Run the closure expecting it to fail closed. Returns the exception."""
    from mojo import errors as merrors
    from mojo.apps.account.services import closure
    try:
        closure.run_account_closure(user)
    except merrors.ValueException as err:
        assert_eq(err.reason, closure.CLOSURE_FAILED_MESSAGE,
                  f"{what}: caller must get the generic message, got {err.reason!r}")
        return err
    raise AssertionError(f"{what}: expected the closure to fail closed, it returned normally")


@th.django_unit_test("closure delegation: handler owns the closure, sees intact identity")
def test_closure_handler_receives_intact_user(opts):
    user = _make_closure_user(with_membership=True)
    original_username = user.username

    with _closure_handler(HANDLER_OK):
        _run_closure(user)

    calls = _closure_handlers.CALLS
    assert_eq(len(calls), 1, f"Handler should be invoked exactly once, got {len(calls)}")
    seen = calls[0]
    assert_eq(seen["username"], original_username,
              "Handler must run BEFORE anonymisation — it should see the real username")
    assert_true(seen["is_active"], "Handler must see the account still active when it runs")
    assert_true(seen["memberships"] >= 1,
                "Handler must run while GroupMember rows still exist — that is the "
                "whole point of delegating before pii_anonymize() deletes them")

    user.refresh_from_db()
    assert_true(not user.is_active, "Account should be inactive after the handler closed it")
    assert_true(user.username.startswith("deleted-"),
                f"Handler called pii_anonymize(), so username should be anonymised, got {user.username}")


@th.django_unit_test("closure delegation: framework does not anonymize behind a handler")
def test_closure_framework_does_not_anonymize(opts):
    user = _make_closure_user()

    with _closure_handler(HANDLER_MARK):
        _run_closure(user)

    # The handler anonymised and THEN stamped a marker. A framework-side
    # pii_anonymize() after the handler returned would have wiped the marker.
    user.refresh_from_db()
    assert_eq(user.username, _closure_handlers.MARKER_USERNAME.format(pk=user.pk),
              "Framework must NOT anonymize after a handler runs — the handler owns "
              f"the final pii_anonymize(); got username {user.username!r}")


@th.django_unit_test("closure delegation: raising handler fails closed and leaks nothing")
def test_closure_handler_raises_fails_closed(opts):
    user = _make_closure_user()
    original_username = user.username
    original_email = str(user.email)
    incidents = _capture_incidents(user)

    with _closure_handler(HANDLER_RAISES):
        err = _expect_closure_failure(user, "raising handler")

    # `raise ... from None` — without it the REST dispatcher files a second
    # incident carrying traceback.format_exc(), which renders the chained
    # handler exception (and the PII in its message) into a readable Event.
    assert_true(err.__suppress_context__,
                "The handler's exception must be unchained, or the dispatcher's "
                "stack_trace incident republishes its message")
    assert_true(err.__cause__ is None, "No cause should be attached to the generic error")

    user.refresh_from_db()
    assert_true(user.is_active, "Account must stay ACTIVE after a failed closure")
    assert_eq(user.username, original_username,
              "Account must NOT be anonymised after a failed closure")

    assert_eq(len(incidents), 1, f"Expected exactly one incident, got {incidents}")
    assert_eq(incidents[0]["event_type"], "account:closure_failed",
              f"Wrong incident category: {incidents[0]['event_type']}")
    details = incidents[0]["details"]
    assert_true(HANDLER_RAISES in details,
                f"Incident should name the configured handler, got: {details}")
    assert_true("exploded" not in details and original_email not in details,
                f"Incident must record handler name and outcome only, got: {details}")


@th.django_unit_test("closure delegation: handler that skips anonymize is an incomplete closure")
def test_closure_incomplete_is_a_failure(opts):
    user = _make_closure_user()
    original_username = user.username
    incidents = _capture_incidents(user)

    with _closure_handler(HANDLER_NO_ANON):
        _expect_closure_failure(user, "handler that returned without closing")

    assert_eq(len(_closure_handlers.CALLS), 1, "The handler should still have been invoked")

    # The dangerous case: a no-op handler must never earn a success response.
    # Fleet-wide that would mean every closure silently doing nothing while
    # telling each data subject their erasure completed.
    user.refresh_from_db()
    assert_true(user.is_active, "Account must stay ACTIVE when the handler did not close it")
    assert_eq(user.username, original_username, "Account must NOT be anonymised")

    assert_eq(len(incidents), 1, f"Expected exactly one incident, got {incidents}")
    assert_eq(incidents[0]["event_type"], "account:closure_failed",
              f"Wrong incident category: {incidents[0]['event_type']}")
    assert_true("incomplete" in incidents[0]["details"],
                f"Incident should record the incomplete outcome, got: {incidents[0]['details']}")


@th.django_unit_test("closure delegation: unresolvable handler paths fail closed")
def test_closure_handler_bad_path_fails_closed(opts):
    # A missing attribute, a missing module, and a relative path. The last one
    # raises TypeError out of import_module, not ImportError — a narrow except
    # would let it escape as a 500 with no closure incident.
    for label, path in (
            ("missing attribute", HANDLER_MISSING),
            ("missing module", "nope.not_a_real_module.handler"),
            ("relative path", ".relative.path.handler")):
        user = _make_closure_user()
        original_username = user.username
        incidents = _capture_incidents(user)

        with _closure_handler(path):
            _expect_closure_failure(user, label)

        user.refresh_from_db()
        assert_true(user.is_active, f"{label}: account must stay ACTIVE")
        assert_eq(user.username, original_username, f"{label}: account must NOT be anonymised")
        assert_eq(len(incidents), 1, f"{label}: expected one incident, got {incidents}")
        assert_eq(incidents[0]["event_type"], "account:closure_failed",
                  f"{label}: wrong incident category")
        assert_true(path in incidents[0]["details"],
                    f"{label}: incident should name the path, got {incidents[0]['details']}")


@th.django_unit_test("closure delegation: a DB Setting row cannot install a handler (THE regression)")
def test_closure_handler_is_file_only(opts):
    """ACCOUNT_CLOSURE_HANDLER selects which code the worker imports and calls.
    settings.get resolves the DB/Redis Setting plane first, so reading it that
    way would turn `manage_settings` into arbitrary code execution. It is read
    with get_static; this pins that."""
    from mojo.apps.account.models.setting import Setting

    user = _make_closure_user()
    orig = _save_conf(CLOSURE_SETTING)
    _closure_handlers.reset()
    try:
        # Nothing in the file, a handler planted in the DB/Redis plane.
        _restore_conf(CLOSURE_SETTING, _SENTINEL)
        Setting.set(CLOSURE_SETTING, HANDLER_RAISES)

        # It must be ignored entirely: the framework anonymizes directly, which
        # a DB-armed HANDLER_RAISES would otherwise have prevented.
        _run_closure(user)

        assert_eq(len(_closure_handlers.CALLS), 0,
                  "a DB/Redis Setting row must NOT install a closure handler — it is "
                  "file-only config, or manage_settings becomes code execution")
        user.refresh_from_db()
        assert_true(user.username.startswith("deleted-"),
                    f"With no FILE setting the framework anonymizes directly, got {user.username}")
    finally:
        Setting.remove(CLOSURE_SETTING)
        _restore_conf(CLOSURE_SETTING, orig)
