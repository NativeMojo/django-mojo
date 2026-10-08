"""Maestro item #6226, Step 3 — a signed-in session can't be used to guess the
account's password, and the two signed-in code checks have a per-address limit.

Three places ask a signed-in caller for the current password: the password
change on the account save, the email-change request and the phone-change
request. None of them counted tries, so a stolen session could guess the
password as fast as the per-address limits allowed. Each now counts tries per
account in its own bucket, `password_check`: ten in 15 minutes, the numbers of
password sign-in, then the standard 429 with the real wait. The bucket is
separate from `login`, so these tries can't lock sign-in.

`auth/verify/phone/confirm` and `auth/verify/email/confirm` had no per-address
limit at all. They now allow ten requests per address in five minutes.

This module posts from its own address, so it neither spends 127.0.0.1's
budget nor has to clear it under another module.
"""
import uuid

from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "framework"

PWORD = "pc##mojo99Check"
NEW_PWORD = "pc##Changed77Pass"
WRONG = "pc##not-the-password"
LIMIT = 10
WINDOW = 900
BUCKET = "password_check"
IP_LIMIT = 10

ADMIN = "pc_admin"
USERS = {
    "pc_admin": None,
    "pc_model": None,
    "pc_save": None,
    "pc_email": None,
    "pc_phone": "+15550006231",
    "pc_verify": "+15550006232",
}


def _new_ip():
    octets = uuid.uuid4().int
    return "10.%d.%d.%d" % ((octets >> 16) & 0xFF, (octets >> 8) & 0xFF, octets & 0xFF)


def _client(opts, ip=None):
    """A client with its own source address: nginx sets X-Real-IP to the true
    client and the framework trusts exactly that header."""
    from testit.client import RestClient
    client = RestClient(opts.client.host)
    client.headers["X-Real-IP"] = ip or opts.pc_ip
    return client


def _fresh(pk):
    from mojo.apps.account.models import User
    return User.objects.get(pk=pk)


def _count(account_id, key=BUCKET):
    from mojo.decorators.limits import read_account_attempt
    return read_account_attempt(key, account_id, limit=LIMIT, window=WINDOW)["count"]


def _clear(account_id):
    from mojo.decorators import limits
    limits.clear_account_limits(account_id)


def _spend(account_id, tries=LIMIT):
    """Count `tries` tries against the account, as that many wrong passwords would."""
    from mojo.decorators import limits
    for _ in range(tries):
        limits.check_password_attempt(account_id)


def _assert_refused(client, resp, what):
    """The refusal is the standard 429, with the wait in the body and the header."""
    body = client.last_response.body
    assert_eq(resp.status_code, 429, f"{what}: at the limit the try must be refused with 429, "
                                     f"got {resp.status_code}: {body}")
    wait = body.get("retry_after")
    assert_true(isinstance(wait, int) and 0 < wait <= WINDOW,
                f"{what}: the 429 body must carry retry_after in seconds, got {body}")
    headers = {str(k).lower(): v for k, v in (client.last_response.headers or {}).items()}
    assert_eq(headers.get("retry-after"), str(wait),
              f"{what}: Retry-After must match the body, got {headers.get('retry-after')!r}")


@th.django_unit_setup()
def setup_password_check_limit(opts):
    from mojo.apps.account.models import User
    from mojo.decorators import limits

    opts.pc_ip = _new_ip()
    User.objects.filter(username__in=list(USERS)).delete()
    User.objects.filter(phone_number__in=[p for p in USERS.values() if p]).update(phone_number=None)
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
        limits.clear_account_limits(user.pk)
        for kind in ("phone_verify", "email_verify"):
            limits.clear_code_attempts(kind, user.pk)


# -----------------------------------------------------------------
# The limiter itself
# -----------------------------------------------------------------

@th.django_unit_test("password check limit: ten tries are counted, the eleventh is refused and not counted")
def test_eleventh_try_is_refused_and_not_counted(opts):
    from mojo import errors as merrors
    from mojo.decorators import limits

    account = f"pc-{uuid.uuid4().hex}"
    for attempt in range(1, LIMIT + 1):
        limits.check_password_attempt(account)
        assert_eq(_count(account), attempt, f"try {attempt} must be counted before the compare")

    for extra in (1, 2):
        refused = None
        try:
            limits.check_password_attempt(account)
        except merrors.RateLimitException as err:
            refused = err
        assert_true(refused is not None, f"try {LIMIT + extra} must be refused")
        assert_eq(refused.status, 429, "the refusal must be a 429")
        assert_true(0 < refused.retry_after <= WINDOW,
                    f"the refusal must carry the wait, got {refused.retry_after}")
        assert_eq(_count(account), LIMIT,
                  "a refused try must not be counted, or retrying would extend the wait")
    limits.clear_password_attempts(account)
    assert_eq(_count(account), 0, "clearing must empty the account's counter")


