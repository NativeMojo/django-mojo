"""Real ORM and MCP regressions for the user readers' lazy model imports (#9351)."""
import json

from testit import helpers as th

TESTIT_TIER = "bug"
PREFIX = "testit-user-readers-9351-"
SENSITIVE_FIELDS = {"password", "auth_key", "onetime_code"}


@th.django_unit_setup()
def setup_user_readers(opts):
    from mojo.apps.account.models import Group, GroupMember, OAuthClient, User
    from mojo.apps.account.services.oauth_server import tokens

    # These fixtures also work on consumer databases that are never flushed.
    Group.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(username__startswith=PREFIX).delete()
    OAuthClient.objects.filter(client_id=PREFIX + "client").delete()

    opts.reader = User.objects.create(
        username=PREFIX + "reader", email=PREFIX + "reader@example.com",
        permissions={"view_admin": True, "assistant": True})
    opts.denied = User.objects.create(
        username=PREFIX + "denied", email=PREFIX + "denied@example.com",
        permissions={"assistant": True})

    opts.target = User.objects.create(
        username=PREFIX + "target", email=PREFIX + "target@example.com",
        display_name="Target account", is_email_verified=True,
        permissions={"manage_users": True, "view_reports": False})
    opts.target.set_password("not-in-results-9351")
    opts.target.save()
    opts.loner = User.objects.create(
        username=PREFIX + "loner", email=PREFIX + "loner@example.com")

    opts.first = Group.objects.create(name=PREFIX + "first")
    opts.second = Group.objects.create(name=PREFIX + "second")
    opts.memberships = [
        GroupMember.objects.create(
            group=opts.first, user=opts.target, permissions={"manage_group": True}),
        GroupMember.objects.create(
            group=opts.second, user=opts.target, is_active=False,
            permissions={"view_members": True}),
    ]

    client = OAuthClient.objects.create(
        client_id=PREFIX + "client", kind="dcr", client_name="User reader regression",
        redirect_uris=["http://127.0.0.1:8500/cb"])
    opts.reader_grant = tokens.create_grant(
        opts.reader, client, ["mcp"], "https://user-reader.example/api/assistant/mcp", 1700000000)
    opts.denied_grant = tokens.create_grant(
        opts.denied, client, ["mcp"], "https://user-reader.example/api/assistant/mcp", 1700000000)


def _sorted_by_group(rows):
    return sorted(rows, key=lambda row: row["group_id"])


def _mcp_call(opts, name, user_id, denied=False):
    from mojo.apps.assistant.mcp import server
    from mojo.apps.assistant.services import agent

    user = opts.denied if denied else opts.reader
    grant = opts.denied_grant if denied else opts.reader_grant
    request = th.get_mock_request(user=user, path="/api/assistant/mcp", method="POST")
    request.oauth_grant = grant
    events = []

    def reporter(details, **kwargs):
        events.append({"details": details, **kwargs})

    status, payload = server.handle(
        json.dumps({"jsonrpc": "2.0", "id": 9351, "method": "tools/call",
                    "params": {"name": name, "arguments": {"user_id": user_id}}}),
        user, grant, agent._build_request_meta(request), "testit-user-readers",
        hide_infrastructure=True, _reporter=reporter)
    assert status == 200, f"{name} should return an MCP response: {status}, {payload}"
    assert payload.get("id") == 9351, f"{name} must preserve the JSON-RPC request ID"
    assert "result" in payload, f"{name} should dispatch through tools/call: {payload}"
    return payload["result"], events


@th.django_unit_test()
def test_user_detail_returns_profile_permissions_and_groups(opts):
    from mojo.apps.assistant.services.tools.users import _tool_get_user_detail

    target = opts.target
    target.refresh_from_db()
    result = _tool_get_user_detail({"user_id": target.pk}, opts.reader)
    groups = result.pop("groups")
    assert result == {
        "id": target.pk, "username": target.username, "email": target.email,
        "display_name": "Target account", "is_active": True, "is_superuser": False,
        "is_email_verified": True, "is_phone_verified": False,
        "created": str(target.created), "last_activity": None,
        "permissions": {"manage_users": True, "view_reports": False},
    }, f"Detail must return the safe profile and the user's own permissions: {result}"
    assert _sorted_by_group(groups) == [
        {"group_id": opts.first.pk, "group_name": opts.first.name, "role": None},
        {"group_id": opts.second.pk, "group_name": opts.second.name, "role": None},
    ], f"Detail must list every membership with its group name: {groups}"
    assert not SENSITIVE_FIELDS & set(result), "Detail must never include sensitive fields"


@th.django_unit_test()
def test_user_detail_without_groups(opts):
    from mojo.apps.assistant.services.tools.users import _tool_get_user_detail

    result = _tool_get_user_detail({"user_id": opts.loner.pk}, opts.reader)
    assert result["groups"] == [], f"A user with no memberships has no groups: {result}"
    assert result["permissions"] == {}, "A user with no permissions has an empty dict"


@th.django_unit_test()
def test_permission_summary_lists_user_and_group_permissions(opts):
    from mojo.apps.assistant.services.tools.users import _tool_get_permission_summary

    result = _tool_get_permission_summary({"user_id": opts.target.pk}, opts.reader)
    group_permissions = result.pop("group_permissions")
    assert result == {
        "user_id": opts.target.pk, "username": opts.target.username, "is_superuser": False,
        "user_permissions": {"manage_users": True, "view_reports": False},
    }, f"Summary must return the user's own permissions: {result}"
    assert _sorted_by_group(group_permissions) == [
        {"group_id": opts.first.pk, "group_name": opts.first.name,
         "permissions": {"manage_group": True}},
        {"group_id": opts.second.pk, "group_name": opts.second.name,
         "permissions": {"view_members": True}},
    ], f"Summary must list each membership's permissions: {group_permissions}"


@th.django_unit_test()
def test_permission_summary_without_groups(opts):
    from mojo.apps.assistant.services.tools.users import _tool_get_permission_summary

    result = _tool_get_permission_summary({"user_id": opts.loner.pk}, opts.reader)
    assert result["group_permissions"] == [], f"No memberships means no group permissions: {result}"
    assert result["user_permissions"] == {}, "A user with no permissions has an empty dict"


@th.django_unit_test()
def test_mcp_both_readers_execute_for_view_admin(opts):
    for name in ("get_user_detail", "get_permission_summary"):
        result, events = _mcp_call(opts, name, opts.target.pk)
        assert not result.get("isError"), f"Authorized MCP {name} must succeed: {result}"
        body = json.loads(result["content"][0]["text"])
        key = "id" if name == "get_user_detail" else "user_id"
        assert body[key] == opts.target.pk, f"MCP {name} must describe the requested user: {body}"
        assert not events, f"Successful {name} must not report errors: {events}"


@th.django_unit_test()
def test_mcp_both_readers_require_view_admin(opts):
    for name in ("get_user_detail", "get_permission_summary"):
        result, events = _mcp_call(opts, name, opts.target.pk, denied=True)
        assert result.get("isError") is True, f"{name} must refuse callers without view_admin"
        body = json.loads(result["content"][0]["text"])
        assert body == {"error": f"Permission denied. You need 'view_admin' to use {name}."}, (
            f"{name} must return permission denial without user data: {body}")
        assert [event["category"] for event in events] == ["assistant:permission_denied"], (
            f"{name} denial must be reported through the permission gate: {events}")
