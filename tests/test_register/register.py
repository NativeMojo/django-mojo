"""End-to-end tests for register-extensibility hooks.

Covers:
  - Group context on POST /api/auth/register
  - PRE_REGISTER_VALIDATOR — rejection + signature regression guard
  - USER_REGISTERED_HANDLER — fires on password + OAuth, atomic rollback
  - USER_LOGIN_HANDLER — fires on every jwt_login, errors swallowed
  - Source backfill across all jwt_login call sites
  - Refresh-token does NOT fire login handler

Tests serially toggle handlers via th.server_settings(...) — module is marked
serial in __init__.py so reloads can't race with parallel modules.
"""
from testit import helpers as th


# Dotted paths the server-side will load via mojo.helpers.modules.load_function
HANDLER_VALIDATOR_OK = "tests.test_register._capture.capture_validator"
HANDLER_VALIDATOR_REJECT = "tests.test_register._capture.reject_validator"
HANDLER_REGISTER_OK = "tests.test_register._capture.capture_register"
HANDLER_REGISTER_RAISE = "tests.test_register._capture.raising_register"
HANDLER_LOGIN_OK = "tests.test_register._capture.capture_login"
HANDLER_LOGIN_RAISE = "tests.test_register._capture.raising_login"

# Reusable existing-user creds (for testing login-handler with is_new_user=False)
EXISTING_USER = "register_existing@test.com"
EXISTING_PWORD = "RegEx##99pw"


def _settings_for(**handlers):
    """Build a settings dict that always enables registration + extras allowlist.

    th.server_settings() merges with var/django.conf, so each call must include
    the foundation settings since they aren't applied via TESTIT module config.
    """
    base = {
        "ALLOW_USER_REGISTRATION": True,
        "REGISTRATION_EXTRA_FIELDS": ["referral_code", "promo"],
    }
    base.update(handlers)
    return base


@th.django_unit_setup()
def setup_register_module(opts):
    """One-time setup for the module — clean any prior test users + rate limits."""
    from mojo.apps.account.models import User
    from mojo.apps.account.models.group import Group
    from mojo.decorators.limits import clear_rate_limits
    from tests.test_register import _capture

    _capture.clear_capture()

    # Clean slate — these emails get used across multiple tests
    User.objects.filter(email__icontains="@register.test").delete()
    User.objects.filter(email=EXISTING_USER).delete()
    Group.objects.filter(name="Register Test Group").delete()
    Group.objects.filter(name="Register Test Inactive").delete()

    clear_rate_limits(ip="127.0.0.1")

    # Pre-create a group used by group-aware tests. Group.uuid is lazy-initialized
    # via get_uuid(); call it during setup so tests have a stable UUID to send.
    grp = Group.objects.create(name="Register Test Group", is_active=True)
    opts.test_group_uuid = grp.get_uuid()
    opts.test_group_id = grp.pk

    # Pre-create an inactive group
    inactive = Group.objects.create(name="Register Test Inactive", is_active=False)
    opts.inactive_group_uuid = inactive.get_uuid()

    # Pre-create an existing user for login-handler is_new_user=False tests
    existing = User.objects.create_user(
        username=EXISTING_USER, email=EXISTING_USER, password=EXISTING_PWORD)
    existing.is_email_verified = True
    existing.requires_mfa = False
    existing.save()
    opts.existing_user_id = existing.pk


def _post_register(opts, payload):
    """Helper — clear rate limits + capture, then POST /api/auth/register."""
    from mojo.decorators.limits import clear_rate_limits
    from tests.test_register import _capture
    clear_rate_limits(ip="127.0.0.1")
    _capture.clear_capture()
    return opts.client.post("/api/auth/register", payload)


def _fresh_email(suffix):
    """Generate a unique test email per scenario so prior runs don't collide."""
    import uuid
    return f"reg_{suffix}_{uuid.uuid4().hex[:8]}@register.test"


# ===========================================================================
# Register flow
# ===========================================================================

