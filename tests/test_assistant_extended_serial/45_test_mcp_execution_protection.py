"""Delegated generic model writes retain sensitive target-specific protections."""
from unittest import mock

from testit import helpers as th


@th.django_unit_test("delegated stale OAuth cannot disable TOTP through a generic model save")
def test_generic_save_retains_approval(opts):
    import json
    from mojo.apps.account.models import OAuthClient, User
    from mojo.apps.account.models.totp import UserTOTP
    from mojo.apps.account.services import fresh_auth
    from mojo.apps.account.services.oauth_server import tokens
    from mojo.apps.assistant.mcp import server
    from mojo.apps.assistant.services import agent

    email = "testit-mcp-protected-save@example.com"
    client_id = "testit-mcp-protected-save"
    User.objects.filter(email=email).delete()
    OAuthClient.objects.filter(client_id=client_id).delete()
    user = User.objects.create_user(username=email, email=email, password="TestPass1!")
    for name in ["view_admin", "assistant", "manage_users"]:
        user.add_permission(name)
    client = OAuthClient.objects.create(
        client_id=client_id, kind="dcr", client_name="Protected save test",
        redirect_uris=["http://127.0.0.1:8998/callback"])
    totp = UserTOTP.objects.create(user=user, is_enabled=True)
    grant = tokens.create_grant(user, client, ["mcp", "mcp:execute"],
                                "https://oauth.testit.example/api/assistant/mcp", 1700000000)
    token = tokens.mint_access_token(grant)
    request = th.get_mock_request(user=user, path="/api/assistant/mcp", method="POST")
    request.bearer = "bearer"
    from objict import objict
    request.auth_token = objict(token=token)
    request.oauth_grant = grant
    original_get = fresh_auth.settings.get

    def enabled_get(name, default=None, **kwargs):
        return {"FRESH_AUTH_ENFORCE": True, "FRESH_AUTH_WINDOW": 60}.get(
            name, original_get(name, default, **kwargs))

    try:
        with mock.patch.object(fresh_auth.settings, "get", side_effect=enabled_get):
            th.assert_true(not fresh_auth.is_fresh(request), "the OAuth session must be stale")
            result = server.call_tool(
                "save_model_instance", {"app_name": "account", "model_name": "User",
                                        "pk": user.pk, "data": {"disable_totp": {}}},
                user, grant, agent._build_request_meta(request),
                hide_infrastructure=False, _reporter=lambda *args, **kwargs: None)
        payload = json.loads(result["content"][0]["text"])
        th.assert_eq(payload.get("status"), "approval_required",
                     "generic model actions must retain per-action protection")
        totp.refresh_from_db()
        th.assert_true(totp.is_enabled, "delegation must not disable MFA through generic saves")
    finally:
        user.delete()
        client.delete()
