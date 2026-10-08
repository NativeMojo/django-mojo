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
SEND_LIMIT = 5
SEND_IP = "127.0.62.26"   # this module's own address for the in-process send calls
SEND_PHONE = "+15550006229"
GENERIC_SMS = "If the account exists, a code was sent."
USERS = {
    "ca_admin": None,
    "ca_send": "+15550006230",
    "ca_sms": "+15550006227",
    "ca_reset": None,
    "ca_num_5550006290": None,   # its email holds ten digits: it reads as a phone number too
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
    limits.clear_account_limits(account_id)


def _spend(kind, account_id, tries=LIMIT):
    """Count `tries` tries against the account, as that many wrong codes would."""
    from mojo.decorators import limits
    for _ in range(tries):
        limits.check_code_attempt(kind, account_id)


def _own_client(opts):
    """A client with this run's own source address. nginx sets X-Real-IP to the
    true client and the framework trusts exactly that header, so the per-IP
    limits on these endpoints are counted apart from every other module's —
    this module neither spends 127.0.0.1's budget nor has to clear it under
    a test that is trying to reach it."""
    from testit.client import RestClient
    client = RestClient(opts.client.host)
    client.headers["X-Real-IP"] = opts.ca_ip
    return client


def _seed(pk, **secrets):
    user = _fresh(pk)
    for key, value in secrets.items():
        user.set_secret(key, value)
    user.save()


def _assert_refused(opts, resp, what):
    """The refusal is the standard 429, with the wait in the body and the header."""
    assert_eq(resp.status_code, 429, f"{what}: at the limit the try must be refused with 429, "
                                     f"got {resp.status_code}: {opts.ca.last_response.body}")
    body = opts.ca.last_response.body
    wait = body.get("retry_after")
    assert_true(isinstance(wait, int) and 0 < wait <= WINDOW,
                f"{what}: the 429 body must carry retry_after in seconds, got {body}")
    # The client stores the raw lowercase wire names in a plain dict.
    headers = {str(k).lower(): v for k, v in (opts.ca.last_response.headers or {}).items()}
    header = headers.get("retry-after")
    assert_eq(header, str(wait), f"{what}: Retry-After must match the body, got {header!r}")


@th.django_unit_setup()
def setup_code_attempts(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.totp import UserTOTP
    from mojo.decorators import limits
    import pyotp

    opts.ca_ip = _new_ip()
    opts.ca = _own_client(opts)
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
    limits.clear_code_sends("phone_register", SEND_PHONE)
    limits.clear_code_attempts("phone_register", SEND_PHONE)

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


@th.django_unit_test("code limit: a refusal a hair before the window ends is still a refusal")
def test_refusal_at_the_window_edge_is_not_an_uncounted_try(opts):
    from mojo import errors as merrors
    from mojo.decorators import limits

    account = f"ca-{uuid.uuid4().hex}"
    start = time.time() - WINDOW
    for _ in range(LIMIT):
        limits.check_code_attempt("sms", account, now=start)
    # Less than a millisecond of the window is left. The wait rounds to zero,
    # and a zero wait must not be read as "the try was let through".
    for _ in range(2):
        try:
            limits.check_code_attempt("sms", account, now=start + WINDOW - 0.0004)
            wait = None
        except merrors.RateLimitException as err:
            wait = err.retry_after
        assert_true(wait is not None, "a try inside the window must be refused, however little of it is left")
        assert_true(wait >= 1, f"a refusal must report at least one second, got {wait}")
    limits.clear_code_attempts("sms", account)


@th.django_unit_test("code limit: with both authenticator caps full, the wait reported is the longer one")
def test_wait_is_the_longest_of_the_full_buckets(opts):
    from mojo import errors as merrors
    from mojo.decorators import limits

    account = f"ca-{uuid.uuid4().hex}"
    now = time.time()
    for _ in range(LIMIT):
        limits.check_code_attempt("totp", account, daily_limit=LIMIT, now=now)
    try:
        limits.check_code_attempt("totp", account, daily_limit=LIMIT, now=now + 1)
        wait = None
    except merrors.RateLimitException as err:
        wait = err.retry_after
    assert_true(wait is not None, "the sixth try must be refused")
    assert_true(wait > WINDOW, f"retrying after the wait reported must not be refused again by the "
                               f"daily cap: got {wait}, the daily cap needs about 86,400")
    assert_eq(_count("totp", account), LIMIT, "a refused try must not stay counted in the 15-minute bucket")
    assert_eq(_count("totp_daily", account), LIMIT, "a refused try must not stay counted in the daily bucket")
    limits.clear_code_attempts("totp", account)


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
    opts.ca.logout()
    return opts.ca.post("/api/auth/sms/verify", {"username": "ca_sms", "code": code})


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
                                     f"got {resp.status_code}: {opts.ca.last_response.body}")
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
    client = _own_client(opts)
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
                                     f"got {resp.status_code}: {opts.ca.last_response.body}")


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

def _ghost_name():
    """A username no account has. Letters only: a name holding ten digits
    reads as a phone number and is counted in that form."""
    return "ca_ghost_" + "".join(chr(97 + int(c, 16)) for c in uuid.uuid4().hex[:10])


def _new_ip():
    octets = uuid.uuid4().int
    return "10.%d.%d.%d" % ((octets >> 16) & 0xFF, (octets >> 8) & 0xFF, octets & 0xFF)