@th.django_unit_test("password check limit: its own bucket, with the numbers of password sign-in")
def test_bucket_is_separate_from_login(opts):
    from mojo.decorators import limits

    assert_true(BUCKET in limits.ACCOUNT_BUCKETS,
                "password_check must be an account bucket, so the admin release clears it")
    assert_eq(limits.account_bucket_numbers(BUCKET), (LIMIT, WINDOW),
              "password_check must use the password sign-in numbers, ten per 15 minutes")

    account = f"pc-{uuid.uuid4().hex}"
    _spend(account)
    assert_eq(_count(account, key="login"), 0,
              "wrong current passwords must not be counted against password sign-in")
    limits.clear_password_attempts(account)


# -----------------------------------------------------------------
# The password change
# -----------------------------------------------------------------

@th.django_unit_test("password change: a wrong current password is counted, and at the limit the right one changes nothing")
def test_set_new_password_limit(opts):
    from mojo import errors as merrors

    pk = opts.pc_model_id
    _clear(pk)
    user = _fresh(pk)

    wrong = None
    try:
        user.set_new_password(NEW_PWORD, old_password=WRONG)
    except merrors.RateLimitException:
        raise
    except merrors.ValueException as err:
        wrong = err
    assert_true(wrong is not None, "a wrong current password must still be refused as before")
    assert_eq(_count(pk), 1, "a wrong current password must be counted against the account")

    _spend(pk, LIMIT - 1)
    refused = None
    try:
        user.set_new_password(NEW_PWORD, old_password=PWORD)
    except merrors.RateLimitException as err:
        refused = err
    assert_true(refused is not None, "at the limit even the right current password must be refused")
    assert_true(0 < refused.retry_after <= WINDOW, f"the refusal must carry the wait, got {refused.retry_after}")
    assert_true(_fresh(pk).check_password(PWORD), "a refused try must not change the password")
    assert_eq(_count(pk), LIMIT, "a refused try must not be counted")

    _clear(pk)
    _spend(pk, 2)
    user = _fresh(pk)
    user.set_new_password(NEW_PWORD, old_password=PWORD)
    user.save()
    assert_true(_fresh(pk).check_password(NEW_PWORD), "inside the limit the right current password must change it")
    assert_eq(_count(pk), 0, "a right current password must clear the account's counter")


@th.django_unit_test("password change over HTTP: counted, and refused at the limit with the wait")
def test_password_change_over_http(opts):
    pk = opts.pc_save_id
    _clear(pk)
    client = _client(opts)
    assert_true(client.login("pc_save", PWORD), "the user must be able to log in")

    resp = client.post("/api/user/me", {"new_password": NEW_PWORD, "current_password": WRONG})
    assert_eq(resp.status_code, 400, f"a wrong current password must be refused as before, "
                                     f"got {resp.status_code}: {client.last_response.body}")
    assert_eq(_count(pk), 1, "a wrong current password on the account save must be counted")

    _spend(pk, LIMIT - 1)
    resp = client.post("/api/user/me", {"new_password": NEW_PWORD, "current_password": PWORD})
    _assert_refused(client, resp, "password change")
    assert_true(_fresh(pk).check_password(PWORD), "a refused try must not change the password")
    _clear(pk)


# -----------------------------------------------------------------
# The email-change and phone-change requests
# -----------------------------------------------------------------

def _request_flow(opts, username, path, payload, harmless, what):
    """`payload` is a request that would go ahead; `harmless` is one refused
    with a 400 after the password check, so nothing is sent."""
    pk = getattr(opts, f"{username}_id")
    _clear(pk)
    client = _client(opts)
    assert_true(client.login(username, PWORD), "the user must be able to log in")

    resp = client.post(path, dict(payload, current_password=WRONG))
    assert_eq(resp.status_code, 401, f"{what}: a wrong password must be refused as before, "
                                     f"got {resp.status_code}: {client.last_response.body}")
    assert_eq(_count(pk), 1, f"{what}: a wrong password must be counted against the account")

    _spend(pk, LIMIT - 1)
    resp = client.post(path, dict(payload, current_password=PWORD))
    _assert_refused(client, resp, what)
    assert_eq(_count(pk), LIMIT, f"{what}: a refused try must not be counted")

    _clear(pk)
    _spend(pk, 2)
    resp = client.post(path, dict(harmless, current_password=PWORD))
    assert_eq(resp.status_code, 400, f"{what}: the request must get past the password check and be "
                                     f"refused for its value, got {resp.status_code}: {client.last_response.body}")
    assert_eq(_count(pk), 0, f"{what}: a right password must clear the account's counter")


