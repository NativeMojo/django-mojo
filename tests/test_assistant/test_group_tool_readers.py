"""Real ORM and MCP regressions for the group readers' lazy model imports."""
import json
from datetime import timedelta

from testit import helpers as th

TESTIT_TIER = "bug"
PREFIX = "testit-group-readers-8956-"


@th.django_unit_setup()
def setup_group_readers(opts):
    from mojo.apps.account.models import Group, GroupMember, OAuthClient, User
    from mojo.apps.account.services.oauth_server import tokens
    from mojo.helpers import dates

    # These fixtures also work on consumer databases that are never flushed.
    Group.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(username__startswith=PREFIX).delete()
    OAuthClient.objects.filter(client_id=PREFIX + "client").delete()

    opts.reader = User.objects.create(
        username=PREFIX + "reader", email=PREFIX + "reader@example.com",
        permissions={"view_admin": True, "assistant": True, "view_groups": True})
    opts.denied = User.objects.create(
        username=PREFIX + "denied", email=PREFIX + "denied@example.com",
        permissions={"view_admin": True, "assistant": True})
    opts.safe_metadata = {
        "timezone": "America/Los_Angeles", "short_name": "Readers",
        "description": "Group reader fixtures", "website": "https://example.com",
        "industry": "testing",
    }
    opts.group = Group.objects.create(
        name=PREFIX + "populated", kind="org",
        metadata={**opts.safe_metadata, "api_key": "private-fixture",
                  "auth_config": {"mfa_required": True}, "unknown": "hidden"})
    opts.empty = Group.objects.create(name=PREFIX + "empty", metadata={})
    child = Group.objects.create(name=PREFIX + "child", parent=opts.group)
    Group.objects.create(name=PREFIX + "inactive-child", parent=opts.group, is_active=False)
    Group.objects.create(name=PREFIX + "grandchild", parent=child)
    unrelated = Group.objects.create(name=PREFIX + "unrelated")

    active = User.objects.create(
        username=PREFIX + "active", email=PREFIX + "active@example.com",
        display_name="Active account", is_active=True,
        permissions={"manage_users": True}, metadata={"mfa_required": True})
    inactive = User.objects.create(
        username=PREFIX + "inactive", email=PREFIX + "inactive@example.com",
        display_name="Inactive account", is_active=False,
        permissions={"manage_security": True}, metadata={"auth": "private-fixture"})
    older = GroupMember.objects.create(
        group=opts.group, user=active, is_active=False,
        permissions={"view_reports": True}, metadata={"role": "auditor"})
    newer = GroupMember.objects.create(
        group=opts.group, user=inactive, is_active=True,
        permissions={"manage_group": True}, metadata={"role": "manager"})
    now = dates.utcnow()
    GroupMember.objects.filter(pk=older.pk).update(created=now - timedelta(days=2))
    GroupMember.objects.filter(pk=newer.pk).update(created=now - timedelta(days=1))
    older.refresh_from_db()
    newer.refresh_from_db()
    opts.members = [newer, older]
    GroupMember.objects.create(group=child, user=active)
    GroupMember.objects.create(group=unrelated, user=inactive)

    opts.large = Group.objects.create(name=PREFIX + "large")
    users = User.objects.bulk_create([
        User(username=f"{PREFIX}limit-{index}", email=f"{PREFIX}limit-{index}@example.com")
        for index in range(52)
    ])
    members = GroupMember.objects.bulk_create([
        GroupMember(group=opts.large, user=user) for user in users
    ])
    # Explicit, distinct timestamps make ordering independent of PK/insertion order.
    for index, member in enumerate(members):
        GroupMember.objects.filter(pk=member.pk).update(created=now - timedelta(minutes=index))
    opts.large_member_ids = [member.pk for member in members]

    client = OAuthClient.objects.create(
        client_id=PREFIX + "client", kind="dcr", client_name="Group reader regression",
        redirect_uris=["http://127.0.0.1:8500/cb"])
    opts.reader_grant = tokens.create_grant(
        opts.reader, client, ["mcp"], "https://group-reader.example/api/assistant/mcp", 1700000000)
    opts.denied_grant = tokens.create_grant(
        opts.denied, client, ["mcp"], "https://group-reader.example/api/assistant/mcp", 1700000000)


def _expected_members(opts):
    return [{
        "id": member.pk, "user_id": member.user_id,
        "username": member.user.username, "display_name": member.user.display_name,
        "is_active": member.user.is_active, "permissions": member.permissions,
        "created": str(member.created),
    } for member in opts.members]


