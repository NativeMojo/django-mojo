"""
Failed-login throttling tests.

Covers the layered defenses on the login endpoint:
  - Per-account sliding-window cap (bypass-resistant)
  - Counter resets after a successful login
  - Independent of duid (omitting/rotating duid does not bypass the cap)
  - Legitimate mistype (3-5 wrongs then correct) is not blocked
  - Unknown-username failures do not lock real accounts
  - invalid_password events emit at level >= 5 so the new ruleset matches
  - Admin clear_rate_limit endpoint accepts username/user_id
  - manage_users permission is required for the admin clear

Tests use clear_cookies() between attempts to rotate the server-set muid
cookie so the account-tier (keyed on user.pk) is what fires the 429,
not the muid tier on the decorator.
"""
from testit import helpers as th

THROTTLE_USER = "throttle_user"
THROTTLE_PWORD = "throttle##mojo99"
WRONG_PWORD = "definitely-not-the-right-password"

CLEAR_ADMIN = "throttle_admin"
CLEAR_ADMIN_PWORD = "throttle##mojo99"

REGULAR_USER = "throttle_regular"
REGULAR_PWORD = "throttle##mojo99"


def _clear_login_state(user_id=None):
    """Clear all login rate-limit state so individual tests can isolate tiers."""
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="login")
    if user_id is not None:
        clear_rate_limits(key="login", account_id=user_id)


def _post_login(client, username, password, rotate_muid=True):
    """POST /api/login. By default rotates the client cookies between calls
    so the muid-tier on the decorator does not fire and the per-account tier
    is the only tier that can produce a 429 in the test scenarios below."""
    if rotate_muid:
        client.clear_cookies()
    return client.post("/api/login", dict(username=username, password=password))


@th.django_unit_setup()
def setup_throttle_users(opts):
    from mojo.apps.account.models import User

    User.objects.filter(username__in=[THROTTLE_USER, CLEAR_ADMIN, REGULAR_USER]).delete()

    user = User(username=THROTTLE_USER, display_name=THROTTLE_USER,
                email=f"{THROTTLE_USER}@example.com")
    user.save()
    user.is_email_verified = True
    user.save_password(THROTTLE_PWORD)
    user.remove_all_permissions()
    opts.throttle_user_id = user.pk

    admin = User(username=CLEAR_ADMIN, display_name=CLEAR_ADMIN,
                 email=f"{CLEAR_ADMIN}@example.com")
    admin.save()
    admin.is_email_verified = True
    admin.add_permission(["manage_users"])
    admin.is_staff = True
    admin.save_password(CLEAR_ADMIN_PWORD)
    opts.admin_id = admin.pk

    regular = User(username=REGULAR_USER, display_name=REGULAR_USER,
                   email=f"{REGULAR_USER}@example.com")
    regular.save()
    regular.is_email_verified = True
    regular.save_password(REGULAR_PWORD)
    regular.remove_all_permissions()
    opts.regular_user_id = regular.pk

    _clear_login_state(opts.throttle_user_id)
    _clear_login_state(opts.regular_user_id)


# -----------------------------------------------------------------
# Per-account cap: bypass-resistant 429
# -----------------------------------------------------------------

@th.django_unit_test("login throttle: account cap blocks at threshold")
def test_per_account_cap_blocks_at_threshold(opts):
    _clear_login_state(opts.throttle_user_id)

    for attempt in range(1, 11):
        resp = _post_login(opts.client, THROTTLE_USER, WRONG_PWORD)
        assert resp.status_code == 401, (
            f"attempt {attempt}: expected 401 (wrong password) before threshold, "
            f"got {resp.status_code}: {resp.response}"
        )

    resp = _post_login(opts.client, THROTTLE_USER, WRONG_PWORD)
    assert resp.status_code == 429, (
        f"attempt 11: expected 429 from per-account cap, got {resp.status_code}: {resp.response}"
    )


