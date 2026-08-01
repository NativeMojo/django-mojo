"""DM-042: disabling a user is an INSTANT kill switch.

Pre-DM-042, validate_jwt never checked is_active and disable_entity never
rotated auth_key — a disabled abuser's live JWTs kept authenticating until
natural expiry. These regressions pin the new contract:

  - disable -> the old JWT dies on the very next request (401, generic error)
  - reactivate -> old tokens STILL dead (auth_key rotated); fresh login works
  - user_api_key JWTs of a disabled user are rejected too
  - revoke_sessions still rotates (and now also drops live websockets)
"""
import uuid as _uuid

from testit import helpers as th


def _make_user(email_prefix):
    from mojo.apps.account.models import User
    email = f"{email_prefix}_{_uuid.uuid4().hex[:8]}@killswitch.test"
    password = "Dm042##kill"
    User.objects.filter(username=email).delete()
    user = User.objects.create_user(username=email, email=email, password=password)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    return user, email, password


@th.django_unit_test()
def test_disable_kills_live_jwt_and_reactivate_does_not_resurrect(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import disable as disable_service

    user, email, password = _make_user("dm042_kill")
    ok = opts.client.login(email, password)
    assert ok, f"login failed for kill-switch user: {opts.client.last_response.body}"
    old_token = opts.client.access_token

    resp = opts.client.get("/api/user/me")
    assert resp.status_code == 200, f"live token must work before disable, got {resp.status_code}"

    old_auth_key = User.objects.get(pk=user.pk).auth_key
    disable_service.disable_entity(user, reason="abuse", note="DM-042 test")

    fresh = User.objects.get(pk=user.pk)
    assert fresh.is_active is False, "disable_entity must flip is_active"
    assert fresh.auth_key != old_auth_key, (
        "disable_entity must rotate auth_key so old tokens die even after reactivation"
    )

    resp = opts.client.get("/api/user/me")
    assert resp.status_code == 401, (
        f"disabled user's live JWT must be rejected on the next request, got {resp.status_code}"
    )
    error = (opts.client.last_response.body or {}).get("error", "")
    assert "disabled" not in str(error).lower(), (
        f"rejection must be generic — no account-state oracle, got {error!r}"
    )

    # Reactivate: the account works again, but ONLY via re-authentication.
    disable_service.reactivate_entity(user)
    opts.client.access_token = old_token
    opts.client.is_authenticated = True
    resp = opts.client.get("/api/user/me")
    assert resp.status_code == 401, (
        f"pre-disable token must STAY dead after reactivation (rotation), got {resp.status_code}"
    )
    ok = opts.client.login(email, password)
    assert ok, f"fresh login after reactivation must work: {opts.client.last_response.body}"
    resp = opts.client.get("/api/user/me")
    assert resp.status_code == 200, (
        f"fresh token after reactivation must work, got {resp.status_code}"
    )
    opts.client.logout()


@th.django_unit_test()
def test_validate_jwt_rejects_disabled_user_generic_error(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import disable as disable_service

    user, email, password = _make_user("dm042_vjwt")
    ok = opts.client.login(email, password)
    assert ok, f"login failed: {opts.client.last_response.body}"
    token = opts.client.access_token
    opts.client.logout()

    validated, error = User.validate_jwt(token)
    assert validated is not None and error is None, f"sanity: token must validate, got {error!r}"

    disable_service.disable_entity(user, reason="admin")
    validated, error = User.validate_jwt(token)
    assert validated is None, "validate_jwt must not return a disabled user"
    assert error == "Invalid token user", (
        f"disabled-user error must match the missing-user error (no oracle), got {error!r}"
    )


@th.django_unit_test()
def test_user_api_key_of_disabled_user_rejected(opts):
    from mojo.apps.account.models import User, UserAPIKey
    from mojo.apps.account.services import disable as disable_service

    user, email, password = _make_user("dm042_uak")
    package = UserAPIKey.create_for_user(user, expire_days=7, label="DM-042 kill test")
    assert package.token, "sanity: create_for_user must return a token"

    validated, error = User.validate_jwt(package.token)
    assert validated is not None and error is None, (
        f"sanity: user_api_key token must validate while active, got {error!r}"
    )

    disable_service.disable_entity(user, reason="abuse")
    validated, error = User.validate_jwt(package.token)
    assert validated is None, "user_api_key of a disabled user must be rejected"
    assert error is not None, "rejection must carry an error"


@th.django_unit_test()
def test_revoke_sessions_still_rotates(opts):
    """disconnect_realtime was added to the revoke path — the rotation contract
    must be unchanged and the realtime call must never break it."""
    from mojo.apps.account.models import User

    user, email, password = _make_user("dm042_revoke")
    ok = opts.client.login(email, password)
    assert ok, f"login failed: {opts.client.last_response.body}"
    old_token = opts.client.access_token

    resp = opts.client.post(f"/api/user/{user.pk}", {"revoke_sessions": True})
    assert resp.status_code == 200, (
        f"revoke_sessions action should succeed for self, got {resp.status_code}: {resp.response}"
    )

    opts.client.access_token = old_token
    opts.client.is_authenticated = True
    resp = opts.client.get("/api/user/me")
    assert resp.status_code == 401, (
        f"old JWT must be dead after revoke_sessions, got {resp.status_code}"
    )
    opts.client.logout()


@th.django_unit_test()
def test_realtime_disconnect_failure_files_suppressed_incident(opts):
    """Item 1100: a realtime disconnect failure during disable/revoke files ONE
    suppressed level-6 incident (per user per window) and NEVER raises — the
    kill-switch guarantee is auth_key rotation, not the socket drop."""
    from unittest import mock

    from mojo.apps.account.services import disable as disable_service
    from mojo.apps.incident.models import Event
    from mojo.apps.incident import notice_key
    from mojo.helpers.redis import get_connection

    user, email, password = _make_user("dm042_rtfail")
    category = "account:realtime_disconnect_failed"
    key = f"account:disable:realtime_alerted:{user.pk}"

    Event.objects.filter(category=category, model_id=user.pk).delete()
    try:
        get_connection().delete(notice_key(category, key))
    except Exception:
        pass

    with mock.patch("mojo.apps.realtime.manager.disconnect_user",
                    side_effect=RuntimeError("realtime down")):
        # Must not raise — a realtime failure is hygiene loss, never an auth hole.
        raised = False
        try:
            disable_service.disconnect_realtime(user)
            disable_service.disconnect_realtime(user)
        except Exception as exc:
            raised = True
            last = exc
        assert not raised, (
            f"disconnect_realtime must swallow a realtime failure, but it raised "
            f"{last!r}"
        )

    events = list(Event.objects.filter(category=category, model_id=user.pk))
    assert len(events) == 1, (
        f"a realtime disconnect failure must file exactly ONE suppressed incident "
        f"per user per window, got {len(events)}: {[e.title for e in events]}"
    )
    assert events[0].level == 6, (
        f"a partial kill switch (auth_key rotated, socket not dropped) is a "
        f"level-6 incident, got level {events[0].level}"
    )
    details = events[0].details or ""
    assert str(user.pk) in details, (
        f"the incident must name the affected user pk: {details!r}"
    )
    assert "RuntimeError" in details and "realtime down" in details, (
        f"the incident must bind the exception type and message: {details!r}"
    )
    Event.objects.filter(category=category, model_id=user.pk).delete()
