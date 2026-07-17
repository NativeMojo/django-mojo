"""Post-credential geofence enforcement (DM-043).

Login-flow geofencing runs AFTER credential verification (inside jwt_login /
the MFA branch of on_user_login), so:
  - invalid credentials from a blocked geo → the normal 401 (never a 403)
  - valid credentials from a blocked geo → geofence 403 with ZERO login side
    effects (no last_login, no UserLoginEvent) and the verified user on the
    geofence_block event
  - bypass_geofence holders log in from anywhere (the point of DM-043)
  - MFA users are blocked BEFORE receiving an mfa_token; MFA finishers are
    blocked at jwt_login
  - exempt sources (sessions_revoke) still work from a blocked geo

Header-driven like the rest of the module (see _helpers). Event assertions use
the datacenter_detected reason — unused by decorator.py (country/tor) and
evidence_plane.py (region/vpn/rule_invalid/lookup/allowlist), so parallel
modules can't steal our dedupe slot.
"""
import uuid as _uuid
from testit import helpers as th
from tests.test_geofence._helpers import headers, GEO_RU, GEO_DATACENTER

IP = "127.0.0.1"
BLOCK_US_ONLY = {"country": {"in": ["US"]}}


def _clear_evt(reason, ip=IP):
    from mojo.helpers.redis import get_connection
    get_connection().delete(f"geofence:evt:{ip}:{reason}")


def _make_user(email, password, **flags):
    from mojo.apps.account.models import User
    # Long-lived DB: remove any leftover row before creating.
    User.objects.filter(username=email).delete()
    user = User.objects.create_user(username=email, email=email, password=password)
    user.is_email_verified = True
    user.requires_mfa = False
    for k, v in flags.items():
        setattr(user, k, v)
    user.save()
    return user


def _login(opts, username, password, **header_kwargs):
    # Clear ip + muid login buckets (mirrors evidence_plane) so repeated
    # direct posts don't trip the strict login limiter.
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip=IP, key="login")
    muid = opts.client.session.cookies.get("_muid")
    if muid:
        clear_rate_limits(key="login", muid=muid)
    return opts.client.post(
        "/api/auth/login", {"username": username, "password": password},
        headers=headers(**header_kwargs))


@th.django_unit_setup()
def setup_post_auth(opts):
    suffix = _uuid.uuid4().hex[:8]
    opts.password = "Geo##post99"
    opts.plain_email = f"geofence_pa_plain_{suffix}@geofence.test"
    opts.bypass_email = f"geofence_pa_bypass_{suffix}@geofence.test"
    opts.mfa_email = f"geofence_pa_mfa_{suffix}@geofence.test"
    opts.reset_email = f"geofence_pa_reset_{suffix}@geofence.test"
    opts.revoke_email = f"geofence_pa_revoke_{suffix}@geofence.test"

    opts.plain_user_id = _make_user(opts.plain_email, opts.password).pk
    bypass_user = _make_user(opts.bypass_email, opts.password)
    bypass_user.add_permission("bypass_geofence")
    opts.bypass_user_id = bypass_user.pk
    mfa_user = _make_user(
        opts.mfa_email, opts.password,
        requires_mfa=True, phone_number=f"+1555{suffix[:7]}", is_phone_verified=True)
    opts.mfa_user_id = mfa_user.pk
    opts.reset_user_id = _make_user(opts.reset_email, opts.password).pk
    opts.revoke_user_id = _make_user(opts.revoke_email, opts.password).pk


@th.django_unit_test("post-auth: invalid credentials from blocked geo return the normal 401")
def test_blocked_geo_invalid_creds_401(opts):
    """The new ordering contract: credentials are checked FIRST; a blocked geo
    must not change the invalid-credentials response (no enumeration signal)."""
    resp = _login(opts, opts.plain_email, "Wrong##pass1",
                  geo=GEO_RU, system_rules=BLOCK_US_ONLY)
    assert resp.status_code == 401, \
        f"invalid creds from blocked geo must 401 (not geofence 403), got {resp.status_code}: {opts.client.last_response.body}"


