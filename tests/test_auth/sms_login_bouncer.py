"""Maestro item #6226, Step 4 — SMS sign-in carries the same bouncer check as
password sign-in.

`auth/sms/login` texts a code to an account's phone and was the one way to
start a sign-in that never looked at the bouncer token. It now runs the same
check as `POST /api/login`: log-only by default, and where a deployment turns
enforcement on (BOUNCER_REQUIRE_TOKEN, or a group's require_bouncer_token) a
request without a valid `login` token is refused before anything is sent.

Enforcement is switched per request with the gated test-mode header, so no
server setting is touched. This module posts from its own address.
"""
import json
import shutil
import subprocess
import uuid
from pathlib import Path

from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "core"

ROOT = Path(__file__).resolve().parents[2]
PWORD = "sb##mojo99Bouncer"
USERNAME = "sb_user"
PHONE = "+15550006234"
DUID = "sb-duid-6226"
GENERIC = "If the account exists, a code was sent."
ENFORCE = {"X-Mojo-Test-Bouncer-Require-Token": "1"}


def _new_ip():
    octets = uuid.uuid4().int
    return "10.%d.%d.%d" % ((octets >> 16) & 0xFF, (octets >> 8) & 0xFF, octets & 0xFF)


def _fresh(pk):
    from mojo.apps.account.models import User
    return User.objects.get(pk=pk)


def _reset(opts):
    """No live code and no counters, so a send that happens is a new code."""
    from mojo.decorators import limits
    user = _fresh(opts.sb_user_id)
    user.set_secret("sms_otp_code", None)
    user.set_secret("sms_otp_ts", None)
    user.save()
    limits.clear_account_limits(user.pk)


def _token(opts, page_type):
    from mojo.apps.account.services.bouncer.token_manager import TokenManager
    return TokenManager.issue(duid=DUID, fingerprint_id="", ip=opts.sb_ip,
                              risk_score=5, page_type=page_type)


def _post(opts, headers=None, **extra):
    opts.sb.logout()
    return opts.sb.post("/api/auth/sms/login", dict({"username": USERNAME, "duid": DUID}, **extra),
                        headers=headers)


@th.django_unit_setup()
def setup_sms_login_bouncer(opts):
    from mojo.apps.account.models import User
    from testit.client import RestClient

    opts.sb_ip = _new_ip()
    opts.sb = RestClient(opts.client.host)
    opts.sb.headers["X-Real-IP"] = opts.sb_ip
    User.objects.filter(username=USERNAME).delete()
    User.objects.filter(phone_number=PHONE).update(phone_number=None)
    user = User(username=USERNAME, display_name=USERNAME, email=f"{USERNAME}@example.com")
    user.phone_number = PHONE
    user.save()
    user.save_password(PWORD)
    opts.sb_user_id = user.pk
    _reset(opts)


@th.django_unit_test("sms login: with enforcement on, a request with no bouncer token is refused and sends nothing")
def test_enforced_without_token_is_refused(opts):
    _reset(opts)
    resp = _post(opts, headers=ENFORCE)
    assert_eq(resp.status_code, 403, f"with enforcement on and no token the request must be refused, "
                                     f"got {resp.status_code}: {opts.sb.last_response.body}")
    assert_true(not _fresh(opts.sb_user_id).get_secret("sms_otp_code"),
                "a refused request must not create or send a code")


@th.django_unit_test("sms login: with enforcement on, a valid login token is accepted")
def test_enforced_with_login_token_sends(opts):
    _reset(opts)
    resp = _post(opts, headers=ENFORCE, bouncer_token=_token(opts, "login"))
    assert_eq(resp.status_code, 200, f"a valid login token must be accepted, "
                                     f"got {resp.status_code}: {opts.sb.last_response.body}")
    assert_eq(opts.sb.last_response.body.get("message"), GENERIC, "the answer must be the usual uniform one")
    assert_true(_fresh(opts.sb_user_id).get_secret("sms_otp_code"),
                "with a valid token the code must be created and sent")


@th.django_unit_test("sms login: a token issued for another page is refused where enforcement is on")
def test_enforced_with_other_page_token_is_refused(opts):
    _reset(opts)
    resp = _post(opts, headers=ENFORCE, bouncer_token=_token(opts, "registration"))
    assert_eq(resp.status_code, 403, f"a registration token must not open SMS sign-in, "
                                     f"got {resp.status_code}: {opts.sb.last_response.body}")
    assert_true(not _fresh(opts.sb_user_id).get_secret("sms_otp_code"),
                "a refused request must not create or send a code")


@th.django_unit_test("sms login: with enforcement off, nothing changes for a caller with no token")
def test_log_only_still_sends(opts):
    _reset(opts)
    resp = _post(opts)
    assert_eq(resp.status_code, 200, f"in log-only mode a request with no token must go ahead as before, "
                                     f"got {resp.status_code}: {opts.sb.last_response.body}")
    assert_eq(opts.sb.last_response.body.get("message"), GENERIC, "the answer must be the usual uniform one")
    assert_true(_fresh(opts.sb_user_id).get_secret("sms_otp_code"),
                "in log-only mode the code must still be created and sent")


@th.tier("framework")  # needs Node.js, like the other hosted-client tests
@th.django_unit_test("hosted pages: SMS sign-in asks for a fresh login token, as password sign-in does")
def test_hosted_client_sends_a_fresh_token(opts):
    node = shutil.which("node")
    assert_true(node, "Node.js is required for the hosted-auth client test")
    result = subprocess.run(
        [node, str(Path(__file__).with_suffix(".js")),
         str(ROOT / "mojo/apps/account/static/account/mojo-auth.js")],
        capture_output=True, text=True, timeout=20)
    assert_eq(result.returncode, 0, f"the client harness must run: {result.stderr}")
    seen = json.loads(result.stdout)

    assert_eq(seen["asked"], ["login"], "SMS sign-in must ask the token provider for one login token")
    assert_eq(len(seen["with_provider"]), 1, "SMS sign-in must post once")
    post = seen["with_provider"][0]
    assert_true(post["url"].endswith("/api/auth/sms/login"), f"it must post to the SMS sign-in endpoint, got {post['url']}")
    assert_eq(post["body"].get("bouncer_token"), "fresh-token", "the post must carry the token the provider gave")
    assert_eq(post["body"].get("username"), "+15555550100", "the phone number must still be sent as username")
    assert_eq(post["body"].get("group_uuid"), "g-1", "the group must still be sent")

    plain = seen["without_provider"]
    assert_eq(len(plain), 1, "with no provider SMS sign-in must still post once")
    assert_eq(plain[0]["body"].get("username"), "+15555550100", "with no provider the payload must be unchanged")
    assert_true("bouncer_token" not in plain[0]["body"], "with no provider and no page token none is sent")