@th.django_unit_test("login throttle: success clears account counter")
def test_per_account_cap_clears_on_success(opts):
    _clear_login_state(opts.throttle_user_id)

    # 9 wrongs (one below threshold)
    for attempt in range(1, 10):
        resp = _post_login(opts.client, THROTTLE_USER, WRONG_PWORD)
        assert resp.status_code == 401, (
            f"warm-up attempt {attempt}: expected 401, got {resp.status_code}"
        )

    # Correct password — counter should be cleared on success
    resp = _post_login(opts.client, THROTTLE_USER, THROTTLE_PWORD)
    assert resp.status_code == 200, (
        f"correct password before threshold should succeed; got {resp.status_code}: {resp.response}"
    )

    # 9 more wrongs must again be allowed (counter was reset)
    for attempt in range(1, 10):
        resp = _post_login(opts.client, THROTTLE_USER, WRONG_PWORD)
        assert resp.status_code == 401, (
            f"post-reset attempt {attempt}: expected 401 (counter reset), got {resp.status_code}"
        )


@th.django_unit_test("login throttle: omitting/rotating duid cannot bypass account cap")
def test_per_account_cap_independent_of_duid(opts):
    _clear_login_state(opts.throttle_user_id)

    # 10 wrongs each with a fresh duid value — duid tier should never trip
    # (per-duid count stays at 1) and the per-account tier still catches us.
    for attempt in range(1, 11):
        opts.client.clear_cookies()
        resp = opts.client.post(
            "/api/login",
            dict(username=THROTTLE_USER, password=WRONG_PWORD, duid=f"rotate-{attempt}"),
        )
        assert resp.status_code == 401, (
            f"duid-rotation attempt {attempt}: expected 401 before threshold, "
            f"got {resp.status_code}"
        )

    opts.client.clear_cookies()
    resp = opts.client.post(
        "/api/login",
        dict(username=THROTTLE_USER, password=WRONG_PWORD, duid="rotate-final"),
    )
    assert resp.status_code == 429, (
        f"with rotating duid, account cap must still trip on attempt 11; "
        f"got {resp.status_code}: {resp.response}"
    )


@th.django_unit_test("login throttle: legitimate mistype (3 wrong, 1 right) is not blocked")
def test_legit_mistype_then_success(opts):
    _clear_login_state(opts.regular_user_id)

    for attempt in range(1, 4):
        resp = _post_login(opts.client, REGULAR_USER, WRONG_PWORD)
        assert resp.status_code == 401, (
            f"legit mistype {attempt}: expected 401, got {resp.status_code}"
        )

    resp = _post_login(opts.client, REGULAR_USER, REGULAR_PWORD)
    assert resp.status_code == 200, (
        f"legit user with correct password after mistypes must succeed; "
        f"got {resp.status_code}: {resp.response}"
    )


@th.django_unit_test("login throttle: unknown username does not lock real account")
def test_unknown_username_does_not_lock_account(opts):
    _clear_login_state(opts.throttle_user_id)

    # Spray a username that does not exist — these attempts must NOT increment
    # the per-account counter for any real user (user resolution fails first).
    for attempt in range(1, 12):
        resp = _post_login(opts.client, "nobody-here-zzz", WRONG_PWORD)
        assert resp.status_code == 401, (
            f"unknown-username attempt {attempt}: expected 401, got {resp.status_code}"
        )

    # The real user's account counter must still be empty — login with correct
    # credentials succeeds even after 11 unknown-username failures.
    resp = _post_login(opts.client, THROTTLE_USER, THROTTLE_PWORD)
    assert resp.status_code == 200, (
        f"real account must still be reachable after unknown-username spray; "
        f"got {resp.status_code}: {resp.response}"
    )


@th.django_unit_test("login throttle: pre-threshold 401 response shape unchanged")
def test_response_shape_pre_threshold_unchanged(opts):
    _clear_login_state(opts.throttle_user_id)

    resp = _post_login(opts.client, THROTTLE_USER, WRONG_PWORD)
    assert resp.status_code == 401, (
        f"first wrong password must return 401 (not 429), got {resp.status_code}"
    )
    body = resp.response or {}
    error_text = (body.get("error") or "").lower()
    assert "invalid" in error_text, (
        f"pre-threshold 401 should keep generic 'invalid' message, got: {body}"
    )


# -----------------------------------------------------------------
# Incident events emitted at level >= 5 (feeds the new ruleset)
# -----------------------------------------------------------------