@th.django_unit_test("post-auth: valid credentials from blocked geo → 403, zero login side effects, uid on event")
def test_blocked_geo_valid_creds_403_no_side_effects(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.login_event import UserLoginEvent

    _clear_evt("datacenter_detected")
    resp = _login(opts, opts.plain_email, opts.password,
                  geo=GEO_DATACENTER, system_rules={"abuse": {"datacenter": False}})
    assert resp.status_code == 403, \
        f"valid creds from blocked geo must 403, got {resp.status_code}: {opts.client.last_response.body}"
    body = opts.client.last_response.body
    assert body.get("error") == "geofence_blocked", f"403 must be the geofence body, got: {body}"
    assert "access_token" not in str(body), f"blocked login must not leak tokens: {body}"

    user = User.objects.get(pk=opts.plain_user_id)
    assert user.last_login is None, \
        f"blocked login must NOT stamp last_login, got {user.last_login!r}"
    assert UserLoginEvent.objects.filter(user=user).count() == 0, \
        "blocked login must NOT record a UserLoginEvent"

    # Evidence: the block event carries the credential-verified user (DM-043).
    from mojo.apps.incident.models import Event
    ev = Event.objects.filter(
        category="geofence_block",
        metadata__username=opts.plain_email).order_by("-id").first()
    assert ev is not None, "block must emit a geofence_block event with the verified username"
    assert ev.uid == opts.plain_user_id, \
        f"block event must carry the verified uid, got {ev.uid!r}"
    assert ev.metadata.get("geofence_scope") == "auth", \
        f"event must keep scope=auth, got {ev.metadata.get('geofence_scope')!r}"
    assert ev.source_ip == IP, f"event must keep source_ip, got {ev.source_ip!r}"


@th.django_unit_test("post-auth: bypass_geofence user logs in from a blocked geo")
def test_bypass_user_logs_in_from_blocked_geo(opts):
    """The point of DM-043 — per-user whitelisting works at login."""
    resp = _login(opts, opts.bypass_email, opts.password,
                  geo=GEO_RU, system_rules=BLOCK_US_ONLY)
    assert resp.status_code == 200, \
        f"bypass_geofence user must log in from RU, got {resp.status_code}: {opts.client.last_response.body}"
    body = opts.client.last_response.body
    assert (body.get("data") or {}).get("access_token"), \
        f"bypass login must return tokens, got: {body}"


@th.django_unit_test("post-auth: blocked MFA user gets 403 and NO mfa_token")
def test_blocked_mfa_user_gets_no_challenge(opts):
    resp = _login(opts, opts.mfa_email, opts.password,
                  geo=GEO_RU, system_rules=BLOCK_US_ONLY)
    assert resp.status_code == 403, \
        f"blocked MFA user must 403 before the challenge, got {resp.status_code}: {opts.client.last_response.body}"
    assert "mfa_token" not in str(opts.client.last_response.body), \
        f"blocked MFA user must NOT receive an mfa_token: {opts.client.last_response.body}"


@th.django_unit_test("post-auth: MFA finish (sms verify) from blocked geo is blocked at jwt_login")
def test_mfa_finish_blocked_geo(opts):
    """An mfa_token minted from an allowed geo cannot be redeemed from a
    blocked one — the finisher goes through the jwt_login choke point."""
    from mojo.apps.account.models import User
    from mojo.apps.account.services import mfa as mfa_service
    from mojo.helpers import dates

    user = User.objects.get(pk=opts.mfa_user_id)
    mfa_token = mfa_service.create_mfa_token(user, ["sms"])
    user.set_secret("sms_otp_code", "123456")
    user.set_secret("sms_otp_ts", int(dates.utcnow().timestamp()))
    user.save()

    resp = opts.client.post(
        "/api/auth/sms/verify", {"mfa_token": mfa_token, "code": "123456"},
        headers=headers(geo=GEO_RU, system_rules=BLOCK_US_ONLY))
    assert resp.status_code == 403, \
        f"MFA finish from blocked geo must 403, got {resp.status_code}: {opts.client.last_response.body}"
    assert opts.client.last_response.body.get("error") == "geofence_blocked", \
        f"must be the geofence 403 body, got: {opts.client.last_response.body}"


@th.django_unit_test("post-auth: sessions_revoke is an exempt source and works from a blocked geo")
def test_sessions_revoke_exempt_from_blocked_geo(opts):
    """A user already holding a session must be able to revoke their sessions
    (a security action) even while in a blocked geo."""
    assert opts.client.login(opts.revoke_email, opts.password), \
        "revoke user login failed (needed to hold a session)"
    resp = opts.client.post(
        "/api/auth/sessions/revoke", {},
        headers=headers(geo=GEO_RU, system_rules=BLOCK_US_ONLY))
    assert resp.status_code == 200, \
        f"sessions_revoke must work from a blocked geo (exempt source), got {resp.status_code}: {opts.client.last_response.body}"
    opts.client.logout()


@th.django_unit_test("post-auth: password reset from blocked geo applies the reset but withholds the session")
def test_password_reset_blocked_geo_no_session(opts):
    """Accepted DM-043 behavior: the emailed code proves the reset; only the
    auto-login is geofenced."""
    from mojo.apps.account.models import User
    from mojo.helpers import dates
    from mojo.decorators.limits import clear_rate_limits

    user = User.objects.get(pk=opts.reset_user_id)
    user.set_secret("password_reset_code", "654321")
    user.set_secret("password_reset_code_ts", int(dates.utcnow().timestamp()))
    user.save()

    clear_rate_limits(ip=IP, key="password_reset_code")
    new_password = "Geo##reset42"
    resp = opts.client.post(
        "/api/auth/password/reset/code",
        {"username": opts.reset_email, "code": "654321", "new_password": new_password},
        headers=headers(geo=GEO_RU, system_rules=BLOCK_US_ONLY))
    assert resp.status_code == 403, \
        f"reset from blocked geo must withhold the session (403), got {resp.status_code}: {opts.client.last_response.body}"
    assert opts.client.last_response.body.get("error") == "geofence_blocked", \
        f"must be the geofence 403 body, got: {opts.client.last_response.body}"

    user = User.objects.get(pk=opts.reset_user_id)
    assert user.check_password(new_password), \
        "the password reset itself must have been applied (token-proven action)"


@th.django_unit_test("post-auth: deferred endpoints stay in the audit registry, annotated after_auth")
def test_registry_annotates_after_auth(opts):
    """GET /api/geo/rules enforced_endpoints must keep deferred endpoints and
    distinguish them from pre-view ones (compliance artifact).

    Probe views give this test deterministic pre-view/deferred fixtures
    independent of how the real endpoints are decorated. (They originally
    worked around auth decorators OVERWRITING the shared SECURITY_REGISTRY
    entry when stacked above @requires_geofence — fixed in DM-044, all
    registration sites merge now; see tests/test_geofence/registry.py.)
    on_user_login doubles as the real-world anchor.
    """
    from mojo import decorators as md

    @md.requires_geofence(scope="dm043_probe")
    def dm043_preview_probe(request):
        pass

    @md.requires_geofence(scope="dm043_probe", after_auth=True)
    def dm043_deferred_probe(request):
        pass

    # Importing the rest modules populates SECURITY_REGISTRY in-process.
    import mojo.apps.account.rest.user  # noqa: F401
    from mojo.apps.account.rest.geofence import _enforced_endpoints

    entries = {e["endpoint"]: e for e in _enforced_endpoints()}
    preview = next((e for k, e in entries.items() if k.endswith(".dm043_preview_probe")), None)
    deferred = next((e for k, e in entries.items() if k.endswith(".dm043_deferred_probe")), None)
    login = next((e for k, e in entries.items() if k.endswith(".on_user_login")), None)

    assert deferred is not None, "deferred endpoint must appear in enforced_endpoints"
    assert deferred.get("after_auth") is True, \
        f"deferred endpoint must be annotated after_auth, got {deferred}"
    assert deferred.get("scope") == "dm043_probe", f"scope must survive, got {deferred}"
    assert preview is not None, "pre-view endpoint must appear in enforced_endpoints"
    assert "after_auth" not in preview, \
        f"pre-view endpoint must NOT carry after_auth, got {preview}"
    assert login is not None and login.get("after_auth") is True, \
        f"on_user_login must be listed and annotated after_auth, got {login}"
