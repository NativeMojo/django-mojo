"""Closure cleanup and post-closure Apple refresh-token revocation."""
from unittest import mock
import uuid

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


def _user():
    from mojo.apps.account.models import User
    suffix = uuid.uuid4().hex[:10]
    return User.objects.create_user(
        username=f"closure_revoke_{suffix}",
        email=f"closure_revoke_{suffix}@example.com",
        password="close##mojo99")


def _connection(user, uid, token=None, active=True, provider="apple"):
    from mojo.apps.account.models import OAuthConnection
    row = OAuthConnection.objects.create(
        user=user, provider=provider, provider_uid=uid, is_active=active)
    if token is not None:
        row.set_secret("refresh_token", token)
        row.save(update_fields=["mojo_secrets"])
    return row


@th.django_unit_test("closure revokes every stored active Apple token after closure lands")
def test_closure_revokes_apple_tokens_after_landing(opts):
    from mojo.apps.account.services import closure

    user = _user()
    _connection(user, uuid.uuid4().hex, "apple-one")
    _connection(user, uuid.uuid4().hex, "apple-two")
    _connection(user, uuid.uuid4().hex, "inactive", active=False)
    _connection(user, uuid.uuid4().hex, "google", provider="google")
    seen = []

    def revoke(_provider, token):
        user.refresh_from_db()
        assert_true(not user.is_active, "Apple revoke must happen only after closure lands")
        seen.append(token)
        return True

    with mock.patch(
            "mojo.apps.account.services.oauth.apple.AppleOAuthProvider.revoke",
            autospec=True, side_effect=revoke):
        closure.run_account_closure(user)

    assert_eq(sorted(seen), ["apple-one", "apple-two"],
              f"closure must revoke each eligible Apple token once, got {seen}")


@th.django_unit_test("Apple revoke failure is non-blocking and reports incident")
def test_closure_revoke_failure_nonblocking(opts):
    from mojo.apps.account.services import closure

    user = _user()
    _connection(user, uuid.uuid4().hex, "apple-false")
    incidents = []
    user.report_incident = lambda details, event_type="info", **kw: incidents.append(event_type)

    with mock.patch(
            "mojo.apps.account.services.oauth.apple.AppleOAuthProvider.revoke",
            autospec=True, return_value=False):
        closure.run_account_closure(user)

    user.refresh_from_db()
    assert_true(not user.is_active, "revocation failure must not roll back landed closure")
    assert_eq(incidents, ["account:apple_revoke_failed"],
              f"revocation failure must report one incident, got {incidents}")


@th.django_unit_test("Apple revoke exception is non-blocking and reports incident")
def test_closure_revoke_exception_nonblocking(opts):
    from mojo.apps.account.services import closure

    user = _user()
    _connection(user, uuid.uuid4().hex, "apple-raises")
    incidents = []
    user.report_incident = lambda details, event_type="info", **kw: incidents.append(event_type)
    with mock.patch(
            "mojo.apps.account.services.oauth.apple.AppleOAuthProvider.revoke",
            autospec=True, side_effect=RuntimeError("network down")):
        closure.run_account_closure(user)
    assert_true(not user.__class__.objects.get(pk=user.pk).is_active,
                "revocation exception must not roll back landed closure")
    assert_eq(incidents, ["account:apple_revoke_failed"],
              f"revocation exception must report one incident, got {incidents}")


@th.unit_test("Apple provider revokes refresh token through the fixed endpoint")
def test_apple_provider_revoke_request(opts):
    from mojo.apps.account.services.oauth.apple import (
        APPLE_REVOKE_URL, AppleOAuthProvider)

    response = mock.Mock(ok=True)
    provider = AppleOAuthProvider()
    with mock.patch.object(provider, "_build_client_secret", return_value="client-secret"), \
            mock.patch("mojo.apps.account.services.oauth.apple.settings.get",
                       return_value="client-id"), \
            mock.patch("mojo.apps.account.services.oauth.apple.requests.post",
                       return_value=response) as post:
        result = provider.revoke("refresh-token")

    assert_true(result, "successful Apple response should return true")
    post.assert_called_once_with(APPLE_REVOKE_URL, data={
        "client_id": "client-id",
        "client_secret": "client-secret",
        "token": "refresh-token",
        "token_type_hint": "refresh_token",
    }, timeout=10)


@th.django_unit_test("incomplete closure never revokes Apple token")
def test_incomplete_closure_does_not_revoke(opts):
    from django.conf import settings as dj_settings
    from mojo import errors as merrors
    from mojo.apps.account.services import closure

    user = _user()
    _connection(user, uuid.uuid4().hex, "must-not-revoke")
    original = getattr(dj_settings, "ACCOUNT_CLOSURE_HANDLER", None)
    setattr(dj_settings, "ACCOUNT_CLOSURE_HANDLER",
            "tests.test_user_mgmt._closure_handlers.capture_without_anonymize")
    try:
        with mock.patch(
                "mojo.apps.account.services.oauth.apple.AppleOAuthProvider.revoke",
                autospec=True) as revoke:
            try:
                closure.run_account_closure(user)
            except merrors.ValueException:
                pass
            else:
                raise AssertionError("incomplete handler must fail closure")
            assert_true(not revoke.called, "failed closure must not revoke Apple credentials")
    finally:
        if original is None:
            delattr(dj_settings, "ACCOUNT_CLOSURE_HANDLER")
        else:
            setattr(dj_settings, "ACCOUNT_CLOSURE_HANDLER", original)


