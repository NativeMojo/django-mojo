"""Integration tests for the configurable register form (AUTH_REGISTER_FIELDS).

Exercises the phone-as-identity flow, DOB collection + age gate, and the
new verified-phone-token contract end-to-end through the HTTP endpoint.
"""
import datetime
import json
import uuid as _uuid

from testit import helpers as th


PHONE_ONLY_FIELDS = [
    {"name": "first_name", "required": True},
    {"name": "last_name",  "required": True},
    {"name": "phone",      "required": True, "verify": "sms"},
    {"name": "dob",        "required": True},
    {"name": "password",   "required": True},
]


def _clear_register_limits():
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="register")
    clear_rate_limits(ip="127.0.0.1", key="phone_register_start")
    clear_rate_limits(ip="127.0.0.1", key="phone_register_verify")


def _fresh_phone():
    # Use the +1555 reserved range; vary the last 7 digits with a uuid hash
    # so concurrent test runs don't collide on the unique constraint.
    suffix = _uuid.uuid4().hex[:7]
    digits = "".join(c for c in suffix if c.isdigit()).ljust(7, "1")[:7]
    return f"+1555{digits}"


def _register_headers(*, fields=PHONE_ONLY_FIELDS, min_age=None, capture_id=None):
    """Build the per-request test-mode header set for a configurable register.

    `X-Mojo-Test-Register-Fields` is the new per-request override for
    AUTH_REGISTER_FIELDS. The other headers come from the existing
    register-extensibility plumbing.
    """
    h = {
        "X-Mojo-Test-Allow-User-Registration": "1",
        "X-Mojo-Test-Register-Fields": json.dumps(fields),
    }
    if capture_id is not None:
        h["X-Mojo-Test-Capture-Id"] = capture_id
    if min_age is not None:
        # Apply via real settings here (no header override for min-age — it's
        # rare enough that test-time server_settings is fine).
        pass
    return h


def _start_and_verify_phone(opts, phone):
    """Call /phone/register/start, read the code from Redis, call /verify.

    Returns the verified_phone_token usable in /auth/register.
    """
    from mojo.helpers.redis import get_connection

    start = opts.client.post(
        "/api/auth/phone/register/start",
        {"phone": phone})
    assert start.status_code == 200, \
        f"phone-register start must succeed, got {start.status_code}: {opts.client.last_response.body}"
    session_token = start.response.data.session_token

    raw = get_connection().get(f"phone:register:session:{session_token}")
    assert raw is not None, "session must be written to redis"
    code = json.loads(raw)["code"]

    verify = opts.client.post(
        "/api/auth/phone/register/verify",
        {"session_token": session_token, "code": code})
    assert verify.status_code == 200, \
        f"phone-register verify must succeed, got {verify.status_code}: {opts.client.last_response.body}"
    return verify.response.data.verified_phone_token


@th.django_unit_test("phone-only register: full flow creates user with normalized phone username")
def test_phone_only_full_flow(opts):
    from mojo.apps.account.models import User
    _clear_register_limits()

    phone = _fresh_phone()
    verified_token = _start_and_verify_phone(opts, phone)

    today = datetime.date.today()
    dob = today.replace(year=today.year - 25).isoformat()

    resp = opts.client.post(
        "/api/auth/register",
        {"first_name": "Pat",
         "last_name": "Phone",
         "phone": phone,
         "dob": dob,
         "password": "Reg##99Phone",
         "verified_phone_token": verified_token},
        headers=_register_headers())
    assert resp.status_code == 200, \
        f"phone-only register must succeed, got {resp.status_code}: {opts.client.last_response.body}"

    user = User.objects.filter(phone_number=phone).first()
    assert user is not None, \
        f"user must exist after phone-only register (phone={phone})"
    # Username prefers first.last for human-readable handles when both names
    # are present; falls back to the normalized phone on collision.
    assert user.username in ("pat.phone", phone) or user.username.startswith("pat.phone."), \
        f"username must derive from first.last (or fall back to phone) for phone-only identity, got {user.username!r}"
    assert user.is_phone_verified is True, \
        "is_phone_verified must be True when verify=sms is in the schema"
    assert user.first_name == "Pat", \
        f"first_name must be persisted, got {user.first_name!r}"
    assert user.dob == datetime.date.fromisoformat(dob), \
        f"dob must be persisted as a date, got {user.dob!r}"