def _mcp_call(opts, name, denied=False):
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
        json.dumps({"jsonrpc": "2.0", "id": 8956, "method": "tools/call",
                    "params": {"name": name, "arguments": {"group_id": opts.group.pk}}}),
        user, grant, agent._build_request_meta(request), "testit-group-readers",
        hide_infrastructure=True, _reporter=reporter)
    assert status == 200, f"{name} should return an MCP response: {status}, {payload}"
    assert payload.get("id") == 8956, f"{name} must preserve the JSON-RPC request ID"
    assert "result" in payload, f"{name} should dispatch through tools/call: {payload}"
    return payload["result"], events


@th.django_unit_test()
def test_detail_counts_all_direct_rows_and_allowlists_metadata(opts):
    from mojo.apps.assistant.services.tools.groups import _tool_get_group_detail

    result = _tool_get_group_detail({"group_id": opts.group.pk}, opts.reader)
    assert result == {
        "id": opts.group.pk, "name": opts.group.name, "kind": "org",
        "is_active": True, "parent_id": None, "member_count": 2, "children_count": 2,
        "metadata": opts.safe_metadata, "created": str(opts.group.created),
        "modified": str(opts.group.modified),
    }, f"Detail must count inactive direct rows, exclude grandchildren, and allowlist metadata: {result}"


@th.django_unit_test()
def test_empty_group_detail(opts):
    from mojo.apps.assistant.services.tools.groups import _tool_get_group_detail

    result = _tool_get_group_detail({"group_id": opts.empty.pk}, opts.reader)
    assert result["member_count"] == 0, "An existing empty group has no memberships"
    assert result["children_count"] == 0, "An existing empty group has no children"
    assert result["metadata"] == {}, "Empty metadata remains empty"


@th.django_unit_test()
def test_members_preserve_order_fields_and_membership_permissions(opts):
    from mojo.apps.assistant.services.tools.groups import _tool_get_group_members

    result = _tool_get_group_members({"group_id": opts.group.pk}, opts.reader)
    assert result == _expected_members(opts), (
        "Members must be newest first, include inactive memberships, use User.is_active, "
        f"and expose membership permissions without User account/sign-in fields: {result}")


@th.django_unit_test()
def test_empty_group_members(opts):
    from mojo.apps.assistant.services.tools.groups import _tool_get_group_members

    result = _tool_get_group_members({"group_id": opts.empty.pk}, opts.reader)
    assert result == [], f"An existing empty group returns an empty member list: {result}"


@th.django_unit_test()
def test_members_requested_limit_default_and_cap(opts):
    from mojo.apps.assistant.services.tools.groups import _tool_get_group_members

    for requested, expected in ((None, 50), (3, 3), (100, 50)):
        params = {"group_id": opts.large.pk}
        if requested is not None:
            params["limit"] = requested
        result = _tool_get_group_members(params, opts.reader)
        assert [row["id"] for row in result] == opts.large_member_ids[:expected], (
            f"Limit {requested} must return the newest {expected} memberships, capped at 50")


@th.django_unit_test()
def test_mcp_detail_executes_real_reader(opts):
    result, events = _mcp_call(opts, "get_group_detail")
    assert not result.get("isError"), f"Authorized MCP group detail must succeed: {result}"
    detail = json.loads(result["content"][0]["text"])
    assert detail["id"] == opts.group.pk, "MCP detail must identify the requested group"
    assert (detail["member_count"], detail["children_count"]) == (2, 2), "MCP must return real ORM counts"
    assert detail["metadata"] == opts.safe_metadata, "MCP preserves the detail metadata allowlist"
    assert not events, f"Successful group reads must not report errors: {events}"


@th.django_unit_test()
def test_mcp_members_executes_real_reader(opts):
    result, events = _mcp_call(opts, "get_group_members")
    assert not result.get("isError"), f"Authorized MCP group members must succeed: {result}"
    assert json.loads(result["content"][0]["text"]) == _expected_members(opts), (
        "MCP must preserve real member rows and their narrow User field selection")
    assert not events, f"Successful member reads must not report errors: {events}"


@th.django_unit_test()
def test_mcp_both_readers_require_view_groups(opts):
    for name in ("get_group_detail", "get_group_members"):
        result, events = _mcp_call(opts, name, denied=True)
        assert result.get("isError") is True, f"{name} must refuse callers without view_groups"
        body = json.loads(result["content"][0]["text"])
        assert body == {"error": f"Permission denied. You need 'view_groups' to use {name}."}, (
            f"{name} must return permission denial without group data: {body}")
        assert [event["category"] for event in events] == ["assistant:permission_denied"], (
            f"{name} denial must be reported through the permission gate: {events}")
