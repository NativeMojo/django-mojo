"""
#8648 — the assistant's model reads apply the nested-User rule too.

query_model serializes outside the REST middleware, so before the fix the
nested User saw no request at all (or a stale one from another thread) and
GroupMember rows carried every member's account fields to any caller holding
`view_members`. The tool now binds its own request around serialization.
"""
from testit import helpers as th

TESTIT_TIER = "core"

PREFIX = "nuvai8648_"
VIEWER = f"{PREFIX}viewer@test.com"
TARGET = f"{PREFIX}target@test.com"

IDENTITY_KEYS = {"id", "uuid", "username", "display_name", "is_active", "avatar"}
NEVER_KEYS = {"permissions", "is_superuser", "requires_mfa",
              "requires_password_change", "has_password", "has_passkey", "metadata"}


@th.django_unit_setup()
def setup_nested_user_visibility_ai(opts):
    from mojo.apps.account.models import User, Group, GroupMember

    User.objects.filter(email__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()

    group = Group.objects.create(name=f"{PREFIX}group", kind="organization")
    opts.group_id = group.pk

    viewer = User.objects.create_user(username=VIEWER, email=VIEWER, password="x" * 16)
    viewer.permissions = {"view_members": True}
    viewer.save()
    opts.viewer = viewer

    target = User.objects.create_user(username=TARGET, email=TARGET, password="x" * 16)
    target.permissions = {"view_logs": True}
    target.metadata = {"note": "private"}
    target.save()
    opts.target_id = target.pk

    GroupMember(user=viewer, group=group).save()
    GroupMember(user=target, group=group).save()


@th.django_unit_test("query_model on GroupMember as a view_members-only user nests identity only")
def test_query_model_group_member_identity(opts):
    from mojo.apps.assistant.services.tools.models import _tool_query_model

    result = _tool_query_model({
        "app_name": "account", "model_name": "GroupMember",
        "filters": {"group": opts.group_id},
    }, opts.viewer)
    assert "error" not in result, f"query_model must succeed: {result.get('error')}"
    users = {row["user"]["id"]: row["user"] for row in result["results"]}
    assert opts.target_id in users, f"the target member must be returned, got {sorted(users)}"
    keys = set(users[opts.target_id].keys())
    assert not keys & NEVER_KEYS, f"the assistant must not return {sorted(keys & NEVER_KEYS)}"
    assert keys == IDENTITY_KEYS, f"expected exactly {sorted(IDENTITY_KEYS)}, got {sorted(keys)}"
    own = set(users[opts.viewer.pk].keys())
    assert NEVER_KEYS <= own, f"the caller's own row stays full, got {sorted(own)}"
