"""Maestro item #6226, Step 1 — a one-time code can't be guessed against one
account.

Every code check used to be limited per IP only, so a guesser who rotated
addresses got as many tries at one account's six-digit code as they had
addresses. Each check now counts tries per account: five in 15 minutes, then
the standard 429 with the real wait, whatever address the try comes from.

The loop-until-limit cases are not driven over HTTP here: the endpoints also
carry per-IP limits shared with every module posting from loopback. The five
tries are counted through the same function the endpoints call, and the wire
proves the three things an endpoint adds: a wrong code is counted, a right
code clears the count, and at the limit the right code is refused.
"""
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "core"

ROOT = Path(__file__).resolve().parents[2]
PWORD = "ca##mojo99Attempts"
STRONG_PWORD = "ca##Reset77Strong"
WEAK_PWORD = "abc"
RIGHT = "246810"
WRONG = "135791"
NEW_EMAIL = "ca_change_new@example.com"
REGISTER_PHONE = "+15550006226"
LIMIT = 5
WINDOW = 900

ADMIN = "ca_admin"
USERS = {
    "ca_admin": None,
    "ca_sms": "+15550006227",
    "ca_reset": None,
    "ca_verify": "+15550006228",
    "ca_change": None,
    "ca_totp": None,
    "ca_manage": None,
}
KINDS = ("sms", "reset", "phone_verify", "email_verify", "email_change",
         "totp", "totp_login", "totp_manage")


def _fresh(pk):
    from mojo.apps.account.models import User
    return User.objects.get(pk=pk)


def _count(kind, account_id):
    from mojo.decorators.limits import read_account_attempt
    return read_account_attempt(f"code:{kind}", account_id, limit=LIMIT, window=WINDOW)["count"]


def _clear(account_id, kinds=KINDS):
    from mojo.decorators import limits
    for kind in kinds:
        limits.clear_code_attempts(kind, account_id)


def _spend(kind, account_id, tries=LIMIT):
    """Count `tries` tries against the account, as that many wrong codes would."""
    from mojo.decorators import limits
    for _ in range(tries):
        limits.check_code_attempt(kind, account_id)


def _clear_ip(*keys):
    from mojo.decorators.limits import clear_rate_limits
    for key in keys:
        clear_rate_limits(ip="127.0.0.1", key=key)


def _seed(pk, **secrets):
    user = _fresh(pk)
    for key, value in secrets.items():
        user.set_secret(key, value)
    user.save()


def _assert_refused(opts, resp, what):
    """The refusal is the standard 429, with the wait in the body and the header."""
    assert_eq(resp.status_code, 429, f"{what}: at the limit the try must be refused with 429, "
                                     f"got {resp.status_code}: {opts.client.last_response.body}")
    body = opts.client.last_response.body
    wait = body.get("retry_after")
    assert_true(isinstance(wait, int) and 0 < wait <= WINDOW,
                f"{what}: the 429 body must carry retry_after in seconds, got {body}")
    # The client stores the raw lowercase wire names in a plain dict.
    headers = {str(k).lower(): v for k, v in (opts.client.last_response.headers or {}).items()}
    header = headers.get("retry-after")
    assert_eq(header, str(wait), f"{what}: Retry-After must match the body, got {header!r}")


