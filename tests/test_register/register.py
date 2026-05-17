"""End-to-end tests for register-extensibility hooks — fast + parallel.

Uses per-request X-Mojo-Test-* headers instead of th.server_settings(), so
there are no server reloads and no cross-test pollution. Each test uses a
unique capture id, unique email, and unique group so it can run safely
alongside other tests in the same module AND alongside other modules.
"""
import uuid as _uuid
from testit import helpers as th
from tests.test_register import _capture


# Dotted paths the server loads via mojo.helpers.modules.load_function
HANDLER_VALIDATOR_OK = "tests.test_register._capture.capture_validator"
HANDLER_VALIDATOR_REJECT = "tests.test_register._capture.reject_validator"
HANDLER_REGISTER_OK = "tests.test_register._capture.capture_register"
HANDLER_REGISTER_RAISE = "tests.test_register._capture.raising_register"
HANDLER_LOGIN_OK = "tests.test_register._capture.capture_login"
HANDLER_LOGIN_RAISE = "tests.test_register._capture.raising_login"


@th.django_unit_setup()
def setup_register_module(opts):
    """Create per-module shared fixtures with unique names."""
    from mojo.apps.account.models import User
    from mojo.apps.account.models.group import Group

    suffix = _uuid.uuid4().hex[:8]
    opts.test_group_name = f"Register Test Group {suffix}"
    opts.inactive_group_name = f"Register Test Inactive {suffix}"

    grp = Group.objects.create(name=opts.test_group_name, is_active=True)
    opts.test_group_uuid = grp.get_uuid()
    opts.test_group_id = grp.pk

    inactive = Group.objects.create(name=opts.inactive_group_name, is_active=False)
    opts.inactive_group_uuid = inactive.get_uuid()

    # Per-module unique existing user (for login-handler tests)
    opts.existing_email = f"register_existing_{suffix}@register.test"
    opts.existing_password = "RegEx##99pw"
    existing = User.objects.create_user(
        username=opts.existing_email, email=opts.existing_email,
        password=opts.existing_password)
    existing.is_email_verified = True
    existing.requires_mfa = False
    existing.save()
    opts.existing_user_id = existing.pk


def _fresh_email(suffix):
    return f"reg_{suffix}_{_uuid.uuid4().hex[:8]}@register.test"


def _reg_headers(*, capture_id, register_handler=None, login_handler=None,
                  validator=None, extras=None, require_group=None):
    """Build the per-request test-mode header set for a register call.

    `capture_id` is REQUIRED so the capture handlers know where to write.
    """
    h = {
        "X-Mojo-Test-Capture-Id": capture_id,
        # Registration is gated by ALLOW_USER_REGISTRATION; opt in per-request.
        "X-Mojo-Test-Allow-User-Registration": "1",
    }
    if register_handler is not None:
        h["X-Mojo-Test-User-Registered-Handler"] = register_handler
    if login_handler is not None:
        h["X-Mojo-Test-User-Login-Handler"] = login_handler
    if validator is not None:
        h["X-Mojo-Test-Pre-Register-Validator"] = validator
    if extras is not None:
        import json
        h["X-Mojo-Test-Registration-Extra-Fields"] = json.dumps(extras)
    if require_group is not None:
        h["X-Mojo-Test-Require-Group-On-Registration"] = "1" if require_group else "0"
    return h


def _post_register(opts, payload, **header_kwargs):
    from mojo.decorators.limits import clear_rate_limits
    # Tests share IP 127.0.0.1; clear the per-IP register bucket so
    # repeated test calls don't hit strict_rate_limit("register", ip_limit=5).
    clear_rate_limits(ip="127.0.0.1", key="register")
    capture_id = _uuid.uuid4().hex
    _capture.clear_capture(capture_id)
    h = _reg_headers(capture_id=capture_id, **header_kwargs)
    resp = opts.client.post("/api/auth/register", payload, headers=h)
    return resp, capture_id


def _clear_login_limits():
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="login")


# ===========================================================================
# Register flow
# ===========================================================================