def _post_reset(opts, code, new_password, username="ca_reset"):
    # The endpoint allows five requests per address in five minutes, and this
    # module makes more than that: each request comes from its own address.
    opts.ca.logout()
    return opts.ca.post("/api/auth/password/reset/code", {
        "username": username, "code": code, "new_password": new_password},
        headers={"X-Real-IP": _new_ip()})


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
                                         f"got {resp.status_code}: {opts.ca.last_response.body}")
        assert_true("weak" in str(opts.ca.last_response.body.get("error", "")).lower(),
                    f"try {attempt} must fail on the password, not the code: "
                    f"{opts.ca.last_response.body}")
        assert_eq(_count("reset", pk), 0, "the counter must be cleared as soon as the code matches")

    resp = _post_reset(opts, RIGHT, STRONG_PWORD)
    assert_eq(resp.status_code, 200, f"the reset must succeed after five weak passwords, "
                                     f"got {resp.status_code}: {opts.ca.last_response.body}")
    assert_true(_fresh(pk).check_password(STRONG_PWORD), "the new password must be set")


@th.django_unit_test("reset code: an unknown account is limited the same way, so the 429 can't tell them apart")
def test_reset_unknown_account_is_counted_like_a_real_one(opts):
    from mojo.apps.account.models import User
    from mojo.decorators import limits

    ghost = _ghost_name()
    assert_true(not User.objects.filter(username=ghost).exists(), "the test needs a name with no account")
    ghost_id = limits.unknown_account_id(ghost)

    resp = _post_reset(opts, WRONG, STRONG_PWORD, username=ghost.upper())
    assert_eq(resp.status_code, 400, f"a try for an unknown account must be refused, got {resp.status_code}")
    assert_eq(opts.ca.last_response.body.get("error"), "Invalid code",
              "an unknown account must answer exactly as a wrong code does")
    assert_eq(_count("reset", ghost_id), 1,
              "a try for an unknown account must be counted, whatever the case it was typed in")

    _spend("reset", ghost_id, LIMIT - 1)
    resp = _post_reset(opts, WRONG, STRONG_PWORD, username=ghost)
    _assert_refused(opts, resp, "reset code for an unknown account")
    limits.clear_code_attempts("reset", ghost_id)


# -----------------------------------------------------------------
# Phone and email verify codes (signed in)
# -----------------------------------------------------------------

def _verify_flow(opts, kind, path, code_key, ts_key, ip_key):
    pk = opts.ca_verify_id
    _clear(pk)
    _seed(pk, **{code_key: RIGHT, ts_key: int(time.time())})
    assert_true(opts.ca.login("ca_verify", PWORD), "the user must be able to log in")

    resp = opts.ca.post(path, {"code": WRONG})
    assert_eq(resp.status_code, 400, f"{kind}: a wrong code must be refused, got {resp.status_code}")
    assert_eq(_count(kind, pk), 1, f"{kind}: a wrong code must be counted against the account")

    _spend(kind, pk, LIMIT - 1)
    resp = opts.ca.post(path, {"code": RIGHT})
    _assert_refused(opts, resp, kind)
    assert_eq(_fresh(pk).get_secret(code_key), RIGHT, f"{kind}: a refused try must not consume the code")

    _clear(pk)
    _spend(kind, pk, 2)
    resp = opts.ca.post(path, {"code": RIGHT})
    assert_eq(resp.status_code, 200, f"{kind}: the right code inside the limit must verify, "
                                     f"got {resp.status_code}: {opts.ca.last_response.body}")
    assert_eq(_count(kind, pk), 0, f"{kind}: a right code must clear the account's counter")


@th.django_unit_test("phone verify code: counted, refused at the limit, cleared on a match")
def test_phone_verify_code_limit(opts):
    _verify_flow(opts, "phone_verify", "/api/auth/verify/phone/confirm",
                 "phone_verify_code", "phone_verify_ts", "phone_verify_confirm")


@th.django_unit_test("email verify code: counted, refused at the limit, cleared on a match")
def test_email_verify_code_limit(opts):
    _verify_flow(opts, "email_verify", "/api/auth/verify/email/confirm",
                 "email_verify_code", "email_verify_code_ts", "email_verify_code_confirm")


@th.django_unit_test("verify codes: a match clears the counter even if saving the result then fails")
def test_match_clears_the_counter_before_anything_can_fail(opts):
    from mojo.apps.account.utils import tokens

    pk = opts.ca_verify_id

    def failing_save(*args, **kwargs):
        raise RuntimeError("the database is away")

    cases = (
        ("email_verify", tokens.verify_email_verify_code,
         dict(email_verify_code=RIGHT, email_verify_code_ts=int(time.time()))),
        ("phone_verify", tokens.verify_phone_verify_code,
         dict(phone_verify_code=RIGHT, phone_verify_ts=int(time.time()))),
        ("email_change", tokens.verify_email_change_otp,
         dict(pending_email=NEW_EMAIL, email_change_otp=RIGHT, email_change_otp_ts=int(time.time()))),
    )
    for kind, verify, secrets in cases:
        _clear(pk)
        _seed(pk, **secrets)
        _spend(kind, pk, LIMIT - 1)
        user = _fresh(pk)
        user.save = failing_save
        failed = False
        try:
            verify(user, RIGHT)
        except RuntimeError:
            failed = True
        assert_true(failed, f"{kind}: the test needs the save after the match to fail")
        assert_eq(_count(kind, pk), 0, f"{kind}: a right code must clear the counter at the match, "
                                       "or five failed saves lock out the person who has the code")
    _clear(pk)
    _seed(pk, pending_email=None, email_change_otp=None, email_change_otp_ts=None,
          email_verify_code=None, email_verify_code_ts=None, phone_verify_code=None, phone_verify_ts=None)


