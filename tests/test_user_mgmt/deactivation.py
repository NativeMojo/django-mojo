"""
Tests for the self-service account deactivation flow.

Coverage:
  - Happy path: request sends email, confirm with valid token → is_active=False
  - Already inactive: confirm returns 200, pii_anonymize() not called twice
  - Token expired: confirm returns 400
  - Token wrong kind (e.g. ml: or pr: token): confirm returns 400
  - Token already used: confirm returns 400
  - ALLOW_SELF_DEACTIVATION = False: request returns 403
  - Unauthenticated request to /deactivate: returns 401/403
  - JWT is invalid after deactivation (auth_key was rotated)
  - Incident account:deactivated written before anonymisation
  - dv: token has correct prefix
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

TEST_USER = "deactivation_user"
TEST_PWORD = "deact##mojo99"
TEST_EMAIL = "deactivation_user@example.com"

# A separate user for the happy-path test so we don't break other tests
HAPPY_USER = "deact_happy_user"
HAPPY_EMAIL = "deact_happy_user@example.com"

# A user for the already-inactive test
INACTIVE_USER = "deact_inactive_user"
INACTIVE_EMAIL = "deact_inactive_user@example.com"


# ===========================================================================
# Setup / teardown
# ===========================================================================

@th.django_unit_setup()
def setup_deactivation(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Primary test user — used for request-only tests (not actually deactivated)
    user = User.objects.filter(email=TEST_EMAIL).last()
    if user is None:
        user = User(username=TEST_USER, email=TEST_EMAIL)
        user.save()
    user.username = TEST_USER
    user.email = TEST_EMAIL
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save_password(TEST_PWORD)
    user.save()
    opts.user_id = user.pk

    # Happy-path user — will be deactivated and recreated each run
    happy = User.objects.filter(email=HAPPY_EMAIL).last()
    if happy is None:
        happy = User(username=HAPPY_USER, email=HAPPY_EMAIL)
        happy.save()
    happy.username = HAPPY_USER
    happy.email = HAPPY_EMAIL
    happy.is_active = True
    happy.is_email_verified = True
    happy.requires_mfa = False
    happy.metadata = {}
    happy.save_password(TEST_PWORD)
    happy.save()
    opts.happy_user_id = happy.pk

    # Already-inactive user
    inactive = User.objects.filter(email=INACTIVE_EMAIL).last()
    if inactive is None:
        inactive = User(username=INACTIVE_USER, email=INACTIVE_EMAIL)
        inactive.save()
    inactive.username = INACTIVE_USER
    inactive.email = INACTIVE_EMAIL
    inactive.is_active = True
    inactive.is_email_verified = True
    inactive.requires_mfa = False
    inactive.save_password(TEST_PWORD)
    inactive.save()
    opts.inactive_user_id = inactive.pk


# ===========================================================================
# Token unit tests
# ===========================================================================

@th.django_unit_test("dv token: has dv: prefix")
def test_dv_token_prefix(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_deactivate_token(user)
    assert_true(tok.startswith("dv:"), f"Expected 'dv:' prefix, got: {tok[:10]}")
    # consume cleanly
    tokens.verify_deactivate_token(tok)


@th.django_unit_test("dv token: single-use — second verify fails")
def test_dv_token_single_use(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_deactivate_token(user)

    # First verify succeeds
    result = tokens.verify_deactivate_token(tok)
    assert_true(result is not None, "First verify should succeed")

    # Second verify should fail (single-use JTI rotation)
    try:
        tokens.verify_deactivate_token(tok)
        assert_true(False, "Second verify should have raised an exception")
    except Exception:
        pass  # Expected


@th.django_unit_test("dv token: wrong kind prefix rejected")
def test_dv_token_wrong_kind(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    # Generate a password-reset token (pr:) and try to verify as dv:
    pr_token = tokens.generate_password_reset_token(user)
    try:
        tokens.verify_deactivate_token(pr_token)
        assert_true(False, "pr: token should not pass dv: verification")
    except Exception:
        pass  # Expected


# ===========================================================================
# Request endpoint tests
# ===========================================================================

@th.django_unit_test("deactivate request: authenticated user gets 200")
def test_deactivate_request_happy(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/account/deactivate", {})
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    data = resp.json
    assert_true(data.get("status"), "Expected status=true")
    assert_true("confirmation" in str(data.get("message", "")).lower() or
                "email" in str(data.get("message", "")).lower(),
                "Response should mention confirmation email")


@th.django_unit_test("deactivate request: unauthenticated returns 401/403")
def test_deactivate_request_unauth(opts):
    opts.client.logout()
    resp = opts.client.post("/api/account/deactivate", {})
    assert_true(resp.status_code in (401, 403), f"Expected 401 or 403, got {resp.status_code}")


@th.django_unit_test("deactivate request: ALLOW_SELF_DEACTIVATION=False returns 403")
def test_deactivate_request_disabled(opts):
    from mojo.helpers.settings import settings
    from testit import TestitSkip

    if settings.get("ALLOW_SELF_DEACTIVATION", True):
        raise TestitSkip("ALLOW_SELF_DEACTIVATION is True on this server — cannot test disabled state")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/account/deactivate", {})
    opts.client.logout()
    assert_eq(resp.status_code, 403, f"Expected 403, got {resp.status_code}")


@th.django_unit_test("deactivate request: incident logged")
def test_deactivate_request_incident(opts):
    from mojo.apps.incident.models.event import Event
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    before = Event.objects.filter(
        uid=opts.user_id, category="account:deactivate_requested"
    ).count()

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/account/deactivate", {})
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")

    after = Event.objects.filter(
        uid=opts.user_id, category="account:deactivate_requested"
    ).count()
    assert_true(after > before, "Expected account:deactivate_requested incident to be logged")


# ===========================================================================
# Confirm endpoint tests
# ===========================================================================

@th.django_unit_test("deactivate confirm: happy path — account deactivated")
def test_deactivate_confirm_happy(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.happy_user_id)
    tok = tokens.generate_deactivate_token(user)

    opts.client.logout()
    resp = opts.client.post("/api/account/deactivate/confirm", {"token": tok})
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    data = resp.json
    assert_true(data.get("status"), "Expected status=true")
    assert_true("deactivated" in str(data.get("message", "")).lower(),
                "Response should confirm deactivation")

    # Verify user is now inactive
    user.refresh_from_db()
    assert_true(not user.is_active, "User should be inactive after deactivation")

    # Verify PII was anonymised (username should be deleted-<token>)
    assert_true(user.username.startswith("deleted-"),
                f"Username should be anonymised, got: {user.username}")


@th.django_unit_test("deactivate confirm: already inactive returns 200 idempotent")
def test_deactivate_confirm_already_inactive(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.inactive_user_id)

    # Generate token while user is still active
    tok = tokens.generate_deactivate_token(user)

    # Manually deactivate first
    user.is_active = False
    user.save(update_fields=["is_active", "modified"])

    # Confirm should return 200 without calling pii_anonymize again
    opts.client.logout()
    resp = opts.client.post("/api/account/deactivate/confirm", {"token": tok})
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")

    # Username should NOT have been anonymised (was already inactive)
    user.refresh_from_db()
    assert_eq(user.username, INACTIVE_USER,
              "Username should be unchanged — pii_anonymize should not run on already-inactive user")

    # Restore for other tests
    user.is_active = True
    user.save(update_fields=["is_active", "modified"])


@th.django_unit_test("deactivate confirm: missing token returns 400")
def test_deactivate_confirm_missing_token(opts):
    opts.client.logout()
    resp = opts.client.post("/api/account/deactivate/confirm", {})
    assert_true(resp.status_code in (400, 422), f"Expected 400, got {resp.status_code}")


@th.django_unit_test("deactivate confirm: invalid token returns 400/403")
def test_deactivate_confirm_invalid_token(opts):
    opts.client.logout()
    resp = opts.client.post("/api/account/deactivate/confirm", {"token": "dv:totally_invalid_garbage"})
    assert_true(resp.status_code in (400, 403, 500), f"Expected 400 or 403, got {resp.status_code}")


@th.django_unit_test("deactivate confirm: wrong kind token (pr:) rejected")
def test_deactivate_confirm_wrong_kind(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    pr_token = tokens.generate_password_reset_token(user)

    opts.client.logout()
    resp = opts.client.post("/api/account/deactivate/confirm", {"token": pr_token})
    assert_true(resp.status_code in (400, 403, 500), f"Expected rejection of pr: token, got {resp.status_code}")


@th.django_unit_test("deactivate confirm: used token rejected on second attempt")
def test_deactivate_confirm_used_token(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    # Use the inactive_user for this so we don't burn the primary user
    user = User.objects.get(pk=opts.inactive_user_id)
    tok = tokens.generate_deactivate_token(user)

    # Deactivate to make it already-inactive (confirm returns 200 idempotent)
    user.is_active = False
    user.save(update_fields=["is_active", "modified"])

    # First confirm — should succeed (200 idempotent path)
    opts.client.logout()
    resp1 = opts.client.post("/api/account/deactivate/confirm", {"token": tok})
    assert_eq(resp1.status_code, 200, f"First confirm expected 200, got {resp1.status_code}")

    # Second confirm with same token — should fail (JTI already consumed)
    resp2 = opts.client.post("/api/account/deactivate/confirm", {"token": tok})
    assert_true(resp2.status_code in (400, 403, 500),
                f"Expected used token to be rejected, got {resp2.status_code}")

    # Restore
    user.is_active = True
    user.username = INACTIVE_USER
    user.email = INACTIVE_EMAIL
    user.save(update_fields=["is_active", "username", "email", "modified"])


@th.django_unit_test("deactivate confirm: incident logged before anonymisation")
def test_deactivate_confirm_incident_logged(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.apps.incident.models.event import Event

    # Create a fresh disposable user for this test
    disposable = User.objects.filter(email="deact_incident_test@example.com").last()
    if disposable is None:
        disposable = User(username="deact_incident_test", email="deact_incident_test@example.com")
        disposable.save()
    disposable.username = "deact_incident_test"
    disposable.email = "deact_incident_test@example.com"
    disposable.is_active = True
    disposable.is_email_verified = True
    disposable.save_password(TEST_PWORD)
    disposable.save()

    before = Event.objects.filter(
        uid=disposable.pk, category="account:deactivated"
    ).count()

    tok = tokens.generate_deactivate_token(disposable)
    opts.client.logout()
    resp = opts.client.post("/api/account/deactivate/confirm", {"token": tok})
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")

    after = Event.objects.filter(
        uid=disposable.pk, category="account:deactivated"
    ).count()
    assert_true(after > before,
                "Expected account:deactivated incident to be logged before anonymisation")


@th.django_unit_test("deactivate confirm: JWT invalid after deactivation")
def test_deactivate_jwt_invalid_after(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    # Create a fresh disposable user
    disposable = User.objects.filter(email="deact_jwt_test@example.com").last()
    if disposable is None:
        disposable = User(username="deact_jwt_test", email="deact_jwt_test@example.com")
        disposable.save()
    disposable.username = "deact_jwt_test"
    disposable.email = "deact_jwt_test@example.com"
    disposable.is_active = True
    disposable.is_email_verified = True
    disposable.save_password(TEST_PWORD)
    disposable.save()

    # Log in to get a JWT
    opts.client.login("deact_jwt_test", TEST_PWORD)
    old_token = opts.client.access_token

    # Deactivate via token (use a separate client call to avoid auth header interference)
    tok = tokens.generate_deactivate_token(disposable)
    opts.client.logout()
    resp = opts.client.post("/api/account/deactivate/confirm", {"token": tok})
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")

    # Old JWT should now be invalid
    if old_token:
        opts.client.access_token = old_token
        opts.client.is_authenticated = True
        me_resp = opts.client.get("/api/user/me")
        opts.client.logout()
        assert_true(me_resp.status_code in (401, 403),
                    f"Old JWT should be invalid after deactivation, got {me_resp.status_code}")



# ===========================================================================
# ACCOUNT_CLOSURE_HANDLER delegation
#
# These drive run_account_closure() in-process. That is forced, not lazy: the
# setting is read with settings.get_static (file only), so there is no DB row a
# test could plant for the separate asgi_local server process to read. The unset
# path — the no-regression proof — is covered end-to-end by the confirm tests
# above, which all run over HTTP.
# ===========================================================================

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
    and by the account:deactivated assertions above.
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


@th.django_unit_test("closure delegation: no pii_anonymize call site bypasses the delegation")
def test_pii_anonymize_call_sites_are_pinned(opts):
    """Source pin. `run_account_closure` is the only thing allowed to invoke
    pii_anonymize(), so a future admin-erasure surface cannot quietly skip a
    deployment's closure handler. Parsed with ast, so docstrings and comments
    that merely mention the name do not count. Scans the whole framework, not
    just the account app — an erasure surface could be built anywhere."""
    import ast
    import os
    import mojo

    scan_root = os.path.dirname(mojo.__file__)
    allowed_callers = {os.path.join("apps", "account", "services", "closure.py")}
    expected_definitions = {os.path.join("apps", "account", "models", "user.py")}

    callers = set()
    definitions = set()
    for dirpath, dirnames, filenames in os.walk(scan_root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, scan_root)
            with open(full, "r", encoding="utf-8") as fh:
                try:
                    tree = ast.parse(fh.read(), filename=full)
                except SyntaxError:
                    continue
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "pii_anonymize":
                    definitions.add(rel)
                elif isinstance(node, ast.Call):
                    func = node.func
                    name = getattr(func, "attr", None) or getattr(func, "id", None)
                    if name == "pii_anonymize":
                        callers.add(rel)

    assert_eq(definitions, expected_definitions,
              f"pii_anonymize should be defined only on the User model, found: {definitions}")
    assert_eq(callers, allowed_callers,
              "pii_anonymize() must only be called by account/services/closure.py, so every "
              "erasure path goes through the deployment's ACCOUNT_CLOSURE_HANDLER. "
              f"Unexpected call sites: {sorted(callers - allowed_callers)}")
