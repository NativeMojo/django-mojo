"""Sign-ins land in the user's own security history (maestro #3329).

`jwt_login` / `group_token_login` have always written `UserLoginEvent`, which
the Security page does not read — it reads `incident.Event`. This module covers
the row that now closes that gap:

  - a real sign-in writes exactly ONE minimal `login` Event for the
    credential-verified subject, with fixed text and no request-derived
    metadata
  - attribution follows the VERIFIED user, never a stale bearer still attached
    to the login POST
  - denied / unfinished / re-issued paths write nothing: bad password,
    unfinished MFA, silent refresh, sessions-revoke re-issue, email-change
    re-issue
  - brand attribution requires an active DIRECT membership, and never changes
    whether the login itself succeeds

HARD RULE for this module: every RAW ``POST /api/login`` is preceded by
``_clear_login_limits()``. `TestClient.login()` flushes the shared
ip/muid/account login counters itself (testit/client.py) and the suite only
survives its login traffic because it does; a module that posts to /api/login
directly must flush the same counters or it bleeds 429s into every parallel
module. Tests that need the Authorization header left in place cannot use the
helper (its `logout()` would drop the header under test), so they clear by hand.

No mock.patch, no server_settings, no settings mutation: every user, group and
membership here is test-owned and uniquely named.
"""
import uuid as _uuid
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

PWORD = "Lse##mojo99"


# ===========================================================================
# Helpers
# ===========================================================================

def _clear_login_limits(opts, account_id=None):
    """Mirror TestClient.login()'s counter flush for a raw /api/login POST."""
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="login")
    muid = opts.client.session.cookies.get("_muid")
    if muid:
        clear_rate_limits(key="login", muid=muid)
    if account_id is not None:
        clear_rate_limits(key="login", account_id=account_id)
        clear_rate_limits(user_id=account_id)