@th.django_unit_test("email change code: counted, refused at the limit, cleared on a match")
def test_email_change_code_limit(opts):
    pk = opts.ca_change_id
    _clear(pk)

    def seed():
        _seed(pk, pending_email=NEW_EMAIL, email_change_otp=RIGHT,
              email_change_otp_ts=int(time.time()))

    seed()
    assert_true(opts.ca.login("ca_change", PWORD), "the user must be able to log in")
    resp = opts.ca.post("/api/auth/email/change/confirm", {"code": WRONG})
    assert_eq(resp.status_code, 400, f"a wrong code must be refused, got {resp.status_code}")
    assert_eq(_count("email_change", pk), 1, "a wrong email-change code must be counted")

    _spend("email_change", pk, LIMIT - 1)
    resp = opts.ca.post("/api/auth/email/change/confirm", {"code": RIGHT})
    _assert_refused(opts, resp, "email change")
    assert_eq(_fresh(pk).email, "ca_change@example.com", "a refused try must not change the email")

    _clear(pk)
    _spend("email_change", pk, 2)
    resp = opts.ca.post("/api/auth/email/change/confirm", {"code": RIGHT})
    assert_eq(resp.status_code, 200, f"the right code inside the limit must change the email, "
                                     f"got {resp.status_code}: {opts.ca.last_response.body}")
    assert_eq(_count("email_change", pk), 0, "a right code must clear the account's counter")
    opts.ca.logout()


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
    opts.ca.logout()
    return opts.ca.post("/api/auth/totp/verify", {"mfa_token": _mfa_token(pk), "code": code})


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
                                     f"got {resp.status_code}: {opts.ca.last_response.body}")
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
    opts.ca.logout()

    resp = opts.ca.post("/api/auth/totp/login", {"username": "ca_totp", "code": "000000"})
    assert_eq(resp.status_code, 401, f"a wrong code must be refused, got {resp.status_code}")
    assert_eq(_count("totp_login", pk), 1, "a wrong authenticator sign-in code must be counted")
    password_tries = read_account_attempt("login", pk, limit=10, window=900)["count"]
    assert_eq(password_tries, 0, "a wrong authenticator code must not use up password tries")

    _spend("totp_login", pk, LIMIT - 1)
    resp = opts.ca.post("/api/auth/totp/login", {
        "username": "ca_totp", "code": _totp_now(opts.ca_totp_secret)})
    _assert_refused(opts, resp, "totp login")
    assert_eq(_count("totp", pk), 0, "sign-in tries must not lock the second-step check")

    _clear(pk)
    resp = opts.ca.post("/api/auth/totp/login", {
        "username": "ca_totp", "code": _totp_now(opts.ca_totp_secret)})
    assert_eq(resp.status_code, 200, f"after the counter is cleared the right code must sign in, "
                                     f"got {resp.status_code}: {opts.ca.last_response.body}")
    opts.ca.logout()


@th.django_unit_test("authenticator set-up checks: their own bucket, so typos can't lock sign-in")
def test_totp_manage_limit(opts):
    pk = opts.ca_manage_id
    _clear(pk)
    assert_true(opts.ca.login("ca_manage", PWORD), "the user must be able to log in")

    resp = opts.ca.post("/api/account/totp/confirm", {"code": "000000"})
    assert_eq(resp.status_code, 400, f"a wrong confirm code must be refused, got {resp.status_code}")
    resp = opts.ca.post("/api/account/totp/recovery-codes/regenerate", {"code": "000000"})
    assert_eq(resp.status_code, 403, f"a wrong regenerate code must be refused, got {resp.status_code}")
    resp = opts.ca.post("/api/user/me", {"confirm_totp": {"code": "000000"}})
    assert_eq(resp.status_code, 400, f"a wrong confirm action code must be refused, got {resp.status_code}")
    resp = opts.ca.post("/api/user/me", {"regenerate_totp_codes": {"code": "000000"}})
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
        resp = opts.ca.post(path, body)
        _assert_refused(opts, resp, f"{path} {sorted(body)}")

    _clear(pk)
    resp = opts.ca.post("/api/user/me", {"regenerate_totp_codes": {"code": _totp_now(opts.ca_manage_secret)}})
    assert_eq(resp.status_code, 200, f"the right code inside the limit must work, "
                                     f"got {resp.status_code}: {opts.ca.last_response.body}")
    assert_eq(_count("totp_manage", pk), 0, "a right code must clear the account's counter")
    opts.ca.logout()


# -----------------------------------------------------------------
# Phone sign-up code (no account yet: keyed on the phone number)
# -----------------------------------------------------------------

def _post_register_verify(opts, session_token, code):
    return opts.ca.post("/api/auth/phone/register/verify", {
        "session_token": session_token, "code": code})


