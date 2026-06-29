"""Integration tests for /auth/phone/register/start and /verify endpoints."""
from unittest.mock import patch

from testit import helpers as th


def _clear_register_limits():
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="phone_register_start")
    clear_rate_limits(ip="127.0.0.1", key="phone_register_verify")


def _mock_sms():
    """Mock phonehub.send_sms in the test-server process via a fake handler."""
    # The test server runs in a separate process, so unittest.mock.patch in the
    # test process is ineffective for the actual SMS send. The existing SMS
    # tests work because phonehub is configured to no-op in test settings.
    # Here we just confirm the endpoint returns 200; the SMS dispatch itself
    # is best-effort and won't fail the endpoint.
    return None


@th.django_unit_test("phone register start: 200 with session_token for fresh phone")
def test_start_happy_path(opts):
    _clear_register_limits()
    resp = opts.client.post(
        "/api/auth/phone/register/start",
        {"phone": "+14155557001"})
    assert resp.status_code == 200, \
        f"phone register start must succeed, got {resp.status_code}: {opts.client.last_response.body}"
    data = resp.response.data
    assert data.session_token, \
        f"start must return session_token, got {resp.response}"
    assert len(data.session_token) == 32, \
        f"session_token must be 32 chars, got {data.session_token!r}"
    assert data.expires_in > 0, "expires_in must be positive"


@th.django_unit_test("phone register start: accepts a phone that already belongs to a user")
def test_start_accepts_existing_phone(opts):
    from mojo.apps.account.models import User
    _clear_register_limits()
    # Pre-create a user with this phone. Registering with an already-registered
    # phone is now a valid flow — `on_register` turns it into a login for the
    # proven owner — so `start` must NOT reject it up front.
    phone = "+14155557002"
    User.objects.filter(phone_number=phone).delete()
    u = User.objects.create_user(username="phone_existing", email="phone_existing@test.com", password="Abcd1234!")
    u.phone_number = phone
    u.save()

    try:
        resp = opts.client.post(
            "/api/auth/phone/register/start",
            {"phone": phone})
        assert resp.status_code == 200, \
            f"start must accept an already-registered phone, got {resp.status_code}: {opts.client.last_response.body}"
        assert bool(resp.response.data.session_token), \
            "start must still mint a session_token for an existing phone"
    finally:
        User.objects.filter(phone_number=phone).delete()


@th.django_unit_test("phone register start: 400 on malformed phone")
def test_start_rejects_bad_phone(opts):
    _clear_register_limits()
    resp = opts.client.post(
        "/api/auth/phone/register/start",
        {"phone": "not-a-phone-at-all"})
    assert resp.status_code in (400, 422), \
        f"malformed phone must be rejected, got {resp.status_code}: {opts.client.last_response.body}"


@th.django_unit_test("phone register start: requires_params returns 400 with no phone")
def test_start_requires_phone(opts):
    _clear_register_limits()
    resp = opts.client.post(
        "/api/auth/phone/register/start",
        {})
    assert resp.status_code in (400, 422), \
        f"missing phone must be 4xx, got {resp.status_code}: {opts.client.last_response.body}"


@th.django_unit_test("phone register verify: 400 on bad session token")
def test_verify_rejects_bad_session(opts):
    _clear_register_limits()
    resp = opts.client.post(
        "/api/auth/phone/register/verify",
        {"session_token": "deadbeef" * 4, "code": "123456"})
    assert resp.status_code in (400, 401, 422), \
        f"unknown session token must be 4xx, got {resp.status_code}: {opts.client.last_response.body}"


@th.django_unit_test("phone register full flow: start + verify mints verified_phone_token")
def test_full_phone_register_flow(opts):
    """End-to-end: start mints a session, the service-side code matches what
    the server stored, and verify returns a verified_phone_token.

    Since the SMS code is server-side, we read it from Redis directly using
    the session_token (only possible because tests share the Redis instance).
    Production callers receive the code via SMS.
    """
    import json
    from mojo.helpers.redis import get_connection

    _clear_register_limits()
    start = opts.client.post(
        "/api/auth/phone/register/start",
        {"phone": "+14155557003"})
    assert start.status_code == 200, \
        f"start must succeed, got {start.status_code}: {opts.client.last_response.body}"
    session_token = start.response.data.session_token

    raw = get_connection().get(f"phone:register:session:{session_token}")
    assert raw is not None, "session must be written so the test can read the code"
    code = json.loads(raw)["code"]

    verify = opts.client.post(
        "/api/auth/phone/register/verify",
        {"session_token": session_token, "code": code})
    assert verify.status_code == 200, \
        f"verify must succeed, got {verify.status_code}: {opts.client.last_response.body}"
    assert verify.response.data.verified_phone_token, \
        f"verify must return verified_phone_token, got {verify.response}"
    assert len(verify.response.data.verified_phone_token) == 32, \
        "verified_phone_token must be a 32-char uuid hex"


@th.django_unit_test("phone register verify: a wrong code does not burn the session; correct code then succeeds")
def test_verify_wrong_then_correct_same_session(opts):
    """Regression (ITEM-005): one wrong code must not invalidate the session.

    Repro of the reported dead-end: start -> verify WRONG (4xx) -> verify CORRECT
    on the SAME session_token must still succeed. On the buggy `main`, getdel
    consumed the session on the first (wrong) attempt, so the second call returned
    "Invalid or expired verification session".
    """
    import json
    from mojo.helpers.redis import get_connection

    _clear_register_limits()
    start = opts.client.post(
        "/api/auth/phone/register/start",
        {"phone": "+14155557006"})
    assert start.status_code == 200, \
        f"start must succeed, got {start.status_code}: {opts.client.last_response.body}"
    session_token = start.response.data.session_token

    raw = get_connection().get(f"phone:register:session:{session_token}")
    assert raw is not None, "session must be written so the test can read the code"
    code = json.loads(raw)["code"]
    wrong = "000000" if code != "000000" else "111111"

    # A wrong code is rejected...
    bad = opts.client.post(
        "/api/auth/phone/register/verify",
        {"session_token": session_token, "code": wrong})
    assert bad.status_code in (400, 401, 422), \
        f"a wrong code must be rejected, got {bad.status_code}: {opts.client.last_response.body}"

    # ...but the SAME session_token still accepts the correct code.
    good = opts.client.post(
        "/api/auth/phone/register/verify",
        {"session_token": session_token, "code": code})
    assert good.status_code == 200, \
        f"correct code on the same session must succeed after a wrong attempt, " \
        f"got {good.status_code}: {opts.client.last_response.body}"
    assert good.response.data.verified_phone_token, \
        f"verify must return verified_phone_token, got {good.response}"
    assert len(good.response.data.verified_phone_token) == 32, \
        "verified_phone_token must be a 32-char uuid hex"
