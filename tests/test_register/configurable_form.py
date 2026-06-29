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
    # 7 fully-random digits (10M space) so parallel tests don't collide on the
    # same +1555 number — a collision pollutes account_exists / lookup checks.
    return f"+1555{_uuid.uuid4().int % 10_000_000:07d}"


@th.django_unit_test("_fresh_phone yields effectively-unique numbers (no cross-test collisions)")
def test_fresh_phone_is_unique(opts):
    """Regression (ITEM-007): _fresh_phone must have enough entropy that parallel
    tests don't collide on the same +1555 number — a collision pollutes the
    account_exists check in test_phone_register_verify_account_exists. The old impl
    extracted only the digits from 7 hex chars and padded with '1', leaving a tiny,
    collision-prone space."""
    phones = {_fresh_phone() for _ in range(1000)}
    assert len(phones) >= 995, (
        f"_fresh_phone must be effectively unique across calls — got only "
        f"{len(phones)}/1000 unique. Low entropy causes cross-test phone collisions "
        f"under -j parallel execution.")


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


@th.django_unit_test("phone register: an SMS-verified existing phone signs into that account")
def test_phone_existing_logs_in(opts):
    from mojo.apps.account.models import User
    _clear_register_limits()
    phone = _fresh_phone()

    existing = User.objects.create_user(
        username=f"existing_{_uuid.uuid4().hex[:6]}",
        email=f"existing_{_uuid.uuid4().hex[:8]}@dup.test",
        password="Abcd1234!")
    existing.first_name = "Original"
    existing.phone_number = phone
    existing.save()
    existing_id = existing.pk
    try:
        token = _start_and_verify_phone(opts, phone)
        # Phone-only payload — no profile fields (the form skips its step 3).
        resp = opts.client.post(
            "/api/auth/register",
            {"phone": phone, "verified_phone_token": token},
            headers=_register_headers())
        assert resp.status_code == 200, \
            f"an SMS-verified existing phone must sign in with no profile " \
            f"fields, got {resp.status_code}: {opts.client.last_response.body}"
        users = list(User.objects.filter(phone_number=phone))
        assert len(users) == 1, \
            f"no duplicate account may be created, found {len(users)}"
        assert users[0].pk == existing_id, \
            "the response must sign into the pre-existing account"
        assert users[0].first_name == "Original", \
            "the existing account profile must not be altered"
        assert bool(resp.response.data.access_token), \
            "an access token must be issued for the existing account"
    finally:
        User.objects.filter(phone_number=phone).delete()