@th.django_unit_test("phone sign-up code: counted per phone number, across sessions")
def test_phone_register_code_limit(opts):
    from mojo.apps.account.services import phone_register
    from mojo.decorators import limits

    limits.clear_code_attempts("phone_register", REGISTER_PHONE)
    opts.ca.logout()
    session, code, _ = phone_register.start(REGISTER_PHONE)
    wrong = "000000" if code != "000000" else "111111"

    resp = _post_register_verify(opts, session, wrong)
    assert_eq(resp.status_code, 400, f"a wrong code must be refused, got {resp.status_code}")
    assert_eq(_count("phone_register", REGISTER_PHONE), 1,
              "a wrong sign-up code must be counted against the phone number")

    resp = _post_register_verify(opts, session, code)
    assert_eq(resp.status_code, 200, f"wrong-then-right inside the limit must verify, "
                                     f"got {resp.status_code}: {opts.ca.last_response.body}")
    assert_eq(_count("phone_register", REGISTER_PHONE), 0, "a right code must clear the counter")

    # A new session for the same number does not start a new count.
    _spend("phone_register", REGISTER_PHONE)
    session, code, _ = phone_register.start(REGISTER_PHONE)
    resp = _post_register_verify(opts, session, code)
    _assert_refused(opts, resp, "phone register verify")
    limits.clear_code_attempts("phone_register", REGISTER_PHONE)


# -----------------------------------------------------------------
# Sends: a stranger can't replace the code being typed, or text an account
# without limit. Driven in-process through the endpoints' `send` seams, since
# a send can't be seen from the other side of HTTP.
# -----------------------------------------------------------------