@th.django_unit_test("phone-only register: missing verified_phone_token → 400")
def test_phone_only_missing_token(opts):
    from mojo.apps.account.models import User
    _clear_register_limits()
    phone = _fresh_phone()
    today = datetime.date.today()
    dob = today.replace(year=today.year - 25).isoformat()

    resp = opts.client.post(
        "/api/auth/register",
        {"first_name": "Pat",
         "last_name": "Phone",
         "phone": phone,
         "dob": dob,
         "password": "Reg##99Phone"},
        headers=_register_headers())
    assert resp.status_code in (400, 422), \
        f"missing verified_phone_token must be 4xx, got {resp.status_code}: {opts.client.last_response.body}"
    assert not User.objects.filter(phone_number=phone).exists(), \
        "no user must be created when phone-verify token is missing"


@th.django_unit_test("phone-only register: bogus verified_phone_token → 400")
def test_phone_only_bad_token(opts):
    from mojo.apps.account.models import User
    _clear_register_limits()
    phone = _fresh_phone()
    today = datetime.date.today()
    dob = today.replace(year=today.year - 25).isoformat()

    resp = opts.client.post(
        "/api/auth/register",
        {"first_name": "Pat",
         "last_name": "Phone",
         "phone": phone,
         "dob": dob,
         "password": "Reg##99Phone",
         "verified_phone_token": "deadbeef" * 4},
        headers=_register_headers())
    assert resp.status_code in (400, 422), \
        f"bogus verified_phone_token must be 4xx, got {resp.status_code}: {opts.client.last_response.body}"
    assert not User.objects.filter(phone_number=phone).exists(), \
        "no user must be created when phone-verify token is invalid"


@th.django_unit_test("phone-only register: token bound to a different phone → 400")
def test_phone_only_token_phone_mismatch(opts):
    from mojo.apps.account.models import User
    _clear_register_limits()
    verified_phone = _fresh_phone()
    other_phone = _fresh_phone()
    verified_token = _start_and_verify_phone(opts, verified_phone)

    today = datetime.date.today()
    dob = today.replace(year=today.year - 25).isoformat()

    resp = opts.client.post(
        "/api/auth/register",
        {"first_name": "Pat",
         "last_name": "Phone",
         "phone": other_phone,  # different from what was verified
         "dob": dob,
         "password": "Reg##99Phone",
         "verified_phone_token": verified_token},
        headers=_register_headers())
    assert resp.status_code in (400, 422), \
        f"phone mismatch must be 4xx, got {resp.status_code}: {opts.client.last_response.body}"
    assert not User.objects.filter(phone_number=other_phone).exists(), \
        "no user must be created when the verified token is bound to a different phone"


@th.django_unit_test("phone-only register: existing phone → 400 duplicate")
def test_phone_only_duplicate(opts):
    from mojo.apps.account.models import User
    _clear_register_limits()
    phone = _fresh_phone()

    # Create a user that already owns this phone
    u = User.objects.create_user(
        username=f"existing_{_uuid.uuid4().hex[:6]}",
        email=f"existing_{_uuid.uuid4().hex[:6]}@dup.test",
        password="Abcd1234!")
    u.phone_number = phone
    u.save()

    try:
        # The phone-register start endpoint itself rejects existing phones
        # before the SMS is even sent.
        start = opts.client.post(
            "/api/auth/phone/register/start",
            {"phone": phone})
        assert start.status_code in (400, 422), \
            f"start must reject existing phone, got {start.status_code}: {opts.client.last_response.body}"
    finally:
        User.objects.filter(phone_number=phone).delete()