def _make_user(prefix):
    from mojo.apps.account.models import User
    email = f"lse_{prefix}_{_uuid.uuid4().hex[:8]}@example.com"
    user = User.objects.create_user(username=email, email=email, password=PWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    return user


def _login_rows(uid, category="login"):
    from mojo.apps.incident.models.event import Event
    return Event.objects.filter(uid=uid, category=category)


def _count(uid, category="login"):
    return _login_rows(uid, category=category).count()


@th.django_unit_setup()
def setup_login_security_event(opts):
    opts.user = _make_user("primary")
    opts.user_id = opts.user.pk
    opts.username = opts.user.username

    opts.other = _make_user("other")
    opts.other_id = opts.other.pk
    opts.other_username = opts.other.username

    opts.revoke_user = _make_user("revoke")
    opts.revoke_id = opts.revoke_user.pk

    opts.ec_user = _make_user("ecuser")
    opts.ec_id = opts.ec_user.pk

    opts.refresh_user = _make_user("refresh")
    opts.refresh_id = opts.refresh_user.pk

    opts.attrib_user = _make_user("attrib")
    opts.attrib_id = opts.attrib_user.pk

    mfa = _make_user("mfa")
    mfa.phone_number = f"+1555{_uuid.uuid4().int % 10000000:07d}"
    mfa.is_phone_verified = True
    mfa.requires_mfa = True
    mfa.save()
    opts.mfa_user = mfa
    opts.mfa_id = mfa.pk
    opts.mfa_username = mfa.username

    # Two brands for the attribution matrix: G1 has an active direct
    # membership, G2 has none at all.
    from mojo.apps.account.models import Group
    suffix = _uuid.uuid4().hex[:8]
    opts.group_member = Group.objects.create(
        name=f"lse_brand_member_{suffix}", kind="organization")
    opts.group_member_id = opts.group_member.pk
    opts.member_row_id = opts.group_member.add_member(opts.attrib_user).pk
    opts.group_outsider = Group.objects.create(
        name=f"lse_brand_outsider_{suffix}", kind="organization")
    opts.group_outsider_id = opts.group_outsider.pk


# ===========================================================================
# The sign-in row itself
# ===========================================================================

@th.django_unit_test("sign-in writes exactly one minimal login Event")
def test_signin_records_one_login_event(opts):
    before = _count(opts.user_id)
    assert_true(opts.client.login(opts.username, PWORD),
                f"login should succeed: {opts.client.last_response}")
    opts.client.logout()

    after = _count(opts.user_id)
    assert_eq(after, before + 1,
              f"one real sign-in must write exactly ONE login Event "
              f"({before} -> {after})")

    row = _login_rows(opts.user_id).order_by("-id").first()
    assert_true(row is not None, "the login row must exist")
    assert_eq(row.category, "login",
              f"the row must use the fixed category, got {row.category!r}")
    assert_eq(row.title, "Successful login",
              f"the row must carry the fixed title, got {row.title!r}")
    assert_eq(row.details, "A sign-in completed for this account.",
              f"the row must carry the fixed details, got {row.details!r}")
    assert_eq(row.source_ip, "127.0.0.1",
              f"the row must carry the request IP, got {row.source_ip!r}")
    assert_eq(row.scope, "account",
              f"session rows stay scope=account, got {row.scope!r}")
    assert_eq(row.level, 1,
              f"a successful sign-in is informational, got level {row.level!r}")
    assert_eq(row.metadata.get("security_activity_scope"), "brand",
              f"login rows are brand-provenance, never the account marker that "
              f"keys the email-change feed exception: {row.metadata}")
    for leaked in ("bearer", "user_email", "http_query_string",
                   "http_user_agent", "user_agent", "path"):
        assert_true(leaked not in row.metadata,
                    f"the recorder is request-less, so {leaked!r} must never "
                    f"reach the row: {row.metadata}")


@th.django_unit_test("attribution follows the verified subject, not a stale bearer")
def test_attribution_is_the_verified_subject_not_the_bearer(opts):
    """The reason the recorder passes an explicit uid with request=None: the
    reporter overwrites a caller-supplied uid with request.user.id whenever the
    request is authenticated, and a client can leave A's Authorization header
    attached while posting B's credentials."""
    assert_true(opts.client.login(opts.username, PWORD),
                f"user A must sign in first: {opts.client.last_response}")
    assert_true(opts.client.access_token,
                "A's bearer must be attached for this test to mean anything")

    before_a = _count(opts.user_id)
    before_b = _count(opts.other_id)

    # NO logout — A's Authorization header rides along on B's login POST.
    _clear_login_limits(opts, account_id=opts.other_id)
    resp = opts.client.post(
        "/api/login", {"username": opts.other_username, "password": PWORD})
    assert_eq(resp.status_code, 200,
              f"B's credentials must still log B in, got {resp.status_code}: "
              f"{opts.client.last_response.body}")
    opts.client.logout()

    assert_eq(_count(opts.other_id), before_b + 1,
              "the row must be attributed to B — the credential-verified subject")
    assert_eq(_count(opts.user_id), before_a,
              "A's history must not grow because A's bearer was still attached")


# ===========================================================================
# Paths that must write NOTHING
# ===========================================================================

@th.django_unit_test("a failed password writes no login Event")
def test_failed_password_records_no_login_event(opts):
    before = _count(opts.user_id)
    _clear_login_limits(opts, account_id=opts.user_id)
    resp = opts.client.post(
        "/api/login", {"username": opts.username, "password": "Wrong##pass1"})
    assert_eq(resp.status_code, 401,
              f"bad credentials must 401, got {resp.status_code}: "
              f"{opts.client.last_response.body}")
    assert_eq(_count(opts.user_id), before,
              "a refused login must never claim a successful sign-in")


@th.django_unit_test("an unfinished MFA challenge writes no login Event")
def test_unfinished_mfa_records_no_login_event(opts):
    before = _count(opts.mfa_id)
    _clear_login_limits(opts, account_id=opts.mfa_id)
    resp = opts.client.post(
        "/api/login", {"username": opts.mfa_username, "password": PWORD})
    assert_eq(resp.status_code, 200,
              f"an MFA challenge is a 200, got {resp.status_code}: "
              f"{opts.client.last_response.body}")
    data = resp.response.data
    assert_true(data.mfa_required is True,
                f"precondition: the challenge must be issued, got {data}")
    assert_true(not data.get("access_token"),
                f"precondition: no tokens before MFA is finished, got {data}")
    assert_eq(_count(opts.mfa_id), before,
              "an unfinished MFA challenge is not a sign-in and must write no row")


@th.django_unit_test("a silent refresh writes no login Event")
def test_refresh_records_no_login_event(opts):
    from mojo.decorators.limits import clear_rate_limits

    _clear_login_limits(opts, account_id=opts.refresh_id)
    resp = opts.client.post(
        "/api/login", {"username": opts.refresh_user.username, "password": PWORD})
    assert_eq(resp.status_code, 200,
              f"the seed login must succeed, got {resp.status_code}: "
              f"{opts.client.last_response.body}")
    refresh_token = resp.response.data.refresh_token
    assert_true(bool(refresh_token),
                f"precondition: a refresh token is required, got {resp.response.data}")

    before = _count(opts.refresh_id)
    clear_rate_limits(ip="127.0.0.1", key="refresh_token")
    resp = opts.client.post("/api/refresh_token", {"refresh_token": refresh_token})
    assert_eq(resp.status_code, 200,
              f"the refresh must succeed, got {resp.status_code}: "
              f"{opts.client.last_response.body}")
    assert_true(bool(resp.response.data.access_token),
                "precondition: the refresh must actually mint an access token")
    assert_eq(_count(opts.refresh_id), before,
              "on_refresh_token never calls jwt_login — a silent refresh is "
              "structurally excluded and must add no sign-in row")


@th.django_unit_test("a sessions-revoke re-issue writes no login Event")
def test_sessions_revoke_reissue_records_no_login_event(opts):
    from mojo.decorators.limits import clear_rate_limits

    assert_true(opts.client.login(opts.revoke_user.username, PWORD),
                f"the revoke user must sign in: {opts.client.last_response}")
    before_login = _count(opts.revoke_id)
    before_revoked = _count(opts.revoke_id, category="sessions:revoked")

    clear_rate_limits(ip="127.0.0.1", key="sessions_revoke")
    resp = opts.client.post("/api/auth/sessions/revoke", {})
    assert_eq(resp.status_code, 200,
              f"the revoke must succeed, got {resp.status_code}: "
              f"{opts.client.last_response.body}")
    assert_true(bool(resp.response.data.access_token),
                "precondition: revoke re-issues a JWT for the calling session")
    opts.client.logout()

    assert_eq(_count(opts.revoke_id), before_login,
              "revoking sessions re-issues a token; it is not a new sign-in and "
              "must not say so in the user's history")
    assert_eq(_count(opts.revoke_id, category="sessions:revoked"),
              before_revoked + 1,
              "the revoke itself must still be recorded")


@th.django_unit_test("an email-change re-issue writes no login Event")
def test_email_change_reissue_records_no_login_event(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits

    user = User.objects.get(pk=opts.ec_id)
    new_email = f"lse_ec_new_{_uuid.uuid4().hex[:8]}@example.com"
    User.objects.filter(email=new_email).delete()
    token = tokens.generate_email_change_token(user, new_email)

    before_login = _count(opts.ec_id)
    before_confirmed = _count(opts.ec_id, category="email_change:confirmed")

    opts.client.logout()
    clear_rate_limits(ip="127.0.0.1", key="email_change_confirm")
    resp = opts.client.post("/api/auth/email/change/confirm", {"token": token})
    assert_eq(resp.status_code, 200,
              f"the email change must confirm, got {resp.status_code}: "
              f"{opts.client.last_response.body}")

    user.refresh_from_db()
    assert_eq(str(user.email), new_email,
              "precondition: the confirm must actually have committed the change")
    assert_eq(_count(opts.ec_id), before_login,
              "the fresh JWT an email change hands back is a re-issue, not a "
              "sign-in, and must add no login row")
    assert_eq(_count(opts.ec_id, category="email_change:confirmed"),
              before_confirmed + 1,
              "the email change itself must still be recorded")


# ===========================================================================
# Brand attribution
# ===========================================================================

@th.django_unit_test("group attribution requires an active direct membership")
def test_group_attribution_requires_active_direct_membership(opts):
    from mojo.apps.account.models.member import GroupMember

    def _signin_with_group(group_id):
        _clear_login_limits(opts, account_id=opts.attrib_id)
        resp = opts.client.post("/api/login", {
            "username": opts.attrib_user.username,
            "password": PWORD,
            "group": group_id,
        })
        assert_eq(resp.status_code, 200,
                  f"attribution must never change login eligibility — got "
                  f"{resp.status_code}: {opts.client.last_response.body}")
        opts.client.logout()
        row = _login_rows(opts.attrib_id).order_by("-id").first()
        assert_true(row is not None, "a successful sign-in must leave a row")
        return row

    # 1. A brand the user is an active direct member of — attributed.
    row = _signin_with_group(opts.group_member_id)
    assert_eq(row.group_id, opts.group_member_id,
              f"an active direct membership must attribute the row, got "
              f"{row.group_id!r}")
    assert_eq(row.metadata.get("origin_group_id"), opts.group_member_id,
              f"the brand marker must name the group: {row.metadata}")

    # 2. A brand the user has nothing to do with — the login still succeeds,
    #    the row is written unattributed. request.group is resolved from the
    #    caller-supplied id with NO membership check, so this is the guard that
    #    stops anyone filing activity into any brand's history.
    row = _signin_with_group(opts.group_outsider_id)
    assert_true(row.group_id is None,
                f"a non-member brand must never be attributed, got {row.group_id!r}")
    assert_true("origin_group_id" not in row.metadata,
                f"an unattributed row carries no origin group: {row.metadata}")

    # 3. The membership exists but is inactive — same as no membership.
    GroupMember.objects.filter(pk=opts.member_row_id).update(is_active=False)
    try:
        row = _signin_with_group(opts.group_member_id)
        assert_true(row.group_id is None,
                    f"an INACTIVE membership must not attribute the row, got "
                    f"{row.group_id!r}")
    finally:
        GroupMember.objects.filter(pk=opts.member_row_id).update(is_active=True)

    # 4. Back to active — the attribution returns, proving 3 tested the
    #    membership state and not some unrelated breakage.
    row = _signin_with_group(opts.group_member_id)
    assert_eq(row.group_id, opts.group_member_id,
              f"reactivating the membership must restore attribution, got "
              f"{row.group_id!r}")