@th.django_unit_setup()
def setup_code_attempts(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.totp import UserTOTP
    from mojo.decorators import limits
    import pyotp

    limits.clear_rate_limits(ip="127.0.0.1", key="login")
    User.objects.filter(username__in=list(USERS)).delete()
    User.objects.filter(phone_number__in=[p for p in USERS.values() if p]).update(phone_number=None)
    User.objects.filter(email=NEW_EMAIL).delete()
    for name, phone in USERS.items():
        user = User(username=name, display_name=name, email=f"{name}@example.com")
        user.phone_number = phone
        user.save()
        user.is_email_verified = True
        user.save_password(PWORD)
        user.remove_all_permissions()
        if name == ADMIN:
            user.add_permission(["manage_users"])
        setattr(opts, f"{name}_id", user.pk)
        _clear(user.pk)
        limits.clear_rate_limits(key="login", account_id=user.pk)
    limits.clear_code_attempts("phone_register", REGISTER_PHONE)

    for name in ("ca_totp", "ca_manage"):
        totp = UserTOTP(user=_fresh(getattr(opts, f"{name}_id")))
        secret = pyotp.random_base32()
        totp.set_secret("totp_secret", secret)
        totp.is_enabled = True
        totp.save()
        setattr(opts, f"{name}_secret", secret)


# -----------------------------------------------------------------
# The limiter itself
# -----------------------------------------------------------------

@th.django_unit_test("code limit: five tries are counted, the sixth is refused and not counted")
def test_sixth_try_is_refused_and_not_counted(opts):
    from mojo import errors as merrors
    from mojo.decorators import limits

    account = f"ca-{uuid.uuid4().hex}"
    for attempt in range(1, LIMIT + 1):
        limits.check_code_attempt("sms", account)
        assert_eq(_count("sms", account), attempt, f"try {attempt} must be counted before the compare")

    for extra in (1, 2, 3):
        refused = None
        try:
            limits.check_code_attempt("sms", account)
        except merrors.RateLimitException as err:
            refused = err
        assert_true(refused is not None, f"try {LIMIT + extra} must be refused")
        assert_eq(refused.status, 429, "the refusal must be a 429")
        assert_true(0 < refused.retry_after <= WINDOW,
                    f"the refusal must carry the wait, got {refused.retry_after}")
        assert_eq(_count("sms", account), LIMIT,
                  "a refused try must not be counted, or retrying would extend the wait")
    limits.clear_code_attempts("sms", account)
    assert_eq(_count("sms", account), 0, "clearing must empty the account's counter")


@th.django_unit_test("code limit: the wait is taken from the try that has to age out")
def test_wait_comes_from_the_oldest_try(opts):
    from mojo import errors as merrors
    from mojo.decorators import limits

    account = f"ca-{uuid.uuid4().hex}"
    start = time.time() - 400
    for step in range(LIMIT):
        limits.check_code_attempt("sms", account, now=start + step * 10)
    try:
        limits.check_code_attempt("sms", account, now=start + 300)
        wait = None
    except merrors.RateLimitException as err:
        wait = err.retry_after
    assert_eq(wait, WINDOW - 300, f"the wait must run from the oldest counted try, got {wait}")

    limits.check_code_attempt("sms", account, now=start + WINDOW + 1)
    limits.clear_code_attempts("sms", account)


@th.django_unit_test("code limit: two tries in the same instant count as two")
def test_same_instant_tries_both_count(opts):
    from mojo.decorators import limits
    from mojo.helpers.redis import get_connection

    account = f"ca-{uuid.uuid4().hex}"
    now = time.time()
    limits.check_code_attempt("sms", account, now=now)
    limits.check_code_attempt("sms", account, now=now)
    assert_eq(_count("sms", account), 2, "two tries at one timestamp must both be counted")
    limits.clear_code_attempts("sms", account)

    # The shared sliding-window helper had the same collision.
    r = get_connection()
    key = f"srl:ca_same_instant:account:{account}"
    limits._check_sliding(r, key, WINDOW, LIMIT, now=now)
    count, _ = limits._check_sliding(r, key, WINDOW, LIMIT, now=now)
    r.delete(key)
    assert_eq(count, 2, "two password tries at one timestamp must both be counted")


@th.django_unit_test("code limit: the limit is never below one and the window never shorter than the code's life")
def test_limit_and_window_floors(opts):
    from mojo import errors as merrors
    from mojo.decorators import limits

    account = f"ca-{uuid.uuid4().hex}"
    limits.check_code_attempt("sms", account, limit=0)
    assert_eq(_count("sms", account), 1, "a limit set to zero must still allow one try")
    limits.clear_code_attempts("sms", account)

    # A code that lives an hour: tries made 20 minutes ago still count.
    start = time.time() - 1200
    for _ in range(LIMIT):
        limits.check_code_attempt("sms", account, ttl=3600, now=start)
    try:
        limits.check_code_attempt("sms", account, ttl=3600)
        refused = False
    except merrors.RateLimitException:
        refused = True
    limits.clear_code_attempts("sms", account)
    assert_true(refused, "the window must stretch to the code's lifetime, "
                         "or a long-lived code gets more than five guesses")


@th.django_unit_test("code limit: authenticator tries have a daily cap that outlasts the 15-minute window")
def test_totp_daily_cap(opts):
    from mojo import errors as merrors
    from mojo.decorators import limits

    account = f"ca-{uuid.uuid4().hex}"
    start = time.time() - 3 * (WINDOW + 1)
    # Three tries, each after the 15-minute window has passed, across both
    # authenticator sign-in checks: they share one daily count.
    limits.check_code_attempt("totp", account, daily_limit=3, now=start)
    limits.check_code_attempt("totp_login", account, daily_limit=3, now=start + WINDOW + 1)
    limits.check_code_attempt("totp", account, daily_limit=3, now=start + 2 * (WINDOW + 1))
    for kind in ("totp", "totp_login"):
        try:
            limits.check_code_attempt(kind, account, daily_limit=3, now=start + 3 * (WINDOW + 1))
            wait = None
        except merrors.RateLimitException as err:
            wait = err.retry_after
        assert_true(wait is not None, f"{kind}: a try beyond the daily cap must be refused "
                                      "even though the 15-minute window is clear")
        assert_true(WINDOW < wait <= 86400, f"{kind}: the wait must be the daily one, got {wait}")
        assert_eq(_count(kind, account), 0, f"{kind}: a try refused by the daily cap must not be counted")

    # The signed-in authenticator checks are not under the daily cap.
    limits.check_code_attempt("totp_manage", account, daily_limit=3)
    for kind in ("totp", "totp_login", "totp_manage"):
        limits.clear_code_attempts(kind, account)
    limits.check_code_attempt("totp", account, daily_limit=3)
    limits.clear_code_attempts("totp", account)


# -----------------------------------------------------------------
# SMS sign-in code
# -----------------------------------------------------------------

def _post_sms(opts, code):
    _clear_ip("sms_verify")
    opts.client.logout()
    return opts.client.post("/api/auth/sms/verify", {"username": "ca_sms", "code": code})


@th.django_unit_test("sms code: a wrong try is counted, a right one clears the count")
def test_sms_wrong_then_right(opts):
    pk = opts.ca_sms_id
    _clear(pk)
    _seed(pk, sms_otp_code=RIGHT, sms_otp_ts=int(time.time()))

    resp = _post_sms(opts, WRONG)
    assert_eq(resp.status_code, 401, f"a wrong code must be refused, got {resp.status_code}")
    assert_eq(_count("sms", pk), 1, "a wrong SMS code must be counted against the account")

    resp = _post_sms(opts, RIGHT)
    assert_eq(resp.status_code, 200, f"wrong-then-right inside the limit must sign in, "
                                     f"got {resp.status_code}: {opts.client.last_response.body}")
    assert_eq(_count("sms", pk), 0, "a right code must clear the account's counter")


@th.django_unit_test("sms code: after five tries the right code is refused with the wait")
def test_sms_right_code_refused_at_limit(opts):
    pk = opts.ca_sms_id
    _clear(pk)
    _seed(pk, sms_otp_code=RIGHT, sms_otp_ts=int(time.time()))
    _spend("sms", pk)

    resp = _post_sms(opts, RIGHT)
    _assert_refused(opts, resp, "sms verify")
    assert_eq(_count("sms", pk), LIMIT, "refused tries must leave the count at five")
    assert_eq(_fresh(pk).get_secret("sms_otp_code"), RIGHT,
              "a refused try must not consume the code")

    resp = _post_sms(opts, RIGHT)
    _assert_refused(opts, resp, "sms verify, second refused try")
    assert_eq(_count("sms", pk), LIMIT, "two more tries must leave the count at five")
    _clear(pk)


# -----------------------------------------------------------------
# Admin release
# -----------------------------------------------------------------

def _admin_client(opts):
    from testit.client import RestClient
    client = RestClient(opts.client.host)
    assert_true(client.login(ADMIN, PWORD), "the admin must be able to log in")
    return client


@th.django_unit_test("admin release: the portal's clear, sent with key 'login', releases a code lock")
def test_admin_release_clears_every_account_counter(opts):
    from mojo.decorators import limits

    pk = opts.ca_sms_id
    _clear(pk)
    _seed(pk, sms_otp_code=RIGHT, sms_otp_ts=int(time.time()))
    for kind in KINDS:
        _spend(kind, pk)
    limits.check_account_attempt("login", pk, 10, 900)
    resp = _post_sms(opts, RIGHT)
    _assert_refused(opts, resp, "sms verify before the release")

    admin = _admin_client(opts)
    resp = admin.post("/api/auth/manage/clear_rate_limit", {"key": "login", "username": "ca_sms"})
    assert_eq(resp.status_code, 200, f"the admin release must succeed, got {resp.status_code}: {resp.response}")
    assert_true(resp.response.data.deleted >= len(KINDS),
                f"the release must report the counters it cleared, got {resp.response.data}")
    for kind in KINDS + ("totp_daily",):
        assert_eq(_count(kind, pk), 0, f"the release must clear the {kind} counter whatever key was sent")
    assert_eq(limits.read_account_attempt("login", pk, limit=10, window=900)["count"], 0,
              "the release must still clear the password counter")

    resp = _post_sms(opts, RIGHT)
    assert_eq(resp.status_code, 200, f"after the release the right code must work, "
                                     f"got {resp.status_code}: {opts.client.last_response.body}")


@th.django_unit_test("admin release: a release by user id works without a key")
def test_admin_release_by_user_id(opts):
    pk = opts.ca_verify_id
    _clear(pk)
    _spend("phone_verify", pk)
    admin = _admin_client(opts)
    resp = admin.post("/api/auth/manage/clear_rate_limit", {"user_id": pk})
    assert_eq(resp.status_code, 200, f"the admin release must succeed, got {resp.status_code}: {resp.response}")
    assert_eq(_count("phone_verify", pk), 0, "a release with no key must clear the code counters too")


@th.django_unit_test("admin throttle read: code counters are reported with their own numbers")
def test_admin_throttle_reads_code_counters(opts):
    pk = opts.ca_reset_id
    _clear(pk)
    _spend("reset", pk)
    _spend("totp", pk, 2)
    admin = _admin_client(opts)

    resp = admin.get("/api/auth/manage/throttle", params={"username": "ca_reset", "key": "code:reset"})
    assert_eq(resp.status_code, 200, f"the read must succeed, got {resp.status_code}: {resp.response}")
    data = resp.response.data
    assert_eq(data.count, LIMIT, f"the reset-code counter must be reported, got {data}")
    assert_eq(data.limit, LIMIT, "a code counter must be reported with the code limit, not the password one")
    assert_eq(data.window, WINDOW, "a code counter must be reported with the code window")
    assert_true(0 < data.retry_after_seconds <= WINDOW, f"a locked counter must report the wait, got {data}")

    resp = admin.get("/api/auth/manage/throttle", params={"user_id": pk, "key": "code:totp_daily"})
    assert_eq(resp.status_code, 200, f"the daily read must succeed, got {resp.status_code}: {resp.response}")
    assert_eq(resp.response.data.count, 2, "the daily authenticator count must be reported")
    assert_eq(resp.response.data.limit, 20, "the daily cap must be reported with its own limit")
    assert_eq(resp.response.data.window, 86400, "the daily cap must be reported with its own window")

    resp = admin.get("/api/auth/manage/throttle", params={"username": "ca_reset"})
    assert_eq(resp.status_code, 200, f"the default read must still succeed, got {resp.status_code}")
    assert_eq(resp.response.data.limit, 10, "with no key the password counter is reported, as before")

    resp = admin.get("/api/auth/manage/throttle", params={"username": "ca_reset", "key": "code:nonsense"})
    assert_eq(resp.status_code, 400, f"an unknown counter must be refused, got {resp.status_code}")
    _clear(pk)


# -----------------------------------------------------------------
# Password reset code
# -----------------------------------------------------------------

def _post_reset(opts, code, new_password):
    _clear_ip("password_reset_code")
    opts.client.logout()
    return opts.client.post("/api/auth/password/reset/code", {
        "username": "ca_reset", "code": code, "new_password": new_password})


def _seed_reset(pk):
    _seed(pk, password_reset_code=RIGHT, password_reset_code_ts=int(time.time()))


@th.django_unit_test("reset code: a wrong try is counted; at the limit the right code changes nothing")
def test_reset_code_limit(opts):
    pk = opts.ca_reset_id
    _clear(pk)
    _seed_reset(pk)

    resp = _post_reset(opts, WRONG, STRONG_PWORD)
    assert_eq(resp.status_code, 400, f"a wrong reset code must be refused, got {resp.status_code}")
    assert_eq(_count("reset", pk), 1, "a wrong reset code must be counted against the account")

    _spend("reset", pk, LIMIT - 1)
    resp = _post_reset(opts, RIGHT, STRONG_PWORD)
    _assert_refused(opts, resp, "reset code")
    user = _fresh(pk)
    assert_true(user.check_password(PWORD), "a refused reset must leave the password unchanged")
    assert_eq(user.get_secret("password_reset_code"), RIGHT, "a refused reset must not consume the code")
    _clear(pk)


@th.django_unit_test("reset code: a right code with a weak password is not a guess")
def test_reset_right_code_weak_password_is_not_counted(opts):
    pk = opts.ca_reset_id
    _clear(pk)
    _seed_reset(pk)

    for attempt in range(1, LIMIT + 1):
        resp = _post_reset(opts, RIGHT, WEAK_PWORD)
        assert_eq(resp.status_code, 400, f"weak password try {attempt} must be turned down, "
                                         f"got {resp.status_code}: {opts.client.last_response.body}")
        assert_true("weak" in str(opts.client.last_response.body.get("error", "")).lower(),
                    f"try {attempt} must fail on the password, not the code: "
                    f"{opts.client.last_response.body}")
        assert_eq(_count("reset", pk), 0, "the counter must be cleared as soon as the code matches")

    resp = _post_reset(opts, RIGHT, STRONG_PWORD)
    assert_eq(resp.status_code, 200, f"the reset must succeed after five weak passwords, "
                                     f"got {resp.status_code}: {opts.client.last_response.body}")
    assert_true(_fresh(pk).check_password(STRONG_PWORD), "the new password must be set")


@th.django_unit_test("reset code: an unknown account is limited the same way, so the 429 can't tell them apart")
def test_reset_unknown_account_is_counted_like_a_real_one(opts):
    from mojo.apps.account.models import User
    from mojo.decorators import limits

    ghost = f"ca_ghost_{uuid.uuid4().hex[:10]}"
    assert_true(not User.objects.filter(username=ghost).exists(), "the test needs a name with no account")
    ghost_id = limits.unknown_account_id(ghost)

    _clear_ip("password_reset_code")
    opts.client.logout()
    resp = opts.client.post("/api/auth/password/reset/code", {
        "username": ghost.upper(), "code": WRONG, "new_password": STRONG_PWORD})
    assert_eq(resp.status_code, 400, f"a try for an unknown account must be refused, got {resp.status_code}")
    assert_eq(opts.client.last_response.body.get("error"), "Invalid code",
              "an unknown account must answer exactly as a wrong code does")
    assert_eq(_count("reset", ghost_id), 1,
              "a try for an unknown account must be counted, whatever the case it was typed in")

    _spend("reset", ghost_id, LIMIT - 1)
    _clear_ip("password_reset_code")
    resp = opts.client.post("/api/auth/password/reset/code", {
        "username": ghost, "code": WRONG, "new_password": STRONG_PWORD})
    _assert_refused(opts, resp, "reset code for an unknown account")
    limits.clear_code_attempts("reset", ghost_id)


# -----------------------------------------------------------------
# Phone and email verify codes (signed in)
# -----------------------------------------------------------------

def _verify_flow(opts, kind, path, code_key, ts_key, ip_key):
    pk = opts.ca_verify_id
    _clear(pk)
    _seed(pk, **{code_key: RIGHT, ts_key: int(time.time())})
    assert_true(opts.client.login("ca_verify", PWORD), "the user must be able to log in")

    _clear_ip(ip_key)
    resp = opts.client.post(path, {"code": WRONG})
    assert_eq(resp.status_code, 400, f"{kind}: a wrong code must be refused, got {resp.status_code}")
    assert_eq(_count(kind, pk), 1, f"{kind}: a wrong code must be counted against the account")

    _spend(kind, pk, LIMIT - 1)
    _clear_ip(ip_key)
    resp = opts.client.post(path, {"code": RIGHT})
    _assert_refused(opts, resp, kind)
    assert_eq(_fresh(pk).get_secret(code_key), RIGHT, f"{kind}: a refused try must not consume the code")

    _clear(pk)
    _spend(kind, pk, 2)
    _clear_ip(ip_key)
    resp = opts.client.post(path, {"code": RIGHT})
    assert_eq(resp.status_code, 200, f"{kind}: the right code inside the limit must verify, "
                                     f"got {resp.status_code}: {opts.client.last_response.body}")
    assert_eq(_count(kind, pk), 0, f"{kind}: a right code must clear the account's counter")


@th.django_unit_test("phone verify code: counted, refused at the limit, cleared on a match")
def test_phone_verify_code_limit(opts):
    _verify_flow(opts, "phone_verify", "/api/auth/verify/phone/confirm",
                 "phone_verify_code", "phone_verify_ts", "phone_verify_confirm")


@th.django_unit_test("email verify code: counted, refused at the limit, cleared on a match")
def test_email_verify_code_limit(opts):
    _verify_flow(opts, "email_verify", "/api/auth/verify/email/confirm",
                 "email_verify_code", "email_verify_code_ts", "email_verify_code_confirm")


@th.django_unit_test("email change code: counted, refused at the limit, cleared on a match")
def test_email_change_code_limit(opts):
    pk = opts.ca_change_id
    _clear(pk)

    def seed():
        _seed(pk, pending_email=NEW_EMAIL, email_change_otp=RIGHT,
              email_change_otp_ts=int(time.time()))

    seed()
    assert_true(opts.client.login("ca_change", PWORD), "the user must be able to log in")
    _clear_ip("email_change_confirm")
    resp = opts.client.post("/api/auth/email/change/confirm", {"code": WRONG})
    assert_eq(resp.status_code, 400, f"a wrong code must be refused, got {resp.status_code}")
    assert_eq(_count("email_change", pk), 1, "a wrong email-change code must be counted")

    _spend("email_change", pk, LIMIT - 1)
    resp = opts.client.post("/api/auth/email/change/confirm", {"code": RIGHT})
    _assert_refused(opts, resp, "email change")
    assert_eq(_fresh(pk).email, "ca_change@example.com", "a refused try must not change the email")

    _clear(pk)
    _spend("email_change", pk, 2)
    resp = opts.client.post("/api/auth/email/change/confirm", {"code": RIGHT})
    assert_eq(resp.status_code, 200, f"the right code inside the limit must change the email, "
                                     f"got {resp.status_code}: {opts.client.last_response.body}")
    assert_eq(_count("email_change", pk), 0, "a right code must clear the account's counter")
    opts.client.logout()


# -----------------------------------------------------------------
# Authenticator codes
# -----------------------------------------------------------------

def _totp_now(secret):
    import pyotp
    return pyotp.TOTP(secret).now()


def _mfa_token(pk):
    from mojo.apps.account.services import mfa as mfa_service
    return mfa_service.create_mfa_token(_fresh(pk), ["totp"])


def _post_totp_verify(opts, pk, code):
    _clear_ip("totp_verify")
    opts.client.logout()
    return opts.client.post("/api/auth/totp/verify", {"mfa_token": _mfa_token(pk), "code": code})


@th.django_unit_test("authenticator second step: counted, refused at the limit, cleared on a match")
def test_totp_verify_limit(opts):
    pk = opts.ca_totp_id
    _clear(pk)

    resp = _post_totp_verify(opts, pk, "000000")
    assert_eq(resp.status_code, 401, f"a wrong code must be refused, got {resp.status_code}")
    assert_eq(_count("totp", pk), 1, "a wrong authenticator code must be counted")
    assert_eq(_count("totp_daily", pk), 1, "and counted toward the daily cap")

    resp = _post_totp_verify(opts, pk, _totp_now(opts.ca_totp_secret))
    assert_eq(resp.status_code, 200, f"wrong-then-right inside the limit must sign in, "
                                     f"got {resp.status_code}: {opts.client.last_response.body}")
    assert_eq(_count("totp", pk), 0, "a right code must clear the account's counter")
    assert_eq(_count("totp_daily", pk), 0, "a right code must clear the daily count")

    _spend("totp", pk)
    resp = _post_totp_verify(opts, pk, _totp_now(opts.ca_totp_secret))
    _assert_refused(opts, resp, "totp verify")
    _clear(pk)


@th.django_unit_test("authenticator sign-in: wrong codes can't lock password sign-in")
def test_totp_login_uses_its_own_bucket(opts):
    from mojo.decorators.limits import read_account_attempt

    pk = opts.ca_totp_id
    _clear(pk)
    opts.client.logout()

    _clear_ip("totp_login")
    resp = opts.client.post("/api/auth/totp/login", {"username": "ca_totp", "code": "000000"})
    assert_eq(resp.status_code, 401, f"a wrong code must be refused, got {resp.status_code}")
    assert_eq(_count("totp_login", pk), 1, "a wrong authenticator sign-in code must be counted")
    password_tries = read_account_attempt("login", pk, limit=10, window=900)["count"]
    assert_eq(password_tries, 0, "a wrong authenticator code must not use up password tries")

    _spend("totp_login", pk, LIMIT - 1)
    _clear_ip("totp_login")
    resp = opts.client.post("/api/auth/totp/login", {
        "username": "ca_totp", "code": _totp_now(opts.ca_totp_secret)})
    _assert_refused(opts, resp, "totp login")
    assert_eq(_count("totp", pk), 0, "sign-in tries must not lock the second-step check")

    _clear(pk)
    _clear_ip("totp_login")
    resp = opts.client.post("/api/auth/totp/login", {
        "username": "ca_totp", "code": _totp_now(opts.ca_totp_secret)})
    assert_eq(resp.status_code, 200, f"after the counter is cleared the right code must sign in, "
                                     f"got {resp.status_code}: {opts.client.last_response.body}")
    opts.client.logout()


@th.django_unit_test("authenticator set-up checks: their own bucket, so typos can't lock sign-in")
def test_totp_manage_limit(opts):
    pk = opts.ca_manage_id
    _clear(pk)
    assert_true(opts.client.login("ca_manage", PWORD), "the user must be able to log in")

    resp = opts.client.post("/api/account/totp/confirm", {"code": "000000"})
    assert_eq(resp.status_code, 400, f"a wrong confirm code must be refused, got {resp.status_code}")
    resp = opts.client.post("/api/account/totp/recovery-codes/regenerate", {"code": "000000"})
    assert_eq(resp.status_code, 403, f"a wrong regenerate code must be refused, got {resp.status_code}")
    resp = opts.client.post("/api/user/me", {"confirm_totp": {"code": "000000"}})
    assert_eq(resp.status_code, 400, f"a wrong confirm action code must be refused, got {resp.status_code}")
    resp = opts.client.post("/api/user/me", {"regenerate_totp_codes": {"code": "000000"}})
    assert_eq(resp.status_code, 403, f"a wrong regenerate action code must be refused, got {resp.status_code}")
    assert_eq(_count("totp_manage", pk), 4, "each signed-in authenticator check must be counted")
    assert_eq(_count("totp", pk), 0, "set-up typos must not count against sign-in")
    assert_eq(_count("totp_login", pk), 0, "set-up typos must not count against sign-in")

    _spend("totp_manage", pk, 1)
    code = _totp_now(opts.ca_manage_secret)
    for path, body in (
            ("/api/account/totp/confirm", {"code": code}),
            ("/api/account/totp/recovery-codes/regenerate", {"code": code}),
            ("/api/user/me", {"confirm_totp": {"code": code}}),
            ("/api/user/me", {"regenerate_totp_codes": {"code": code}})):
        resp = opts.client.post(path, body)
        _assert_refused(opts, resp, f"{path} {sorted(body)}")

    _clear(pk)
    resp = opts.client.post("/api/user/me", {"regenerate_totp_codes": {"code": _totp_now(opts.ca_manage_secret)}})
    assert_eq(resp.status_code, 200, f"the right code inside the limit must work, "
                                     f"got {resp.status_code}: {opts.client.last_response.body}")
    assert_eq(_count("totp_manage", pk), 0, "a right code must clear the account's counter")
    opts.client.logout()


# -----------------------------------------------------------------
# Phone sign-up code (no account yet: keyed on the phone number)
# -----------------------------------------------------------------

def _post_register_verify(opts, session_token, code):
    _clear_ip("phone_register_verify")
    return opts.client.post("/api/auth/phone/register/verify", {
        "session_token": session_token, "code": code})


@th.django_unit_test("phone sign-up code: counted per phone number, across sessions")
def test_phone_register_code_limit(opts):
    from mojo.apps.account.services import phone_register
    from mojo.decorators import limits

    limits.clear_code_attempts("phone_register", REGISTER_PHONE)
    opts.client.logout()
    session, code, _ = phone_register.start(REGISTER_PHONE)
    wrong = "000000" if code != "000000" else "111111"

    resp = _post_register_verify(opts, session, wrong)
    assert_eq(resp.status_code, 400, f"a wrong code must be refused, got {resp.status_code}")
    assert_eq(_count("phone_register", REGISTER_PHONE), 1,
              "a wrong sign-up code must be counted against the phone number")

    resp = _post_register_verify(opts, session, code)
    assert_eq(resp.status_code, 200, f"wrong-then-right inside the limit must verify, "
                                     f"got {resp.status_code}: {opts.client.last_response.body}")
    assert_eq(_count("phone_register", REGISTER_PHONE), 0, "a right code must clear the counter")

    # A new session for the same number does not start a new count.
    _spend("phone_register", REGISTER_PHONE)
    session, code, _ = phone_register.start(REGISTER_PHONE)
    resp = _post_register_verify(opts, session, code)
    _assert_refused(opts, resp, "phone register verify")
    limits.clear_code_attempts("phone_register", REGISTER_PHONE)


# -----------------------------------------------------------------
# The compare itself
# -----------------------------------------------------------------

@th.django_unit_test("code compare: constant-time, and safe on any input")
def test_codes_match(opts):
    from mojo.helpers import crypto

    assert_true(crypto.codes_match("246810", "246810"), "equal codes must match")
    assert_true(crypto.codes_match(246810, "246810"), "a code posted as a number must match its text")
    assert_true(not crypto.codes_match("246811", "246810"), "different codes must not match")
    assert_true(not crypto.codes_match("", ""), "an empty code must never match")
    assert_true(not crypto.codes_match(None, None), "a missing code must never match")
    assert_true(not crypto.codes_match("246810", None), "a code must not match when none is stored")
    assert_true(not crypto.codes_match("２４６８１０", "246810"),
                "non-ASCII input must be refused, not raise")


@th.django_unit_test("code compare: an expired code is reported as expired before it is compared")
def test_expired_code_is_checked_before_the_compare(opts):
    pk = opts.ca_reset_id
    _clear(pk)
    _seed(pk, password_reset_code=RIGHT, password_reset_code_ts=int(time.time()) - 3600)

    resp = _post_reset(opts, WRONG, STRONG_PWORD)
    assert_eq(resp.status_code, 400, f"an expired code must be refused, got {resp.status_code}")
    assert_eq(opts.client.last_response.body.get("error"), "Expired code",
              "expiry must be decided before the compare, so the answer can't depend on the guess")
    _clear(pk)


# -----------------------------------------------------------------
# What the hosted pages show
# -----------------------------------------------------------------

@th.tier("framework")  # needs Node.js, like the other hosted-client tests
@th.django_unit_test("hosted pages: a 429 with a wait reads 'Too many attempts. Try again in N minutes.'")
def test_hosted_pages_show_the_wait(opts):
    node = shutil.which("node")
    assert_true(node, "Node.js is required for the hosted-auth client test")
    result = subprocess.run(
        [node, str(Path(__file__).with_suffix(".js")),
         str(ROOT / "mojo/apps/account/static/account/mojo-auth.js")],
        capture_output=True, text=True, timeout=20)
    assert_eq(result.returncode, 0, f"the client harness must run: {result.stderr}")
    shown = json.loads(result.stdout)
    assert_eq(shown["wait_840"], "Too many attempts. Try again in 14 minutes.",
              "a 14-minute wait must be shown in minutes")
    assert_eq(shown["wait_61"], "Too many attempts. Try again in 2 minutes.",
              "a part minute must round up, never down")
    assert_eq(shown["wait_30"], "Too many attempts. Try again in 1 minute.",
              "under a minute must read as one minute")
    assert_eq(shown["no_wait"], "Too many attempts. Try again later.",
              "a 429 with no wait given must still say what happened")
    assert_eq(shown["other"], "Invalid code", "other errors must be shown unchanged")