class _Sender:
    """Stand-in for the SMS or email transport, injected through a seam."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _accepted_sms()


def _accepted_sms():
    from mojo.apps.phonehub.models import SMS
    return SMS(direction="outbound", from_number="+15550000000", to_number=SEND_PHONE,
               body="code", status="sent")


def _request(path, data):
    """A RequestFactory POST carrying the attributes mojo middleware stamps."""
    from django.test import RequestFactory
    from objict import objict
    from mojo.middleware.mojo import ANONYMOUS_USER

    request = RequestFactory(REMOTE_ADDR=SEND_IP).post(path, {})
    request.DATA = objict(data)
    request.ip = SEND_IP
    request.user = ANONYMOUS_USER
    request.bearer = None
    request.group = None
    request.duid = None
    request.muid = None
    request.user_agent = "testit"
    return request


def _body(response):
    return json.loads(response.content)


def _sms_login(sender):
    from mojo.apps.account.rest import sms as sms_rest
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip=SEND_IP, key="sms_login")
    response = sms_rest.on_sms_login(_request("/api/auth/sms/login", {"username": "ca_send"}), send=sender)
    assert_eq(response.status_code, 200, f"sms login must answer 200, got {response.status_code}")
    assert_eq(_body(response).get("message"), GENERIC_SMS, "sms login must give its one uniform answer")


def _reset_sends(pk):
    from mojo.decorators import limits
    _clear(pk)
    for kind in ("sms", "reset"):
        limits.clear_code_sends(kind, pk)
    _seed(pk, sms_otp_code=None, sms_otp_ts=None, password_reset_code=None, password_reset_code_ts=None)


@th.django_unit_test("sms login send: a repeat request re-sends the live code, it does not replace it")
def test_sms_login_resends_the_live_code(opts):
    pk = opts.ca_send_id
    _reset_sends(pk)
    sender = _Sender()

    _sms_login(sender)
    first = _fresh(pk).get_secret("sms_otp_code")
    first_ts = _fresh(pk).get_secret("sms_otp_ts")
    assert_true(first, "the first request must store a code")
    _sms_login(sender)
    assert_eq(len(sender.calls), 2, "both requests inside the limit must send")
    assert_eq(_fresh(pk).get_secret("sms_otp_code"), first,
              "a second request must not replace the code a user may be typing")
    assert_eq(_fresh(pk).get_secret("sms_otp_ts"), first_ts,
              "a re-send must not extend the code's life")
    assert_eq(sender.calls[0][0][1], sender.calls[1][0][1], "the second text must carry the same code")

    # An expired code is replaced.
    _seed(pk, sms_otp_ts=int(time.time()) - 3600)
    _sms_login(sender)
    assert_true(int(_fresh(pk).get_secret("sms_otp_ts")) > int(time.time()) - 60,
                "an expired code must be replaced by one with a fresh time")
    _reset_sends(pk)


@th.django_unit_test("sms login send: the sixth send in 15 minutes sends nothing and answers the same")
def test_sms_login_send_cap(opts):
    pk = opts.ca_send_id
    _reset_sends(pk)
    sender = _Sender()
    for _ in range(SEND_LIMIT):
        _sms_login(sender)
    assert_eq(len(sender.calls), SEND_LIMIT, "five sends must go out")
    _sms_login(sender)
    _sms_login(sender)
    assert_eq(len(sender.calls), SEND_LIMIT, "a send beyond the cap must send nothing")
    _reset_sends(pk)


@th.django_unit_test("sms login send: nothing is sent while the account's code entry is locked")
def test_sms_login_sends_nothing_while_locked(opts):
    pk = opts.ca_send_id
    _reset_sends(pk)
    _spend("sms", pk)
    sender = _Sender()
    _sms_login(sender)
    assert_eq(len(sender.calls), 0, "a code sent while entry is locked could expire before the lock ends")
    assert_eq(_fresh(pk).get_secret("sms_otp_code"), None, "no code must be minted while locked")
    _reset_sends(pk)


def _forgot(sms_sender, email_sender, channel=None):
    from mojo.apps.account.rest import user as user_rest
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip=SEND_IP, key="auth_forgot")
    data = {"username": "ca_send", "method": "code"}
    if channel:
        data["channel"] = channel
    response = user_rest.on_user_forgot(_request("/api/auth/forgot", data),
                                        send_sms=sms_sender, send_email=email_sender)
    assert_eq(response.status_code, 200, f"forgot must answer 200, got {response.status_code}")
    assert_eq(_body(response).get("status"), True, "forgot must give its one uniform answer")


@th.django_unit_test("forgot (code): re-sends the live code, caps sends, and sends nothing while locked")
def test_forgot_code_sends(opts):
    pk = opts.ca_send_id
    _reset_sends(pk)
    sms, email = _Sender(), _Sender()

    _forgot(sms, email)
    first = _fresh(pk).get_secret("password_reset_code")
    assert_true(first, "the first request must store a reset code")
    assert_eq((len(sms.calls), len(email.calls)), (0, 1), "the code goes by email by default")
    _forgot(sms, email, channel="sms")
    assert_eq(_fresh(pk).get_secret("password_reset_code"), first,
              "a second request must not replace the reset code, whatever the channel")
    assert_eq((len(sms.calls), len(email.calls)), (1, 1), "the second request must send by SMS")
    assert_true(first in sms.calls[0][0][1], "the text must carry the same code")

    for _ in range(SEND_LIMIT - 2):
        _forgot(sms, email)
    assert_eq(len(sms.calls) + len(email.calls), SEND_LIMIT, "five sends must go out")
    _forgot(sms, email)
    _forgot(sms, email, channel="sms")
    assert_eq(len(sms.calls) + len(email.calls), SEND_LIMIT, "a send beyond the cap must send nothing")

    _reset_sends(pk)
    _spend("reset", pk)
    sms, email = _Sender(), _Sender()
    _forgot(sms, email)
    assert_eq(len(sms.calls) + len(email.calls), 0, "nothing must be sent while reset-code entry is locked")
    assert_eq(_fresh(pk).get_secret("password_reset_code"), None, "no code must be minted while locked")
    _reset_sends(pk)


@th.django_unit_test("forgot (code) over HTTP: a second request keeps the code")
def test_forgot_code_over_http_keeps_the_code(opts):
    pk = opts.ca_send_id
    _reset_sends(pk)
    opts.ca.logout()
    codes = []
    for _ in range(2):
        resp = opts.ca.post("/api/auth/forgot", {"username": "ca_send", "method": "code"})
        assert_eq(resp.status_code, 200, f"forgot must answer 200, got {resp.status_code}: "
                                         f"{opts.ca.last_response.body}")
        codes.append(_fresh(pk).get_secret("password_reset_code"))
    assert_true(codes[0], "the first request must store a reset code")
    assert_eq(codes[1], codes[0], "a second request must not replace the reset code")
    _reset_sends(pk)


def _register_start(sender):
    from mojo.apps.account.rest import sms as sms_rest
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip=SEND_IP, key="phone_register_start")
    response = sms_rest.on_phone_register_start(
        _request("/api/auth/phone/register/start", {"phone": SEND_PHONE}), send=sender)
    assert_eq(response.status_code, 200, f"register start must answer 200, got {response.status_code}")
    data = _body(response)["data"]
    assert_true(len(data.get("session_token", "")) == 32 and data.get("expires_in", 0) > 0,
                f"register start must give its usual answer, got {data}")


@th.django_unit_test("phone sign-up send: capped per phone number, and nothing is sent while locked")
def test_phone_register_send_cap(opts):
    from mojo.decorators import limits

    limits.clear_code_sends("phone_register", SEND_PHONE)
    limits.clear_code_attempts("phone_register", SEND_PHONE)
    sender = _Sender()
    for _ in range(SEND_LIMIT):
        _register_start(sender)
    assert_eq(len(sender.calls), SEND_LIMIT, "five sends must go out")
    _register_start(sender)
    assert_eq(len(sender.calls), SEND_LIMIT, "a sixth text to one number in 15 minutes must not be sent")

    limits.clear_code_sends("phone_register", SEND_PHONE)
    _spend("phone_register", SEND_PHONE)
    sender = _Sender()
    _register_start(sender)
    assert_eq(len(sender.calls), 0, "nothing must be sent while that number's code entry is locked")
    limits.clear_code_sends("phone_register", SEND_PHONE)
    limits.clear_code_attempts("phone_register", SEND_PHONE)


@th.django_unit_test("admin release: clears the send counters too")
def test_admin_release_clears_send_counters(opts):
    pk = opts.ca_send_id
    _reset_sends(pk)
    sender = _Sender()
    for _ in range(SEND_LIMIT + 1):
        _sms_login(sender)
    assert_eq(len(sender.calls), SEND_LIMIT, "setup: the cap must be reached")

    admin = _admin_client(opts)
    resp = admin.post("/api/auth/manage/clear_rate_limit", {"key": "login", "username": "ca_send"})
    assert_eq(resp.status_code, 200, f"the admin release must succeed, got {resp.status_code}: {resp.response}")
    _sms_login(sender)
    assert_eq(len(sender.calls), SEND_LIMIT + 1, "after the release a code must be sent again")
    _reset_sends(pk)


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


@th.django_unit_test("reset code: an expired code never works, and a wrong guess can't tell it is there")
def test_expired_reset_code_answers_like_an_unknown_account(opts):
    other_pword = "ca##Other55Reset"
    pk = opts.ca_reset_id
    _clear(pk)
    _seed(pk, password_reset_code=RIGHT, password_reset_code_ts=int(time.time()) - 3600)

    resp = _post_reset(opts, WRONG, other_pword)
    assert_eq(resp.status_code, 400, f"a wrong guess must be refused, got {resp.status_code}")
    assert_eq(opts.ca.last_response.body.get("error"), "Invalid code",
              "a wrong guess at an account with a stale code must answer exactly as an unknown "
              "account does, or the first guess tells which accounts exist")

    resp = _post_reset(opts, RIGHT, other_pword)
    assert_eq(resp.status_code, 400, f"an expired code must be refused, got {resp.status_code}")
    assert_eq(opts.ca.last_response.body.get("error"), "Expired code",
              "the person holding the code that was sent is still told it has expired")
    assert_true(not _fresh(pk).check_password(other_pword), "an expired code must not change the password")
    assert_eq(_count("reset", pk), 2, "an expired code is not a match: both tries stay counted")
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


# -----------------------------------------------------------------
# Review 63853: the reset endpoint and several identifiers; overlapping sends
# -----------------------------------------------------------------

def _post_reset_fields(opts, code, **fields):
    opts.ca.logout()
    return opts.ca.post("/api/auth/password/reset/code",
                        dict(fields, code=code, new_password=STRONG_PWORD),
                        headers={"X-Real-IP": _new_ip()})


def _typed_count(value):
    """The reset counter for one typed identifier, read as support tooling would."""
    from mojo.decorators import limits
    return _count("reset", limits.unknown_account_id(value))


def _clear_typed(*values):
    from mojo.decorators import limits
    for value in values:
        limits.clear_code_attempts("reset", limits.unknown_account_id(value))


@th.django_unit_test("reset code: a second identifier can't be used to tell a real account from an unknown one")
def test_reset_extra_identifier_cannot_tell_accounts_apart(opts):
    from mojo.apps.account.models import User

    pk = opts.ca_reset_id
    ghost = _ghost_name()
    assert_true(not User.objects.filter(username=ghost).exists(), "the test needs a name with no account")
    _clear(pk)
    _clear_typed("ca_reset", ghost)
    _seed_reset(pk)

    seen = {}
    for username in ("ca_reset", ghost):
        statuses = []
        for _ in range(LIMIT + 1):
            # A fixed username with a different, unrelated email on every try.
            resp = _post_reset_fields(opts, WRONG, username=username,
                                      email=f"nobody_{uuid.uuid4().hex[:12]}@example.com")
            statuses.append(resp.status_code)
        seen[username] = statuses
    assert_eq(seen["ca_reset"], [400] * LIMIT + [429],
              f"a real account must be refused on the sixth try, got {seen['ca_reset']}")
    assert_eq(seen[ghost], seen["ca_reset"],
              "an unknown username must be answered exactly as a real one, try for try, "
              f"whatever other identifier is sent with it: got {seen[ghost]}")
    _clear(pk)
    _clear_typed("ca_reset", ghost)


@th.django_unit_test("reset code: every identifier sent is counted the same whether or not an account was found")
def test_reset_identifiers_are_counted_alike(opts):
    from mojo.apps.account.models import User

    pk = opts.ca_reset_id
    ghost = _ghost_name()
    assert_true(not User.objects.filter(username=ghost).exists(), "the test needs a name with no account")
    _clear(pk)
    _clear_typed("ca_reset", ghost)
    _seed_reset(pk)

    counts = {}
    for username in ("ca_reset", ghost):
        email = f"nobody_{uuid.uuid4().hex[:12]}@example.com"
        resp = _post_reset_fields(opts, WRONG, username=username, email=email)
        assert_eq(resp.status_code, 400, f"a wrong code must be refused, got {resp.status_code}")
        counts[username] = (_typed_count(username), _typed_count(email))
    assert_eq(counts["ca_reset"], (1, 1),
              f"with a real account each identifier sent must still be counted, got {counts['ca_reset']}")
    assert_eq(counts[ghost], counts["ca_reset"],
              "the counters left behind must not depend on whether the account exists")
    _clear(pk)
    _clear_typed("ca_reset", ghost)


@th.django_unit_test("reset code: identifiers of two different accounts in one try are each counted")
def test_reset_conflicting_identifiers(opts):
    first, second = opts.ca_reset_id, opts.ca_sms_id
    other_email = "ca_sms@example.com"
    for pk in (first, second):
        _clear(pk)
    _clear_typed("ca_reset", other_email)
    _seed_reset(first)
    _seed_reset(second)
    stored = {pk: _fresh(pk).password for pk in (first, second)}

    resp = _post_reset_fields(opts, WRONG, username="ca_reset", email=other_email)
    assert_eq(resp.status_code, 400, f"a wrong code must be refused, got {resp.status_code}")
    assert_eq(_count("reset", first) + _count("reset", second), 1,
              "the try must be counted against the one account whose code was compared")
    assert_eq((_typed_count("ca_reset"), _typed_count(other_email)), (1, 1),
              "both identifiers sent must be counted, so neither can be swapped out to get more tries")

    # Five tries naming the first account, each with the second's email.
    for _ in range(LIMIT - 1):
        _post_reset_fields(opts, WRONG, username="ca_reset", email=other_email)
    for fields in (dict(username="ca_reset"), dict(email=other_email),
                   dict(username="ca_reset", email=other_email)):
        resp = _post_reset_fields(opts, RIGHT, **fields)
        _assert_refused(opts, resp, f"reset code after five tries, sent as {sorted(fields)}")
    for pk in (first, second):
        assert_eq(_fresh(pk).password, stored[pk], "a refused reset must leave the password unchanged")
        _clear(pk)
        _seed(pk, password_reset_code=None, password_reset_code_ts=None)
    _clear_typed("ca_reset", other_email)


@th.django_unit_test("reset code: one unknown phone number typed in different ways is one counter")
def test_reset_phone_formats_share_a_counter(opts):
    from mojo.apps.account.models import User

    last4 = "%04d" % (uuid.uuid4().int % 10000)
    plain = f"555000{last4}"
    e164 = f"+1{plain}"
    assert_true(not User.objects.filter(phone_number=e164).exists(), "the test needs a number with no account")
    _clear_typed(e164)
    formats = [plain, e164, f"(555) 000-{last4}", f"555-000-{last4}", f"1 555 000 {last4}"]

    statuses = [_post_reset_fields(opts, WRONG, username=typed).status_code for typed in formats]
    assert_eq(statuses, [400] * LIMIT, f"five tries must be answered as wrong codes, got {statuses}")
    resp = _post_reset_fields(opts, WRONG, username=plain)
    _assert_refused(opts, resp, "reset code for an unknown phone number, sixth try")
    resp = _post_reset_fields(opts, WRONG, phone_number=e164)
    _assert_refused(opts, resp, "the same number sent as phone_number")
    _clear_typed(*formats)


@th.django_unit_test("reset code: a right code and an admin release clear the identifier counters too")
def test_reset_identifier_counters_are_cleared(opts):
    pk = opts.ca_reset_id
    _clear(pk)
    _clear_typed("ca_reset", "ca_reset@example.com")
    _seed_reset(pk)

    for _ in range(LIMIT):
        _post_reset_fields(opts, WRONG, username="ca_reset")
    resp = _post_reset_fields(opts, RIGHT, username="ca_reset")
    _assert_refused(opts, resp, "reset code at the limit")

    admin = _admin_client(opts)
    resp = admin.post("/api/auth/manage/clear_rate_limit", {"key": "login", "username": "ca_reset"})
    assert_eq(resp.status_code, 200, f"the admin release must succeed, got {resp.status_code}: {resp.response}")
    assert_eq(_typed_count("ca_reset"), 0, "the release must clear the counter for the name that was typed")

    resp = _post_reset_fields(opts, WRONG, username="ca_reset")
    assert_eq(resp.status_code, 400, f"after the release a try must reach the code check, got {resp.status_code}")
    resp = _post_reset_fields(opts, RIGHT, username="ca_reset")
    assert_eq(resp.status_code, 200, f"after the release the right code must work, "
                                     f"got {resp.status_code}: {opts.ca.last_response.body}")
    assert_eq(_count("reset", pk), 0, "a right code must clear the account's counter")
    assert_eq(_typed_count("ca_reset"), 0, "a right code must clear the counter for the name that was typed")
    _fresh(pk).save_password(PWORD)


# -----------------------------------------------------------------
# Review 63901: one identifier sent under different field names
# -----------------------------------------------------------------

NUM_USER = "ca_num_5550006290"
NUM_EMAIL = f"{NUM_USER}@example.com"
NUM_PHONE_FORM = "+15550006290"   # how the email above reads as a phone number


def _ghost_num_email():
    """An email no account has, whose local part holds ten digits."""
    return "ca_ghost_556%07d@example.com" % (uuid.uuid4().int % 10000000)


def _phone_form(value):
    from mojo.apps.account.models import User
    return User.normalize_phone(value)


@th.django_unit_test("reset code: one email sent as `email` or as `username` is one counter, account or not")
def test_reset_same_email_through_either_field(opts):
    from mojo.apps.account.models import User

    pk = opts.ca_num_5550006290_id
    for first, then in (("email", "username"), ("username", "email")):
        ghost = _ghost_num_email()
        assert_true(not User.objects.filter(email=ghost).exists(), "the test needs an email with no account")
        assert_true(_phone_form(ghost), "the test needs an email that also reads as a phone number")
        _clear(pk)
        _clear_typed(NUM_EMAIL, NUM_PHONE_FORM, ghost, _phone_form(ghost))
        _seed_reset(pk)

        seen = {}
        for address in (NUM_EMAIL, ghost):
            statuses = [_post_reset_fields(opts, WRONG, **{first: address}).status_code
                        for _ in range(LIMIT)]
            # The identical address, now under the other field name.
            statuses.append(_post_reset_fields(opts, WRONG, **{then: address}).status_code)
            seen[address] = statuses
        assert_eq(seen[NUM_EMAIL], [400] * LIMIT + [429],
                  f"a real account must be refused on the sixth try ({first} then {then}), got {seen[NUM_EMAIL]}")
        assert_eq(seen[ghost], seen[NUM_EMAIL],
                  f"an unknown email must be answered exactly as a real one, try for try, when the same "
                  f"address moves from `{first}` to `{then}`: got {seen[ghost]}")
        _clear(pk)
        _clear_typed(NUM_EMAIL, NUM_PHONE_FORM, ghost, _phone_form(ghost))
    _seed(pk, password_reset_code=None, password_reset_code_ts=None)


@th.django_unit_test("reset code: the counters a try leaves depend on what was typed, not on the field it was typed in")
def test_reset_counters_ignore_the_field_name(opts):
    from mojo.apps.account.models import User

    pk = opts.ca_num_5550006290_id
    ghost = _ghost_num_email()
    assert_true(not User.objects.filter(email=ghost).exists(), "the test needs an email with no account")
    left = {}
    for address, phone_form in ((NUM_EMAIL, NUM_PHONE_FORM), (ghost, _phone_form(ghost))):
        for field in ("email", "username"):
            _clear(pk)
            _clear_typed(address, phone_form)
            _seed_reset(pk)
            resp = _post_reset_fields(opts, WRONG, **{field: address})
            assert_eq(resp.status_code, 400, f"a wrong code must be refused, got {resp.status_code}")
            left[(address, field)] = (_typed_count(address), _typed_count(phone_form))
        _clear(pk)
        _clear_typed(address, phone_form)
    assert_eq(left[(NUM_EMAIL, "email")], (1, 1),
              f"an address is counted as typed and in its phone form, got {left[(NUM_EMAIL, 'email')]}")
    for key, counts in left.items():
        assert_eq(counts, left[(NUM_EMAIL, "email")],
                  f"the counters left by {key} must match those left by the real address sent as `email`")
    _seed(pk, password_reset_code=None, password_reset_code_ts=None)


@th.django_unit_test("reset code: a right code clears every counter the earlier tries used, whatever field they used")
def test_reset_right_code_clears_across_fields(opts):
    pk = opts.ca_num_5550006290_id
    _clear(pk)
    _clear_typed(NUM_EMAIL, NUM_PHONE_FORM)
    _seed_reset(pk)

    for _ in range(2):
        resp = _post_reset_fields(opts, WRONG, username=NUM_EMAIL)
        assert_eq(resp.status_code, 400, f"a wrong code must be refused, got {resp.status_code}")
    resp = _post_reset_fields(opts, RIGHT, email=NUM_EMAIL)
    assert_eq(resp.status_code, 200, f"the right code must work, got {resp.status_code}: {opts.ca.last_response.body}")
    assert_eq((_count("reset", pk), _typed_count(NUM_EMAIL), _typed_count(NUM_PHONE_FORM)), (0, 0, 0),
              "a right code must clear the account's counter and both forms of the address, "
              "or tries made under the other field name stay counted")
    _fresh(pk).save_password(PWORD)


@th.django_unit_test("reset code: an admin release clears both forms of the account's own identifiers")
def test_reset_admin_release_clears_both_forms(opts):
    pk = opts.ca_num_5550006290_id
    _clear(pk)
    _clear_typed(NUM_EMAIL, NUM_PHONE_FORM, NUM_USER)
    _seed_reset(pk)

    for _ in range(LIMIT):
        _post_reset_fields(opts, WRONG, email=NUM_EMAIL)
    resp = _post_reset_fields(opts, RIGHT, username=NUM_USER)
    _assert_refused(opts, resp, "reset code at the limit, sent under the account's username")

    admin = _admin_client(opts)
    resp = admin.post("/api/auth/manage/clear_rate_limit", {"key": "login", "username": NUM_USER})
    assert_eq(resp.status_code, 200, f"the admin release must succeed, got {resp.status_code}: {resp.response}")
    assert_eq((_count("reset", pk), _typed_count(NUM_EMAIL), _typed_count(NUM_PHONE_FORM), _typed_count(NUM_USER)),
              (0, 0, 0, 0), "the release must clear the account's counter and every form of its identifiers")
    resp = _post_reset_fields(opts, RIGHT, username=NUM_USER)
    assert_eq(resp.status_code, 200, f"after the release the right code must work, "
                                     f"got {resp.status_code}: {opts.ca.last_response.body}")
    _fresh(pk).save_password(PWORD)


@th.django_unit_test("sms code send: two requests that read the account before either wrote send one code")
def test_overlapping_sms_sends_keep_one_code(opts):
    from mojo.apps.account.rest import sms as sms_rest

    pk = opts.ca_send_id
    _reset_sends(pk)
    sender = _Sender()
    # Both requests have loaded the account before either has stored a code.
    first, second = _fresh(pk), _fresh(pk)

    sms_rest._send_otp(first, send=sender)
    stored = _fresh(pk).get_secret("sms_otp_code")
    stored_ts = _fresh(pk).get_secret("sms_otp_ts")
    sms_rest._send_otp(second, send=sender)

    assert_eq(len(sender.calls), 2, "both requests must send")
    assert_eq(sender.calls[0][0][1], sender.calls[1][0][1],
              "the second request must send the code the first one stored, not a new one")
    assert_eq(_fresh(pk).get_secret("sms_otp_code"), stored, "the stored code must not be replaced")
    assert_eq(_fresh(pk).get_secret("sms_otp_ts"), stored_ts, "the code's life must not be extended")
    assert_eq(second.get_secret("sms_otp_code"), stored,
              "the request's own copy of the account must carry the live code, or a later save would undo it")
    _reset_sends(pk)


@th.django_unit_test("reset code send: two requests that read the account before either wrote send one code")
def test_overlapping_reset_requests_keep_one_code(opts):
    from mojo.apps.account.rest import user as user_rest

    pk = opts.ca_send_id
    _reset_sends(pk)
    first, second = _fresh(pk), _fresh(pk)

    code = user_rest._reset_code_for(first)
    stored_ts = _fresh(pk).get_secret("password_reset_code_ts")
    again = user_rest._reset_code_for(second)

    assert_eq(again, code, "the second request must be given the code the first one stored, not a new one")
    assert_eq(_fresh(pk).get_secret("password_reset_code"), code, "the stored code must not be replaced")
    assert_eq(_fresh(pk).get_secret("password_reset_code_ts"), stored_ts, "the code's life must not be extended")
    _reset_sends(pk)