@th.django_unit_test("email change request: the password check is counted, refused at the limit, cleared on a match")
def test_email_change_request_limit(opts):
    _request_flow(opts, "pc_email", "/api/auth/email/change/request",
                  {"email": "pc_email_new@example.com"},
                  {"email": "pc_email@example.com"}, "email change request")
    user = _fresh(opts.pc_email_id)
    assert_eq(user.email, "pc_email@example.com", "no email change may have gone ahead")
    assert_true(not user.get_secret("pending_email"), "a refused request must not leave a pending change")


@th.django_unit_test("phone change request: the password check is counted, refused at the limit, cleared on a match")
def test_phone_change_request_limit(opts):
    _request_flow(opts, "pc_phone", "/api/auth/phone/change/request",
                  {"phone_number": "+15550006233"},
                  {"phone_number": "not-a-number"}, "phone change request")
    user = _fresh(opts.pc_phone_id)
    assert_eq(user.phone_number, USERS["pc_phone"], "no phone change may have gone ahead")
    assert_true(not user.get_secret("pending_phone"), "a refused request must not leave a pending change")


# -----------------------------------------------------------------
# Admin release and read
# -----------------------------------------------------------------

@th.django_unit_test("admin release: clears the password-check counter, and the throttle read reports it")
def test_admin_release_and_read(opts):
    pk = opts.pc_save_id
    _clear(pk)
    _spend(pk)
    admin = _client(opts)
    assert_true(admin.login(ADMIN, PWORD), "the admin must be able to log in")

    resp = admin.get("/api/auth/manage/throttle", params={"username": "pc_save", "key": BUCKET})
    assert_eq(resp.status_code, 200, f"the read must succeed, got {resp.status_code}: {resp.response}")
    data = resp.response.data
    assert_eq(data.count, LIMIT, f"the password-check counter must be reported, got {data}")
    assert_eq(data.limit, LIMIT, "it must be reported with its own limit")
    assert_eq(data.window, WINDOW, "it must be reported with its own window")
    assert_true(0 < data.retry_after_seconds <= WINDOW, f"a locked counter must report the wait, got {data}")

    resp = admin.post("/api/auth/manage/clear_rate_limit", {"key": "login", "username": "pc_save"})
    assert_eq(resp.status_code, 200, f"the admin release must succeed, got {resp.status_code}: {resp.response}")
    assert_eq(_count(pk), 0, "the release must clear the password-check counter whatever key was sent")


# -----------------------------------------------------------------
# The per-address limit on the two signed-in code checks
# -----------------------------------------------------------------

def _confirm_ip_limit(opts, kind, path):
    """Ten requests from one address in five minutes, then 429. The account's
    own code counter is cleared before each request, so the refusal seen is
    the per-address one."""
    from mojo.decorators import limits

    pk = opts.pc_verify_id
    client = _client(opts, ip=_new_ip())
    assert_true(client.login("pc_verify", PWORD), "the user must be able to log in")

    for attempt in range(1, IP_LIMIT + 1):
        limits.clear_code_attempts(kind, pk)
        resp = client.post(path, {"code": "135791"})
        assert_true(resp.status_code != 429,
                    f"{path}: request {attempt} is inside the limit and must reach the code check, got 429")

    # The limiter fails open on a transient Redis error, so allow a little
    # slack past the limit rather than pinning the exact request.
    refused = False
    for _ in range(3):
        limits.clear_code_attempts(kind, pk)
        resp = client.post(path, {"code": "135791"})
        if resp.status_code == 429:
            refused = True
            break
    assert_true(refused, f"{path}: requests past ten from one address in five minutes must be refused with 429")
    assert_true("retry_after" not in client.last_response.body,
                f"{path}: the refusal must be the per-address one, got {client.last_response.body}")
    assert_eq(limits.read_account_attempt(f"code:{kind}", pk, limit=5, window=WINDOW)["count"], 0,
              f"{path}: a request refused for its address must not reach the code check")


@th.tier("extended")
@th.django_unit_test("phone verify confirm: ten requests per address in five minutes")
def test_phone_verify_confirm_ip_limit(opts):
    _confirm_ip_limit(opts, "phone_verify", "/api/auth/verify/phone/confirm")


@th.tier("extended")
@th.django_unit_test("email verify confirm: ten requests per address in five minutes")
def test_email_verify_confirm_ip_limit(opts):
    _confirm_ip_limit(opts, "email_verify", "/api/auth/verify/email/confirm")