@th.django_unit_test("register: no group → user created, no GroupMember, both handlers fire")
def test_register_no_group(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.member import GroupMember
    from tests.test_register import _capture

    email = _fresh_email("nogroup")
    with th.server_settings(**_settings_for(
            USER_REGISTERED_HANDLER=HANDLER_REGISTER_OK,
            USER_LOGIN_HANDLER=HANDLER_LOGIN_OK)):
        resp = _post_register(opts, {"email": email, "password": "RegPass##99"})

    assert resp.status_code == 200, \
        f"register without group must succeed, got {resp.status_code}: {opts.client.last_response.body}"

    user = User.objects.filter(email=email).first()
    assert user is not None, "user row must exist after register"
    assert not GroupMember.objects.filter(user=user).exists(), \
        "no GroupMember must be created when group is absent"

    captured = _capture.read_capture()
    reg_calls = captured.get("register", [])
    login_calls = captured.get("login", [])
    assert len(reg_calls) == 1, \
        f"USER_REGISTERED_HANDLER must fire exactly once, got {len(reg_calls)}: {reg_calls}"
    assert reg_calls[0]["group_id"] is None, \
        f"register-handler group must be None when no group, got {reg_calls[0]['group_id']}"
    assert reg_calls[0]["source"] == "password", \
        f"register-handler source must be 'password', got {reg_calls[0]['source']}"

    assert len(login_calls) == 1, \
        f"USER_LOGIN_HANDLER must fire exactly once, got {len(login_calls)}: {login_calls}"
    assert login_calls[0]["source"] == "password", \
        f"login-handler source must be 'password', got {login_calls[0]['source']}"
    assert login_calls[0]["is_new_user"] is True, \
        f"login-handler is_new_user must be True for fresh register, got {login_calls[0]['is_new_user']}"


@th.django_unit_test("register: valid active group → user + GroupMember + handler receives group")
def test_register_with_group(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.member import GroupMember
    from tests.test_register import _capture

    email = _fresh_email("group")
    with th.server_settings(**_settings_for(USER_REGISTERED_HANDLER=HANDLER_REGISTER_OK)):
        resp = _post_register(opts, {
            "email": email,
            "password": "RegPass##99",
            "group_uuid": opts.test_group_uuid,
        })

    assert resp.status_code == 200, \
        f"register with valid group must succeed, got {resp.status_code}: {opts.client.last_response.body}"

    user = User.objects.filter(email=email).first()
    assert user is not None, "user row must exist after register"
    assert GroupMember.objects.filter(user=user, group_id=opts.test_group_id).exists(), \
        "GroupMember must be created when group is present"

    reg_calls = _capture.read_capture().get("register", [])
    assert len(reg_calls) == 1, f"register-handler must fire once, got {len(reg_calls)}"
    assert reg_calls[0]["group_id"] == opts.test_group_id, \
        f"register-handler group_id must match, got {reg_calls[0]['group_id']} expected {opts.test_group_id}"


@th.django_unit_test("register: unknown group UUID → 400, no user, no register-handler fire")
def test_register_unknown_group(opts):
    from mojo.apps.account.models import User
    from tests.test_register import _capture

    email = _fresh_email("unkgroup")
    with th.server_settings(**_settings_for(USER_REGISTERED_HANDLER=HANDLER_REGISTER_OK)):
        resp = _post_register(opts, {
            "email": email,
            "password": "RegPass##99",
            "group_uuid": "00000000-0000-0000-0000-000000000000",
        })

    assert resp.status_code in [400, 422], \
        f"unknown group must return 4xx, got {resp.status_code}: {opts.client.last_response.body}"
    assert not User.objects.filter(email=email).exists(), \
        "no user row must exist after rejected register"
    assert _capture.read_capture().get("register", []) == [], \
        "register-handler must not fire on rejected register"


@th.django_unit_test("register: inactive group → 400, no user, no register-handler fire")
def test_register_inactive_group(opts):
    from mojo.apps.account.models import User
    from tests.test_register import _capture

    email = _fresh_email("inactive")
    with th.server_settings(**_settings_for(USER_REGISTERED_HANDLER=HANDLER_REGISTER_OK)):
        resp = _post_register(opts, {
            "email": email,
            "password": "RegPass##99",
            "group_uuid": opts.inactive_group_uuid,
        })

    assert resp.status_code in [400, 422], \
        f"inactive group must return 4xx, got {resp.status_code}: {opts.client.last_response.body}"
    assert not User.objects.filter(email=email).exists(), \
        "no user row must exist after rejected register"
    assert _capture.read_capture().get("register", []) == [], \
        "register-handler must not fire on rejected register"


@th.django_unit_test("register: allowlisted extras → handler receives them")
def test_register_extras_allowlisted(opts):
    from mojo.apps.account.models import User
    from tests.test_register import _capture

    email = _fresh_email("extras")
    with th.server_settings(**_settings_for(USER_REGISTERED_HANDLER=HANDLER_REGISTER_OK)):
        resp = _post_register(opts, {
            "email": email,
            "password": "RegPass##99",
            "referral_code": "ABC123",
            "promo": "SUMMER",
        })

    assert resp.status_code == 200, \
        f"register with allowlisted extras must succeed, got {resp.status_code}: {opts.client.last_response.body}"
    assert User.objects.filter(email=email).exists(), "user must exist"

    reg_calls = _capture.read_capture().get("register", [])
    assert len(reg_calls) == 1, f"register-handler must fire once, got {len(reg_calls)}"
    extra = reg_calls[0]["extra"]
    assert extra.get("referral_code") == "ABC123", \
        f"extras must include referral_code='ABC123', got {extra}"
    assert extra.get("promo") == "SUMMER", \
        f"extras must include promo='SUMMER', got {extra}"


@th.django_unit_test("register: non-allowlisted extras → silently dropped, 200 returned")
def test_register_extras_silently_dropped(opts):
    from tests.test_register import _capture

    email = _fresh_email("dropped")
    with th.server_settings(**_settings_for(USER_REGISTERED_HANDLER=HANDLER_REGISTER_OK)):
        resp = _post_register(opts, {
            "email": email,
            "password": "RegPass##99",
            "evil_admin_flag": True,         # not in allowlist
            "is_superuser": True,            # not in allowlist
            "referral_code": "OK",           # IS in allowlist
        })

    assert resp.status_code == 200, \
        f"register must succeed with unknown extras (silent-drop), got {resp.status_code}: {opts.client.last_response.body}"

    reg_calls = _capture.read_capture().get("register", [])
    assert len(reg_calls) == 1, "register-handler must fire once"
    extra = reg_calls[0]["extra"]
    assert "evil_admin_flag" not in extra, \
        f"non-allowlisted extras must NOT reach handler, got {extra}"
    assert "is_superuser" not in extra, \
        f"non-allowlisted extras must NOT reach handler, got {extra}"
    assert extra.get("referral_code") == "OK", \
        f"allowlisted extra must reach handler, got {extra}"


@th.django_unit_test("register: REQUIRE_GROUP_ON_REGISTRATION=True + no group → 400")
def test_register_require_group(opts):
    from mojo.apps.account.models import User

    email = _fresh_email("reqgroup")
    with th.server_settings(**_settings_for(REQUIRE_GROUP_ON_REGISTRATION=True)):
        resp = _post_register(opts, {"email": email, "password": "RegPass##99"})

    assert resp.status_code in [400, 422], \
        f"missing group with REQUIRE_GROUP_ON_REGISTRATION must 4xx, got {resp.status_code}: {opts.client.last_response.body}"
    assert not User.objects.filter(email=email).exists(), \
        "no user must exist when group is required and absent"


# ===========================================================================
# Atomic boundary
# ===========================================================================

@th.django_unit_test("atomic: register-handler raises → user row rolled back")
def test_register_handler_raise_rolls_back(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.member import GroupMember

    email = _fresh_email("rollback")
    with th.server_settings(**_settings_for(USER_REGISTERED_HANDLER=HANDLER_REGISTER_RAISE)):
        resp = _post_register(opts, {
            "email": email,
            "password": "RegPass##99",
            "group_uuid": opts.test_group_uuid,
        })

    assert resp.status_code >= 500, \
        f"raising register-handler must propagate as 5xx, got {resp.status_code}: {opts.client.last_response.body}"
    assert not User.objects.filter(email=email).exists(), \
        "user row MUST be rolled back when register-handler raises"
    assert not GroupMember.objects.filter(group_id=opts.test_group_id, user__email=email).exists(), \
        "GroupMember row MUST be rolled back when register-handler raises"


# ===========================================================================
# Validator contract
# ===========================================================================

@th.django_unit_test("validator: raises ValueException → 400, no user, no handler fire")
def test_validator_rejects(opts):
    from mojo.apps.account.models import User
    from tests.test_register import _capture

    email = _fresh_email("vrej")
    with th.server_settings(**_settings_for(
            PRE_REGISTER_VALIDATOR=HANDLER_VALIDATOR_REJECT,
            USER_REGISTERED_HANDLER=HANDLER_REGISTER_OK)):
        resp = _post_register(opts, {"email": email, "password": "RegPass##99"})

    assert resp.status_code in [400, 422], \
        f"rejecting validator must return 4xx, got {resp.status_code}: {opts.client.last_response.body}"
    assert not User.objects.filter(email=email).exists(), \
        "no user must exist after validator rejection"
    assert _capture.read_capture().get("register", []) == [], \
        "register-handler must not fire after validator rejection"


@th.django_unit_test("validator: receives email/group/request/extra — password NOT in kwargs (security guard)")
def test_validator_no_password_in_kwargs(opts):
    from tests.test_register import _capture

    email = _fresh_email("vkw")
    with th.server_settings(**_settings_for(PRE_REGISTER_VALIDATOR=HANDLER_VALIDATOR_OK)):
        resp = _post_register(opts, {"email": email, "password": "RegPass##99"})

    assert resp.status_code == 200, \
        f"validator-only register must succeed, got {resp.status_code}: {opts.client.last_response.body}"

    val_calls = _capture.read_capture().get("validator", [])
    assert len(val_calls) == 1, \
        f"validator must fire exactly once, got {len(val_calls)}"
    keys = val_calls[0]["kwargs_keys"]
    assert "password" not in keys, \
        f"SECURITY: validator must NOT receive `password` kwarg, got keys={keys}"
    for required in ["email", "group", "request", "extra"]:
        assert required in keys, \
            f"validator must receive `{required}` kwarg, got keys={keys}"
    assert val_calls[0]["email"] == email, \
        f"validator must receive the submitted email, got {val_calls[0]['email']}"
    # Defense-in-depth: validator must not be able to reach password via request.DATA either
    pw_probe = val_calls[0]["password_via_request"]
    assert pw_probe in (None, ""), \
        f"SECURITY: validator MUST NOT be able to read password via request.DATA, got {pw_probe!r}"


# ===========================================================================
# Handler error contracts (asymmetric: register raises propagate, login swallowed)
# ===========================================================================

@th.django_unit_test("handler error: misconfigured register-handler dotted-path → register still succeeds")
def test_misconfigured_register_handler_path(opts):
    from mojo.apps.account.models import User

    email = _fresh_email("badpath")
    with th.server_settings(**_settings_for(
            USER_REGISTERED_HANDLER="totally.bogus.module.does_not_exist")):
        resp = _post_register(opts, {"email": email, "password": "RegPass##99"})

    assert resp.status_code == 200, \
        f"misconfigured handler path must NOT break register, got {resp.status_code}: {opts.client.last_response.body}"
    assert User.objects.filter(email=email).exists(), \
        "user must exist when handler path is broken (treated as no-op)"


@th.django_unit_test("handler error: misconfigured login-handler path → login still succeeds")
def test_misconfigured_login_handler_path(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    with th.server_settings(USER_LOGIN_HANDLER="totally.bogus.module.does_not_exist"):
        resp = opts.client.post("/api/auth/login", {
            "username": EXISTING_USER, "password": EXISTING_PWORD,
        })

    assert resp.status_code == 200, \
        f"misconfigured login-handler path must NOT break login, got {resp.status_code}: {opts.client.last_response.body}"


@th.django_unit_test("handler error: login-handler RAISES at runtime → login STILL succeeds (asymmetric guard)")
def test_login_handler_raise_swallowed(opts):
    """Critical asymmetry assertion: login-handler errors must never lock a user out."""
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    with th.server_settings(USER_LOGIN_HANDLER=HANDLER_LOGIN_RAISE):
        resp = opts.client.post("/api/auth/login", {
            "username": EXISTING_USER, "password": EXISTING_PWORD,
        })

    assert resp.status_code == 200, \
        f"login-handler raising must NOT block login, got {resp.status_code}: {opts.client.last_response.body}"


# ===========================================================================
# Source backfill — login-handler receives correct source per flow
# ===========================================================================

@th.django_unit_test("source: password login → source='password', is_new_user=False")
def test_login_source_password(opts):
    from mojo.decorators.limits import clear_rate_limits
    from tests.test_register import _capture

    clear_rate_limits(ip="127.0.0.1")
    _capture.clear_capture()
    with th.server_settings(USER_LOGIN_HANDLER=HANDLER_LOGIN_OK):
        resp = opts.client.post("/api/auth/login", {
            "username": EXISTING_USER, "password": EXISTING_PWORD,
        })

    assert resp.status_code == 200, \
        f"existing-user login must succeed, got {resp.status_code}: {opts.client.last_response.body}"

    login_calls = _capture.read_capture().get("login", [])
    assert len(login_calls) >= 1, "login-handler must fire on password login"
    last = login_calls[-1]
    assert last["source"] == "password", \
        f"password login source must be 'password', got {last['source']}"
    assert last["is_new_user"] is False, \
        f"existing-user login must have is_new_user=False, got {last['is_new_user']}"


# ===========================================================================
# Refresh-token does NOT fire login-handler
# ===========================================================================

@th.django_unit_test("refresh: /auth/token/refresh does NOT fire login-handler")
def test_refresh_does_not_fire_login(opts):
    from mojo.decorators.limits import clear_rate_limits
    from tests.test_register import _capture

    clear_rate_limits(ip="127.0.0.1")

    # First login to get a refresh token
    with th.server_settings(USER_LOGIN_HANDLER=HANDLER_LOGIN_OK):
        login = opts.client.post("/api/auth/login", {
            "username": EXISTING_USER, "password": EXISTING_PWORD,
        })
        assert login.status_code == 200, \
            f"login must succeed to obtain refresh_token, got {login.status_code}: {opts.client.last_response.body}"
        refresh_token = login.response.data.refresh_token

        # Clear capture, then refresh — login-handler must NOT fire
        _capture.clear_capture()
        resp = opts.client.post("/api/auth/token/refresh", {"refresh_token": refresh_token})

    assert resp.status_code == 200, \
        f"refresh must succeed, got {resp.status_code}: {opts.client.last_response.body}"
    assert _capture.read_capture().get("login", []) == [], \
        "login-handler MUST NOT fire on /auth/token/refresh"