@th.django_unit_test("AUTH_MIN_AGE_YEARS gate: DOB below threshold → 400")
def test_min_age_gate_below(opts):
    from mojo.apps.account.models import User
    _clear_register_limits()
    phone = _fresh_phone()
    verified_token = _start_and_verify_phone(opts, phone)

    today = datetime.date.today()
    too_young = today.replace(year=today.year - 10).isoformat()

    headers = _register_headers()
    headers["X-Mojo-Test-Min-Age-Years"] = "13"
    resp = opts.client.post(
        "/api/auth/register",
        {"first_name": "Pat",
         "last_name": "Phone",
         "phone": phone,
         "dob": too_young,
         "password": "Reg##99Phone",
         "verified_phone_token": verified_token},
        headers=headers)
    assert resp.status_code in (400, 422), \
        f"DOB below min-age must be 4xx, got {resp.status_code}: {opts.client.last_response.body}"
    assert not User.objects.filter(phone_number=phone).exists(), \
        "no user must be created when age gate rejects"


@th.django_unit_test("phone-only register: username uses first.last when both names supplied")
def test_phone_only_username_uses_first_last(opts):
    from mojo.apps.account.models import User
    _clear_register_limits()
    phone = _fresh_phone()
    verified_token = _start_and_verify_phone(opts, phone)
    today = datetime.date.today()
    dob = today.replace(year=today.year - 30).isoformat()

    # Use a unique-enough first/last so we don't collide with prior runs.
    suffix = _uuid.uuid4().hex[:6]
    first = f"Alex{suffix}"
    last = "Tester"

    resp = opts.client.post(
        "/api/auth/register",
        {"first_name": first, "last_name": last,
         "phone": phone, "dob": dob,
         "password": "Reg##99Phone",
         "verified_phone_token": verified_token},
        headers=_register_headers())
    assert resp.status_code == 200, \
        f"register must succeed, got {resp.status_code}: {opts.client.last_response.body}"

    user = User.objects.get(phone_number=phone)
    expected = f"{first}.{last}".lower()
    assert user.username == expected, \
        f"username must be `first.last` lowercased for phone-only identity, " \
        f"got {user.username!r} (expected {expected!r})"


@th.django_unit_test("generate_username_from_names: collision falls through to a numeric suffix")
def test_generate_username_from_names_collision(opts):
    from mojo.apps.account.models import User
    # Pre-create a user that owns the bare `first.last` username
    base_first = f"Coll{_uuid.uuid4().hex[:5]}"
    base_last = "Smith"
    base_username = f"{base_first}.{base_last}".lower()
    User.objects.filter(username=base_username).delete()
    u1 = User(username=base_username, email=None)
    u1.phone_number = _fresh_phone()
    u1.set_password("Abcd1234!")
    u1.save()

    # New user with the same first.last must fall through to a suffixed handle
    # (not blow up, not collide).
    new_phone = _fresh_phone()
    u2 = User(email=None)
    u2.phone_number = new_phone
    handle = u2.generate_username_from_names(first_name=base_first, last_name=base_last,
                                             fallback=new_phone)
    assert handle != base_username, \
        f"collision must produce a different username, got {handle!r}"
    assert handle.startswith(base_username + ".") or handle == new_phone, \
        f"collision must produce `{base_username}.<suffix>` or the phone fallback, got {handle!r}"


@th.django_unit_test("generate_username_from_names: empty names fall back to phone")
def test_generate_username_from_names_empty_names(opts):
    from mojo.apps.account.models import User
    phone = _fresh_phone()
    u = User(email=None)
    u.phone_number = phone
    handle = u.generate_username_from_names(first_name="", last_name="", fallback=phone)
    assert handle == phone, \
        f"empty first/last must fall back to the phone fallback, got {handle!r}"


@th.django_unit_test("default register: AUTH_REGISTER_FIELDS unset uses email-based form (regression)")
def test_default_email_register_still_works(opts):
    from mojo.apps.account.models import User
    _clear_register_limits()
    email = f"default_{_uuid.uuid4().hex[:8]}@cfg.test"
    resp = opts.client.post(
        "/api/auth/register",
        {"email": email, "password": "Reg##99Email"},
        headers={"X-Mojo-Test-Allow-User-Registration": "1"})
    assert resp.status_code == 200, \
        f"default email register must still work without the test-fields header, got {resp.status_code}: {opts.client.last_response.body}"
    assert User.objects.filter(email=email).exists(), \
        "user row must exist after default email register"
