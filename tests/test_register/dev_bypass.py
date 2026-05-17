"""Tests for AUTH_PHONE_VERIFY_DEV_BYPASS_CODE.

The bypass setting lets dev environments exercise the phone-verify flow
without an SMS gateway. Two paths to enable it:

  - Globally via AUTH_PHONE_VERIFY_DEV_BYPASS_CODE setting (operator
    sets this in dev settings.py).
  - Per-request via X-Mojo-Test-Phone-Verify-Bypass-Code header when the
    test-mode gate passes (loopback + MOJO_TEST_MODE + no proxy chain).
    Used by these tests so test_register can stay parallel.
"""
import json

from testit import helpers as th


BYPASS_CODE = "000000"


def _clear_register_limits():
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="phone_register_start")
    clear_rate_limits(ip="127.0.0.1", key="phone_register_verify")


@th.django_unit_test("dev bypass: endpoint accepts bypass code when header is set")
def test_endpoint_accepts_bypass_via_header(opts):
    """End-to-end with the test-mode header: bypass code mints a verified
    token without us knowing the real generated code at all."""
    _clear_register_limits()
    start = opts.client.post(
        "/api/auth/phone/register/start",
        {"phone": "+14155557101"})
    assert start.status_code == 200, \
        f"phone-register start must succeed, got {start.status_code}: {opts.client.last_response.body}"
    session_token = start.response.data.session_token

    verify = opts.client.post(
        "/api/auth/phone/register/verify",
        {"session_token": session_token, "code": BYPASS_CODE},
        headers={"X-Mojo-Test-Phone-Verify-Bypass-Code": BYPASS_CODE})
    assert verify.status_code == 200, \
        f"verify with bypass code must succeed when header is set, " \
        f"got {verify.status_code}: {opts.client.last_response.body}"
    assert verify.response.data.verified_phone_token, \
        f"bypass-verify must return verified_phone_token, got {verify.response}"


@th.django_unit_test("dev bypass: endpoint rejects bypass code without the header")
def test_endpoint_rejects_bypass_without_header(opts):
    _clear_register_limits()
    start = opts.client.post(
        "/api/auth/phone/register/start",
        {"phone": "+14155557102"})
    assert start.status_code == 200, \
        f"phone-register start must succeed, got {start.status_code}"
    session_token = start.response.data.session_token

    verify = opts.client.post(
        "/api/auth/phone/register/verify",
        {"session_token": session_token, "code": BYPASS_CODE})
    assert verify.status_code in (400, 401, 422), \
        f"bypass code must be rejected without the header, " \
        f"got {verify.status_code}: {opts.client.last_response.body}"


@th.django_unit_test("dev bypass: real code still works with bypass header set")
def test_real_code_still_works_with_header(opts):
    """If the operator happens to know the real code (we read from Redis
    here for the test), it should continue to verify correctly even when
    the bypass header is also set on the request."""
    from mojo.helpers.redis import get_connection
    _clear_register_limits()
    start = opts.client.post(
        "/api/auth/phone/register/start",
        {"phone": "+14155557103"})
    assert start.status_code == 200, \
        f"start must succeed, got {start.status_code}"
    session_token = start.response.data.session_token

    raw = get_connection().get(f"phone:register:session:{session_token}")
    assert raw is not None, "session must be in redis"
    real_code = json.loads(raw)["code"]

    verify = opts.client.post(
        "/api/auth/phone/register/verify",
        {"session_token": session_token, "code": real_code},
        headers={"X-Mojo-Test-Phone-Verify-Bypass-Code": BYPASS_CODE})
    assert verify.status_code == 200, \
        f"real code must still verify when bypass header is also set, " \
        f"got {verify.status_code}: {opts.client.last_response.body}"


@th.django_unit_test("dev bypass: wrong code rejected even when bypass header is set")
def test_wrong_code_rejected_with_header(opts):
    _clear_register_limits()
    start = opts.client.post(
        "/api/auth/phone/register/start",
        {"phone": "+14155557104"})
    assert start.status_code == 200
    session_token = start.response.data.session_token

    verify = opts.client.post(
        "/api/auth/phone/register/verify",
        {"session_token": session_token, "code": "999999"},
        headers={"X-Mojo-Test-Phone-Verify-Bypass-Code": BYPASS_CODE})
    assert verify.status_code in (400, 401, 422), \
        f"a code matching neither real nor bypass must be rejected, " \
        f"got {verify.status_code}: {opts.client.last_response.body}"


@th.django_unit_test("dev bypass: empty bypass header value is treated as unset")
def test_empty_bypass_header_is_unset(opts):
    _clear_register_limits()
    start = opts.client.post(
        "/api/auth/phone/register/start",
        {"phone": "+14155557105"})
    assert start.status_code == 200
    session_token = start.response.data.session_token

    verify = opts.client.post(
        "/api/auth/phone/register/verify",
        {"session_token": session_token, "code": BYPASS_CODE},
        headers={"X-Mojo-Test-Phone-Verify-Bypass-Code": ""})
    assert verify.status_code in (400, 401, 422), \
        f"empty header value must disable bypass, " \
        f"got {verify.status_code}: {opts.client.last_response.body}"


@th.django_unit_test("dev bypass: bypass code binds to the session's phone")
def test_bypass_code_binds_to_session_phone(opts):
    """The verified_phone_token returned by the bypass path must still be
    bound to the phone the session was started with — bypass doesn't
    let an attacker arbitrarily-mint a verified token for any phone."""
    _clear_register_limits()
    target_phone = "+14155557106"
    start = opts.client.post(
        "/api/auth/phone/register/start",
        {"phone": target_phone})
    assert start.status_code == 200
    session_token = start.response.data.session_token

    verify = opts.client.post(
        "/api/auth/phone/register/verify",
        {"session_token": session_token, "code": BYPASS_CODE},
        headers={"X-Mojo-Test-Phone-Verify-Bypass-Code": BYPASS_CODE})
    assert verify.status_code == 200, \
        f"bypass verify must succeed, got {verify.status_code}: {opts.client.last_response.body}"

    # Now check the verified key payload: the phone must match what we
    # started the session with.
    from mojo.helpers.redis import get_connection
    verified_token = verify.response.data.verified_phone_token
    raw = get_connection().get(f"phone:register:verified:{verified_token}")
    assert raw is not None, "verified-key must be in redis"
    payload = json.loads(raw)
    assert payload["phone"] == target_phone, \
        f"bypass-minted token must bind to the start-time phone " \
        f"({target_phone}), got {payload['phone']!r}"
    # Cleanup
    get_connection().delete(f"phone:register:verified:{verified_token}")
