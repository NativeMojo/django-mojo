"""
#8648: the assistant's model reads apply the nested-User rule too.

query_model and export_data serialize on worker threads, outside the REST
middleware, so before the fix the nested User saw no request at all (or a
stale one from another thread) and GroupMember rows carried every member's
account privileges and sign-in settings to any caller holding `view_members`.
The tools now bind their own request around serialization, and a filter into
those fields is refused, as REST drops it.
"""
from testit import helpers as th

TESTIT_TIER = "core"

PREFIX = "nuvai8648_"
VIEWER = f"{PREFIX}viewer@test.com"
TARGET = f"{PREFIX}target@test.com"
FM_NAME = f"{PREFIX}fm"

PRIVATE_KEYS = {"permissions", "is_superuser", "requires_mfa",
                "requires_password_change", "has_password", "has_passkey", "metadata"}


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
@th.requires_app("mojo.apps.fileman")
def setup_nested_user_visibility_ai(opts):
    from mojo.apps.account.models import User, Group, GroupMember
    from mojo.apps.fileman.models import FileManager, File

    User.objects.filter(email__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    FileManager.objects.filter(name=FM_NAME).delete()

    group = Group.objects.create(name=f"{PREFIX}group", kind="organization")
    opts.group_id = group.pk

    viewer = User.objects.create_user(username=VIEWER, email=VIEWER, password="x" * 16)
    viewer.permissions = {"view_members": True}
    viewer.save()
    opts.viewer = viewer
    File.objects.filter(user=viewer).delete()
    FileManager.objects.create(
        name=FM_NAME, backend_type="file", backend_url=f"filesystem:///tmp/{PREFIX}files",
        is_default=True, is_active=True, user=viewer)

    target = User.objects.create_user(username=TARGET, email=TARGET, password="x" * 16)
    target.permissions = {"view_logs": True}
    target.metadata = {"note": "private"}
    target.save()
    User.objects.filter(pk=target.pk).update(requires_mfa=True)
    opts.target_id = target.pk

    GroupMember(user=viewer, group=group).save()
    GroupMember(user=target, group=group).save()


def _query(opts, filters):
    from mojo.apps.assistant.services.tools.models import _tool_query_model
    return _tool_query_model({
        "app_name": "account", "model_name": "GroupMember", "filters": filters,
    }, opts.viewer)


@th.django_unit_test("query_model on GroupMember as a view_members-only user withholds the private fields")
def test_query_model_group_member_stripped(opts):
    result = _query(opts, {"group": opts.group_id})
    assert "error" not in result, f"query_model must succeed: {result.get('error')}"
    users = {row["user"]["id"]: row["user"] for row in result["results"]}
    assert opts.target_id in users, f"the target member must be returned, got {sorted(users)}"
    keys = set(users[opts.target_id].keys())
    assert not keys & PRIVATE_KEYS, f"the assistant must not return {sorted(keys & PRIVATE_KEYS)}"
    assert users[opts.target_id].get("email") == TARGET, \
        f"the member's email stays, got keys {sorted(keys)}"
    own = set(users[opts.viewer.pk].keys())
    assert PRIVATE_KEYS & own, f"the caller's own row stays full, got {sorted(own)}"


@th.django_unit_test("query_model refuses a filter into the private user fields, keeps the email filter")
def test_query_model_private_filter_refused(opts):
    for key in ("user__is_superuser", "user__requires_mfa", "user__totp__is_enabled"):
        result = _query(opts, {"group": opts.group_id, key: True})
        assert "not available" in str(result.get("error", "")), \
            f"filters={{{key!r}: True}} must be refused for a view_members viewer, got {result!r}"
    result = _query(opts, {"group": opts.group_id, "user__email": TARGET})
    assert "error" not in result, f"an email filter still works: {result.get('error')}"
    assert [row["user"]["id"] for row in result["results"]] == [opts.target_id], \
        "the email filter must select the target alone"


@th.django_unit_test("export_data on GroupMember as a view_members-only user withholds the private fields")
def test_export_data_group_member_stripped(opts):
    from mojo.apps.assistant.services.tools.models import _tool_export_data
    from mojo.apps.fileman.models import File

    result = _tool_export_data({
        "app_name": "account", "model_name": "GroupMember",
        "filters": {"group": opts.group_id, "user": opts.target_id},
        "fields": ["id", "user"],
    }, opts.viewer)
    assert "error" not in result, f"export_data must succeed: {result.get('error')}"
    f = File.objects.filter(user=opts.viewer).order_by("-pk").first()
    assert f is not None, "the export must create a File row"
    with f.file_manager.backend.open(f.storage_file_path) as fh:
        text = fh.read().decode("utf-8")
    assert TARGET in text, f"the member's email stays in the export: {text!r}"
    for key in PRIVATE_KEYS:
        assert key not in text, f"the export must not carry {key}: {text!r}"