@th.django_unit_test("raising closure handler never revokes Apple token")
def test_raising_closure_does_not_revoke(opts):
    from django.conf import settings as dj_settings
    from mojo import errors as merrors
    from mojo.apps.account.services import closure

    user = _user()
    _connection(user, uuid.uuid4().hex, "must-not-revoke-on-raise")
    original = getattr(dj_settings, "ACCOUNT_CLOSURE_HANDLER", None)
    setattr(dj_settings, "ACCOUNT_CLOSURE_HANDLER",
            "tests.test_user_mgmt._closure_handlers.raising")
    try:
        with mock.patch(
                "mojo.apps.account.services.oauth.apple.AppleOAuthProvider.revoke",
                autospec=True) as revoke:
            try:
                closure.run_account_closure(user)
            except merrors.ValueException:
                pass
            else:
                raise AssertionError("raising handler must fail closure")
            assert_true(not revoke.called,
                        "raising closure handler must not revoke Apple credentials")
            user.refresh_from_db()
            assert_true(user.is_active, "raising handler must leave account active")
    finally:
        if original is None:
            delattr(dj_settings, "ACCOUNT_CLOSURE_HANDLER")
        else:
            setattr(dj_settings, "ACCOUNT_CLOSURE_HANDLER", original)


@th.django_unit_test("pii anonymize removes OAuth rows and handles linked ApiKeys by mode")
def test_pii_anonymize_oauth_and_keys(opts):
    from django.utils import timezone
    from mojo.apps.account.models import (
        ApiKey, Group, OAuthClient, OAuthCode, OAuthConnection, OAuthGrant,
        UserAPIKey)

    user = _user()
    group = Group.objects.create(name=f"Closure cleanup {user.pk}")
    group.add_member(user)
    override, _ = ApiKey.create_for_group(
        group, "override", user=user, override_user=True)
    reference, _ = ApiKey.create_for_group(group, "reference", user=user)
    _connection(user, uuid.uuid4().hex, "apple-cleanup")
    UserAPIKey.create_for_user(user, label="cleanup")
    client = OAuthClient.objects.create(
        client_id=f"cleanup-{uuid.uuid4().hex}", kind="dcr",
        client_name="Cleanup", redirect_uris=["https://example.com/cb"])
    grant = OAuthGrant.objects.create(
        user=user, client=client, access_jti=uuid.uuid4().hex,
        refresh_hash=uuid.uuid4().hex, refresh_expires=timezone.now(),
        resource="https://example.com/api")
    OAuthCode.objects.create(
        user=user, client=client, grant=grant, code_hash=uuid.uuid4().hex,
        redirect_uri="https://example.com/cb", code_challenge="challenge",
        resource="https://example.com/api", expires=timezone.now())

    summary = user.pii_anonymize()

    assert_eq(summary["deleted_oauth_connections"], 1, f"wrong OAuthConnection summary: {summary}")
    assert_eq(summary["deleted_user_api_keys"], 1, f"wrong UserAPIKey summary: {summary}")
    assert_eq(summary["deleted_oauth_grants"], 1, f"wrong OAuthGrant summary: {summary}")
    assert_eq(summary["deleted_oauth_codes"], 1, f"wrong OAuthCode summary: {summary}")
    assert_eq(summary["deactivated_api_keys"], 1, f"wrong override ApiKey summary: {summary}")
    assert_eq(summary["detached_api_keys"], 1, f"wrong reference ApiKey summary: {summary}")
    override.refresh_from_db()
    reference.refresh_from_db()
    assert_true(not override.is_active and override.user_id is None,
                "override ApiKey must be inactive and detached")
    assert_true(reference.is_active and reference.user_id is None,
                "reference ApiKey must stay active and detach")


@th.django_unit_test("account close: deployment gate disables immediate closure")
def test_account_close_setting_gate(opts):
    from mojo.apps.account.models import Setting

    user = _user()
    Setting.objects.filter(key="ALLOW_SELF_DEACTIVATION", group=None).delete()
    with th.server_settings(ALLOW_SELF_DEACTIVATION=False):
        assert_true(opts.client.login(user.username, "close##mojo99"),
                    "login must succeed")
        resp = opts.client.post(
            "/api/account/close", {"current_password": "close##mojo99"})
        opts.client.logout()
    assert_eq(resp.status_code, 403,
              f"disabled self closure must return 403, got {resp.status_code}: {resp.json}")


@th.django_unit_test("account close: OAuth api-scope bearer is forbidden")
def test_account_close_oauth_grant_forbidden(opts):
    from mojo.apps.account.models import OAuthClient, Setting
    from mojo.apps.account.services.oauth_server import tokens

    base = "https://oauth.testit.example"
    for key in ("BASE_URL", "ASSISTANT_MCP_ENABLED"):
        Setting.objects.filter(key=key, group=None).delete()
    user = _user()
    client = OAuthClient.objects.create(
        client_id=f"close-{uuid.uuid4().hex}", kind="dcr",
        client_name="Closure refusal", redirect_uris=["https://example.com/cb"])
    with th.server_settings(BASE_URL=base, ASSISTANT_MCP_ENABLED=True):
        grant = tokens.create_grant(
            user, client, ["api"], f"{base}/api", 1700000000)
        token = tokens.mint_access_token(grant)
        resp = opts.client.post(
            "/api/account/close", {"current_password": "close##mojo99"},
            headers={"Authorization": f"Bearer {token}"})
    assert_eq(resp.status_code, 403,
              f"OAuth api-scope token must not close an account, got {resp.status_code}: {resp.json}")