@th.django_unit_test("phone register: existing user joins a new group + USER_REGISTERED_HANDLER fires")
def test_phone_existing_joins_new_group(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.account.models.member import GroupMember
    from tests.test_register import _capture
    _clear_register_limits()
    phone = _fresh_phone()
    group_uuid = _uuid.uuid4().hex

    existing = User.objects.create_user(
        username=f"existing_{_uuid.uuid4().hex[:6]}",
        email=f"existing_{_uuid.uuid4().hex[:8]}@grp.test",
        password="Abcd1234!")
    existing.phone_number = phone
    existing.save()
    group = Group.objects.create(
        name=f"grp-{group_uuid[:8]}", uuid=group_uuid, is_active=True)
    capture_id = _capture.new_capture_id()
    _capture.clear_capture(capture_id)
    try:
        token = _start_and_verify_phone(opts, phone)
        headers = _register_headers(capture_id=capture_id)
        headers["X-Mojo-Test-User-Registered-Handler"] = \
            "tests.test_register._capture.capture_register"
        resp = opts.client.post(
            "/api/auth/register",
            {"phone": phone, "verified_phone_token": token, "group_uuid": group_uuid},
            headers=headers)
        assert resp.status_code == 200, \
            f"existing user joining a new group must succeed, got " \
            f"{resp.status_code}: {opts.client.last_response.body}"
        assert GroupMember.objects.filter(user=existing, group=group).exists(), \
            "the existing user must be added to the new group"
        reg_calls = _capture.read_capture(capture_id).get("register", [])
        assert len(reg_calls) == 1, \
            f"USER_REGISTERED_HANDLER must fire once for the new group, got {reg_calls}"
        assert reg_calls[0]["group_uuid"] == group_uuid, \
            f"the handler must receive the new group, got {reg_calls[0].get('group_uuid')}"
    finally:
        _capture.clear_capture(capture_id)
        User.objects.filter(phone_number=phone).delete()
        Group.objects.filter(uuid=group_uuid).delete()


@th.django_unit_test("phone register: existing user already in the group → login only, no handler")
def test_phone_existing_already_member(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.account.models.member import GroupMember
    from tests.test_register import _capture
    _clear_register_limits()
    phone = _fresh_phone()
    group_uuid = _uuid.uuid4().hex

    existing = User.objects.create_user(
        username=f"existing_{_uuid.uuid4().hex[:6]}",
        email=f"existing_{_uuid.uuid4().hex[:8]}@mem.test",
        password="Abcd1234!")
    existing.phone_number = phone
    existing.save()
    group = Group.objects.create(
        name=f"grp-{group_uuid[:8]}", uuid=group_uuid, is_active=True)
    GroupMember.objects.create(user=existing, group=group)
    capture_id = _capture.new_capture_id()
    _capture.clear_capture(capture_id)
    try:
        token = _start_and_verify_phone(opts, phone)
        headers = _register_headers(capture_id=capture_id)
        headers["X-Mojo-Test-User-Registered-Handler"] = \
            "tests.test_register._capture.capture_register"
        resp = opts.client.post(
            "/api/auth/register",
            {"phone": phone, "verified_phone_token": token, "group_uuid": group_uuid},
            headers=headers)
        assert resp.status_code == 200, \
            f"an existing member must still sign in, got {resp.status_code}: " \
            f"{opts.client.last_response.body}"
        assert not _capture.read_capture(capture_id).get("register"), \
            "USER_REGISTERED_HANDLER must NOT fire when the user is already a member"
    finally:
        _capture.clear_capture(capture_id)
        User.objects.filter(phone_number=phone).delete()
        Group.objects.filter(uuid=group_uuid).delete()


@th.django_unit_test("phone/register/verify reports account_exists")
def test_phone_register_verify_account_exists(opts):
    from mojo.apps.account.models import User
    from mojo.helpers.redis import get_connection
    _clear_register_limits()

    # A new phone — no account.
    new_phone = _fresh_phone()
    start = opts.client.post("/api/auth/phone/register/start", {"phone": new_phone})
    assert start.status_code == 200, \
        f"start must succeed for a new phone: {opts.client.last_response.body}"
    st = start.response.data.session_token
    code = json.loads(get_connection().get(f"phone:register:session:{st}"))["code"]
    verify = opts.client.post(
        "/api/auth/phone/register/verify", {"session_token": st, "code": code})
    assert verify.status_code == 200, \
        f"verify must succeed: {opts.client.last_response.body}"
    assert verify.response.data.account_exists is False, \
        "account_exists must be False for a phone with no account"

    # An existing phone — has an account; start must now accept it.
    _clear_register_limits()
    ex_phone = _fresh_phone()
    u = User.objects.create_user(
        username=f"ex_{_uuid.uuid4().hex[:6]}",
        email=f"ex_{_uuid.uuid4().hex[:8]}@ae.test", password="Abcd1234!")
    u.phone_number = ex_phone
    u.save()
    try:
        start2 = opts.client.post("/api/auth/phone/register/start", {"phone": ex_phone})
        assert start2.status_code == 200, \
            f"start must accept an already-registered phone now, got " \
            f"{start2.status_code}: {opts.client.last_response.body}"
        st2 = start2.response.data.session_token
        code2 = json.loads(get_connection().get(f"phone:register:session:{st2}"))["code"]
        verify2 = opts.client.post(
            "/api/auth/phone/register/verify", {"session_token": st2, "code": code2})
        assert verify2.status_code == 200, \
            f"verify must succeed: {opts.client.last_response.body}"
        assert verify2.response.data.account_exists is True, \
            "account_exists must be True for a phone that already has an account"
    finally:
        User.objects.filter(phone_number=ex_phone).delete()


@th.django_unit_test("phone register: existing phone with no SMS-verify schema is still rejected")
def test_phone_existing_no_verify_rejects(opts):
    from mojo.apps.account.models import User
    _clear_register_limits()
    phone = _fresh_phone()
    u = User.objects.create_user(
        username=f"nv_{_uuid.uuid4().hex[:6]}",
        email=f"nv_{_uuid.uuid4().hex[:8]}@nv.test", password="Abcd1234!")
    u.phone_number = phone
    u.save()
    # Phone identity but the schema does NOT SMS-verify the phone — ownership
    # cannot be proven, so an existing phone must still be a hard duplicate.
    no_verify_fields = [
        {"name": "first_name", "required": True},
        {"name": "phone", "required": True},
        {"name": "password", "required": True},
    ]
    try:
        resp = opts.client.post(
            "/api/auth/register",
            {"phone": phone},
            headers=_register_headers(fields=no_verify_fields))
        assert resp.status_code in (400, 422), \
            f"an existing phone without SMS verification must be rejected, " \
            f"got {resp.status_code}: {opts.client.last_response.body}"
        assert "already exists" in str(opts.client.last_response.body).lower(), \
            "the rejection must be the duplicate-account error"
    finally:
        User.objects.filter(phone_number=phone).delete()


@th.django_unit_test("register.html skips the profile step when the phone already has an account")
def test_register_html_skips_step_for_existing_account(opts):
    import os
    import mojo.apps.account as account_pkg
    tpl = os.path.join(os.path.dirname(account_pkg.__file__),
                       "templates", "account", "register.html")
    with open(tpl) as fh:
        src = fh.read()
    assert "data.account_exists" in src, \
        "register.html must branch on data.account_exists from phone-verify"
    assert "function finishRegister" in src, \
        "register.html must route post-register handling through finishRegister"


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


@th.django_unit_test("phone register: a raising USER_REGISTERED_HANDLER must NOT burn the token (existing account retries)")
def test_existing_account_handler_raise_keeps_token(opts):
    """Regression (ITEM-008): when the per-group registration handler raises, the
    existing user isn't logged in AND the verified token was consumed before the
    failure — so on `main` a retry 400s ("Invalid or expired phone verification").
    After the fix the SAME token is restored and the retry signs the user in."""
    from mojo.apps.account.models import User, Group
    from mojo.apps.account.models.member import GroupMember
    _clear_register_limits()
    phone = _fresh_phone()
    group_uuid = _uuid.uuid4().hex

    existing = User.objects.create_user(
        username=f"exist_{_uuid.uuid4().hex[:6]}",
        email=f"exist_{_uuid.uuid4().hex[:8]}@dup.test",
        password="Abcd1234!")
    existing.phone_number = phone
    existing.save()
    group = Group.objects.create(name=f"g-{group_uuid[:8]}", uuid=group_uuid, is_active=True)
    try:
        token = _start_and_verify_phone(opts, phone)

        # First attempt: the group's handler raises → request fails, user not signed in.
        bad = opts.client.post(
            "/api/auth/register",
            {"phone": phone, "verified_phone_token": token, "group_uuid": group_uuid},
            headers={**_register_headers(),
                     "X-Mojo-Test-User-Registered-Handler": "tests.test_register._capture.raising_register"})
        assert bad.status_code >= 400, \
            f"a raising handler must fail the request, got {bad.status_code}: {opts.client.last_response.body}"
        assert not GroupMember.objects.filter(user=existing, group=group).exists(), \
            "the group join must roll back when the handler raises"

        # Retry with the SAME token (handler no longer raising) — must sign in.
        good = opts.client.post(
            "/api/auth/register",
            {"phone": phone, "verified_phone_token": token, "group_uuid": group_uuid},
            headers=_register_headers())
        assert good.status_code == 200, \
            f"the same token must still work on retry after the failed attempt, got " \
            f"{good.status_code}: {opts.client.last_response.body}"
        assert bool(good.response.data.access_token), \
            "an access token must be issued on the successful retry"
        assert GroupMember.objects.filter(user=existing, group=group).exists(), \
            "the retry must complete the group join"
    finally:
        GroupMember.objects.filter(group=group).delete()
        User.objects.filter(phone_number=phone).delete()
        Group.objects.filter(uuid=group_uuid).delete()


@th.django_unit_test("phone register: a raising USER_REGISTERED_HANDLER must NOT burn the token (new user retries)")
def test_new_user_handler_raise_keeps_token(opts):
    """Regression (ITEM-008): a new phone-only registration whose handler raises
    creates no user and (on `main`) burns the token → retry 400s. After the fix the
    token is restored and the retry creates the account + signs in."""
    from mojo.apps.account.models import User, Group
    _clear_register_limits()
    phone = _fresh_phone()
    group_uuid = _uuid.uuid4().hex
    group = Group.objects.create(name=f"g-{group_uuid[:8]}", uuid=group_uuid, is_active=True)
    payload = {"phone": phone, "group_uuid": group_uuid, "first_name": "Pat",
               "last_name": "Lee", "dob": "1990-01-01", "password": "Abcd1234!"}
    try:
        token = _start_and_verify_phone(opts, phone)

        # First attempt: handler raises → no user created.
        bad = opts.client.post(
            "/api/auth/register",
            {**payload, "verified_phone_token": token},
            headers={**_register_headers(),
                     "X-Mojo-Test-User-Registered-Handler": "tests.test_register._capture.raising_register"})
        assert bad.status_code >= 400, \
            f"a raising handler must fail the request, got {bad.status_code}: {opts.client.last_response.body}"
        assert not User.objects.filter(phone_number=phone).exists(), \
            "no user may be created when the handler raises"

        # Retry with the SAME token — must create the user + sign in.
        good = opts.client.post(
            "/api/auth/register",
            {**payload, "verified_phone_token": token},
            headers=_register_headers())
        assert good.status_code == 200, \
            f"the same token must still work on retry, got {good.status_code}: {opts.client.last_response.body}"
        assert User.objects.filter(phone_number=phone).exists(), \
            "the retry must create the user"
        assert bool(good.response.data.access_token), \
            "an access token must be issued on the successful retry"
    finally:
        User.objects.filter(phone_number=phone).delete()
        Group.objects.filter(uuid=group_uuid).delete()