@th.django_unit_test("register: no group → user created, no GroupMember, both handlers fire")
def test_register_no_group(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.member import GroupMember

    email = _fresh_email("nogroup")
    resp, capture_id = _post_register(
        opts, {"email": email, "password": "RegPass##99"},
        register_handler=HANDLER_REGISTER_OK,
        login_handler=HANDLER_LOGIN_OK)

    assert resp.status_code == 200, \
        f"register must succeed, got {resp.status_code}: {opts.client.last_response.body}"

    user = User.objects.filter(email=email).first()
    assert user is not None, "user row must exist after register"
    assert not GroupMember.objects.filter(user=user).exists(), \
        "no GroupMember must be created when group is absent"

    captured = _capture.read_capture(capture_id)
    reg_calls = captured.get("register", [])
    login_calls = captured.get("login", [])
    assert len(reg_calls) == 1, \
        f"USER_REGISTERED_HANDLER must fire exactly once, got {len(reg_calls)}"
    assert reg_calls[0]["group_id"] is None, \
        f"register-handler group must be None, got {reg_calls[0]['group_id']}"
    assert reg_calls[0]["source"] == "password", \
        f"register-handler source must be 'password', got {reg_calls[0]['source']}"

    assert len(login_calls) == 1, \
        f"USER_LOGIN_HANDLER must fire exactly once, got {len(login_calls)}"
    assert login_calls[0]["source"] == "password", \
        f"login-handler source must be 'password', got {login_calls[0]['source']}"
    assert login_calls[0]["is_new_user"] is True, \
        f"login-handler is_new_user must be True for fresh register, got {login_calls[0]['is_new_user']}"


@th.django_unit_test("register: display_name must be auto-populated from username (regression)")
def test_register_sets_display_name(opts):
    """Regression: users created via /auth/register must have display_name set.

    The User model auto-generates display_name in on_rest_pre_save(), but the
    register handler calls user.save() directly — bypassing the REST hook.
    Result: registered users have display_name = None.
    """
    from mojo.apps.account.models import User

    email = _fresh_email("displayname")
    resp, _ = _post_register(
        opts, {"email": email, "password": "RegPass##99"})

    assert resp.status_code == 200, \
        f"register must succeed, got {resp.status_code}: {opts.client.last_response.body}"

    user = User.objects.filter(email=email).first()
    assert user is not None, "user row must exist after register"
    assert user.display_name, \
        f"display_name must be populated after register, got {user.display_name!r} for username={user.username!r}"


@th.django_unit_test("register: display_name prefers first+last when both provided")
def test_register_display_name_priority_names(opts):
    from mojo.apps.account.models import User

    email = _fresh_email("priorityname")
    resp, _ = _post_register(opts, {
        "email": email,
        "first_name": "Alice",
        "last_name": "Cooper",
        "password": "RegPass##99",
    })
    assert resp.status_code == 200, \
        f"register must succeed, got {resp.status_code}: {opts.client.last_response.body}"

    user = User.objects.filter(email=email).first()
    assert user is not None, "user row must exist after register"
    assert user.display_name == "Alice Cooper", \
        f"display_name must equal 'Alice Cooper' when first+last provided, got {user.display_name!r}"


@th.django_unit_test("register: display_name falls back to phone number when no names or email")
def test_register_display_name_priority_phone_only(opts):
    """Phone-only register: display_name should equal the normalized phone number."""
    import json
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    # Override schema to require phone (no email, no SMS verify) so we can
    # exercise the phone-identity path without a real verified-phone-token.
    phone_only_schema = json.dumps([
        {"name": "phone", "required": True, "verify": None},
        {"name": "password", "required": True, "verify": None},
    ])

    clear_rate_limits(ip="127.0.0.1", key="register")
    headers = {
        "X-Mojo-Test-Allow-User-Registration": "1",
        "X-Mojo-Test-Register-Fields": phone_only_schema,
    }
    # Build a unique phone number per test run so we don't collide with
    # other tests in this DB.
    import uuid as _uuid
    suffix_digits = _uuid.uuid4().int % 10_000_000
    phone = f"+1555{suffix_digits:07d}"

    resp = opts.client.post(
        "/api/auth/register",
        {"phone": phone, "password": "RegPass##99"},
        headers=headers)
    assert resp.status_code == 200, \
        f"phone-only register must succeed, got {resp.status_code}: {opts.client.last_response.body}"

    user = User.objects.filter(phone_number=phone).first()
    assert user is not None, "user row must exist after phone-only register"
    assert user.display_name == phone, \
        f"display_name must equal phone number {phone!r} when no names or email, got {user.display_name!r}"


@th.django_unit_test("register: business email infers first/last and display_name is built from them")
def test_register_infers_names_from_business_email(opts):
    from mojo.apps.account.models import User

    # Business email (not in CONSUMER_DOMAINS), local part has exactly one dot,
    # both parts >= 2 chars — triggers infer_names_from_email().
    suffix = _uuid.uuid4().hex[:8]
    email = f"john.smith@acme-{suffix}.test"
    resp, _ = _post_register(opts, {"email": email, "password": "RegPass##99"})
    assert resp.status_code == 200, \
        f"register must succeed, got {resp.status_code}: {opts.client.last_response.body}"

    user = User.objects.filter(email=email).first()
    assert user is not None, "user row must exist after register"
    assert user.first_name == "John", \
        f"first_name must be inferred as 'John' from business email, got {user.first_name!r}"
    assert user.last_name == "Smith", \
        f"last_name must be inferred as 'Smith' from business email, got {user.last_name!r}"
    assert user.display_name == "John Smith", \
        f"display_name must be 'John Smith' (built from inferred names), got {user.display_name!r}"


@th.django_unit_test("register: valid active group → user + GroupMember + handler receives group")
def test_register_with_group(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.member import GroupMember

    email = _fresh_email("group")
    resp, capture_id = _post_register(
        opts, {"email": email, "password": "RegPass##99",
               "group_uuid": opts.test_group_uuid},
        register_handler=HANDLER_REGISTER_OK)

    assert resp.status_code == 200, \
        f"register with valid group must succeed, got {resp.status_code}: {opts.client.last_response.body}"

    user = User.objects.filter(email=email).first()
    assert user is not None, "user row must exist after register"
    assert GroupMember.objects.filter(user=user, group_id=opts.test_group_id).exists(), \
        "GroupMember must be created when group is present"

    reg_calls = _capture.read_capture(capture_id).get("register", [])
    assert len(reg_calls) == 1, f"register-handler must fire once, got {len(reg_calls)}"
    assert reg_calls[0]["group_id"] == opts.test_group_id, \
        f"register-handler group_id must match, got {reg_calls[0]['group_id']}"


@th.django_unit_test("register: unknown group UUID → 400, no user, no register-handler fire")
def test_register_unknown_group(opts):
    from mojo.apps.account.models import User

    email = _fresh_email("unkgroup")
    resp, capture_id = _post_register(
        opts, {"email": email, "password": "RegPass##99",
               "group_uuid": "00000000000000000000000000000000"},
        register_handler=HANDLER_REGISTER_OK)

    assert resp.status_code in [400, 422], \
        f"unknown group must return 4xx, got {resp.status_code}: {opts.client.last_response.body}"
    assert not User.objects.filter(email=email).exists(), \
        "no user row must exist after rejected register"
    assert _capture.read_capture(capture_id).get("register", []) == [], \
        "register-handler must not fire on rejected register"


@th.django_unit_test("register: inactive group → 400, no user, no register-handler fire")
def test_register_inactive_group(opts):
    from mojo.apps.account.models import User

    email = _fresh_email("inactive")
    resp, capture_id = _post_register(
        opts, {"email": email, "password": "RegPass##99",
               "group_uuid": opts.inactive_group_uuid},
        register_handler=HANDLER_REGISTER_OK)

    assert resp.status_code in [400, 422], \
        f"inactive group must return 4xx, got {resp.status_code}: {opts.client.last_response.body}"
    assert not User.objects.filter(email=email).exists(), \
        "no user row must exist after rejected register"
    assert _capture.read_capture(capture_id).get("register", []) == [], \
        "register-handler must not fire on rejected register"


@th.django_unit_test("register: allowlisted extras → handler receives them")
def test_register_extras_allowlisted(opts):
    from mojo.apps.account.models import User

    email = _fresh_email("extras")
    resp, capture_id = _post_register(
        opts, {"email": email, "password": "RegPass##99",
               "referral_code": "ABC123", "promo": "SUMMER"},
        register_handler=HANDLER_REGISTER_OK,
        extras=["referral_code", "promo"])

    assert resp.status_code == 200, \
        f"register with allowlisted extras must succeed, got {resp.status_code}: {opts.client.last_response.body}"
    assert User.objects.filter(email=email).exists(), "user must exist"

    reg_calls = _capture.read_capture(capture_id).get("register", [])
    assert len(reg_calls) == 1, f"register-handler must fire once, got {len(reg_calls)}"
    extra = reg_calls[0]["extra"]
    assert extra.get("referral_code") == "ABC123", \
        f"extras must include referral_code='ABC123', got {extra}"
    assert extra.get("promo") == "SUMMER", \
        f"extras must include promo='SUMMER', got {extra}"


@th.django_unit_test("register: non-allowlisted extras → silently dropped, 200 returned")
def test_register_extras_silently_dropped(opts):
    email = _fresh_email("dropped")
    resp, capture_id = _post_register(
        opts, {"email": email, "password": "RegPass##99",
               "evil_admin_flag": True, "is_superuser": True,
               "referral_code": "OK"},
        register_handler=HANDLER_REGISTER_OK,
        extras=["referral_code", "promo"])

    assert resp.status_code == 200, \
        f"register must succeed with unknown extras (silent-drop), got {resp.status_code}: {opts.client.last_response.body}"

    reg_calls = _capture.read_capture(capture_id).get("register", [])
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
    resp, _ = _post_register(
        opts, {"email": email, "password": "RegPass##99"},
        require_group=True)

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
    resp, _ = _post_register(
        opts, {"email": email, "password": "RegPass##99",
               "group_uuid": opts.test_group_uuid},
        register_handler=HANDLER_REGISTER_RAISE)

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

    email = _fresh_email("vrej")
    resp, capture_id = _post_register(
        opts, {"email": email, "password": "RegPass##99"},
        validator=HANDLER_VALIDATOR_REJECT,
        register_handler=HANDLER_REGISTER_OK)

    assert resp.status_code in [400, 422], \
        f"rejecting validator must return 4xx, got {resp.status_code}: {opts.client.last_response.body}"
    assert not User.objects.filter(email=email).exists(), \
        "no user must exist after validator rejection"
    assert _capture.read_capture(capture_id).get("register", []) == [], \
        "register-handler must not fire after validator rejection"


@th.django_unit_test("validator: receives email/group/request/extra — password NOT in kwargs (security guard)")
def test_validator_no_password_in_kwargs(opts):
    email = _fresh_email("vkw")
    resp, capture_id = _post_register(
        opts, {"email": email, "password": "RegPass##99"},
        validator=HANDLER_VALIDATOR_OK)

    assert resp.status_code == 200, \
        f"validator-only register must succeed, got {resp.status_code}: {opts.client.last_response.body}"

    val_calls = _capture.read_capture(capture_id).get("validator", [])
    assert len(val_calls) == 1, f"validator must fire exactly once, got {len(val_calls)}"
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
    resp, _ = _post_register(
        opts, {"email": email, "password": "RegPass##99"},
        register_handler="totally.bogus.module.does_not_exist")

    assert resp.status_code == 200, \
        f"misconfigured handler path must NOT break register, got {resp.status_code}: {opts.client.last_response.body}"
    assert User.objects.filter(email=email).exists(), \
        "user must exist when handler path is broken (treated as no-op)"


@th.django_unit_test("handler error: misconfigured login-handler path → login still succeeds")
def test_misconfigured_login_handler_path(opts):
    _clear_login_limits()
    resp = opts.client.post(
        "/api/auth/login",
        {"username": opts.existing_email, "password": opts.existing_password},
        headers={"X-Mojo-Test-User-Login-Handler": "totally.bogus.module.does_not_exist"})

    assert resp.status_code == 200, \
        f"misconfigured login-handler path must NOT break login, got {resp.status_code}: {opts.client.last_response.body}"


@th.django_unit_test("handler error: login-handler RAISES at runtime → login STILL succeeds (asymmetric guard)")
def test_login_handler_raise_swallowed(opts):
    """Critical asymmetry: login-handler errors must never lock a user out."""
    _clear_login_limits()
    resp = opts.client.post(
        "/api/auth/login",
        {"username": opts.existing_email, "password": opts.existing_password},
        headers={"X-Mojo-Test-User-Login-Handler": HANDLER_LOGIN_RAISE})

    assert resp.status_code == 200, \
        f"login-handler raising must NOT block login, got {resp.status_code}: {opts.client.last_response.body}"


# ===========================================================================
# Source backfill
# ===========================================================================

@th.django_unit_test("source: password login → source='password', is_new_user=False")
def test_login_source_password(opts):
    _clear_login_limits()
    capture_id = _uuid.uuid4().hex
    _capture.clear_capture(capture_id)
    resp = opts.client.post(
        "/api/auth/login",
        {"username": opts.existing_email, "password": opts.existing_password},
        headers={
            "X-Mojo-Test-Capture-Id": capture_id,
            "X-Mojo-Test-User-Login-Handler": HANDLER_LOGIN_OK,
        })

    assert resp.status_code == 200, \
        f"existing-user login must succeed, got {resp.status_code}: {opts.client.last_response.body}"

    login_calls = _capture.read_capture(capture_id).get("login", [])
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
    _clear_login_limits()
    capture_id = _uuid.uuid4().hex

    # First login to get a refresh token
    login = opts.client.post(
        "/api/auth/login",
        {"username": opts.existing_email, "password": opts.existing_password},
        headers={
            "X-Mojo-Test-Capture-Id": capture_id,
            "X-Mojo-Test-User-Login-Handler": HANDLER_LOGIN_OK,
        })
    assert login.status_code == 200, \
        f"login must succeed to obtain refresh_token, got {login.status_code}"
    refresh_token = login.response.data.refresh_token

    # Clear capture, then refresh — login-handler must NOT fire
    _capture.clear_capture(capture_id)
    resp = opts.client.post(
        "/api/auth/token/refresh",
        {"refresh_token": refresh_token},
        headers={
            "X-Mojo-Test-Capture-Id": capture_id,
            "X-Mojo-Test-User-Login-Handler": HANDLER_LOGIN_OK,
        })

    assert resp.status_code == 200, \
        f"refresh must succeed, got {resp.status_code}: {opts.client.last_response.body}"
    assert _capture.read_capture(capture_id).get("login", []) == [], \
        "login-handler MUST NOT fire on /auth/token/refresh"