@th.django_unit_test("login throttle: invalid_password events emit at level >= 5")
def test_invalid_password_level_emits_event_for_rule(opts):
    from mojo.apps.incident.models.event import Event
    _clear_login_state(opts.throttle_user_id)

    # Filter by details substring — login emits "<username> enter an invalid
    # password" so we can scope to this user without relying on uid (the user
    # is unauthenticated at the time of the event).
    needle = THROTTLE_USER
    baseline = Event.objects.filter(
        category="invalid_password",
        level__gte=5,
        details__contains=needle,
    ).count()

    for _ in range(5):
        resp = _post_login(opts.client, THROTTLE_USER, WRONG_PWORD)
        assert resp.status_code == 401, (
            f"setup attempt expected 401, got {resp.status_code}"
        )

    after = Event.objects.filter(
        category="invalid_password",
        level__gte=5,
        details__contains=needle,
    ).count()
    assert (after - baseline) >= 5, (
        f"expected at least 5 new invalid_password events at level>=5 mentioning "
        f"{needle!r}, got delta={after - baseline}"
    )


@th.django_unit_test("login throttle: invalid_password ruleset is registered")
def test_invalid_password_ruleset_present(opts):
    from mojo.apps.incident.models.rule import RuleSet
    RuleSet.ensure_auth_rules()
    rs = RuleSet.objects.filter(category="invalid_password").first()
    assert rs is not None, "ensure_auth_rules() must create an invalid_password ruleset"
    assert rs.is_active, "invalid_password ruleset should be active"
    assert (rs.handler or "").startswith("block://"), (
        f"invalid_password ruleset should use a block:// handler, got {rs.handler!r}"
    )
    rules = list(rs.rules.all())
    assert any(r.field_name == "level" for r in rules), (
        "invalid_password ruleset must include a level rule"
    )


# -----------------------------------------------------------------
# Admin clear_rate_limit endpoint
# -----------------------------------------------------------------

@th.django_unit_test("login throttle: admin can clear per-account counter by username")
def test_admin_clear_per_account_counter(opts):
    _clear_login_state(opts.throttle_user_id)

    # Trip the cap from one client (cookies are session-scoped on RestClient,
    # so we use the shared opts.client for the failing attempts).
    for _ in range(10):
        _post_login(opts.client, THROTTLE_USER, WRONG_PWORD)

    resp = _post_login(opts.client, THROTTLE_USER, WRONG_PWORD)
    assert resp.status_code == 429, (
        f"setup: account cap should be tripped, got {resp.status_code}"
    )

    # Admin clears via username
    from testit.client import RestClient
    admin_client = RestClient(opts.client.host)
    assert admin_client.login(CLEAR_ADMIN, CLEAR_ADMIN_PWORD), "admin login failed"
    clear_resp = admin_client.post(
        "/api/auth/manage/clear_rate_limit",
        dict(key="login", username=THROTTLE_USER),
    )
    assert clear_resp.status_code == 200, (
        f"admin clear should succeed, got {clear_resp.status_code}: {clear_resp.response}"
    )
    assert clear_resp.response.data.deleted >= 1, (
        f"admin clear should report >=1 deleted key, got {clear_resp.response.data}"
    )

    # Per-account counter cleared — next attempt must be 401, not 429
    resp = _post_login(opts.client, THROTTLE_USER, WRONG_PWORD)
    assert resp.status_code == 401, (
        f"after admin clear, attempt should be allowed (401 wrong password); "
        f"got {resp.status_code}: {resp.response}"
    )


@th.django_unit_test("login throttle: admin clear requires manage_users")
def test_admin_clear_requires_manage_users(opts):
    _clear_login_state(opts.throttle_user_id)

    # Regular user (no manage_users) cannot clear
    from testit.client import RestClient
    regular_client = RestClient(opts.client.host)
    assert regular_client.login(REGULAR_USER, REGULAR_PWORD), "regular login failed"
    resp = regular_client.post(
        "/api/auth/manage/clear_rate_limit",
        dict(key="login", username=THROTTLE_USER),
    )
    assert resp.status_code in (401, 403), (
        f"regular user must not be able to clear rate limits; got {resp.status_code}: {resp.response}"
    )
