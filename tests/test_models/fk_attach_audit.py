"""
FK-attach silent-skip audit (`fk_attach_denied` event).

When a save assigns an FK by primary key and the requester lacks
VIEW_PERMS on the related instance, the framework silently skips the
assignment to prevent attaching to records the user can't otherwise
see — but emits an `fk_attach_denied` incident so the audit trail
survives now that `rest_check_permission` is event-free.

Setup: a user with `manage_settings` (and `groups` so they can save
Settings) but no `view_groups` perm and no membership of the target
group. POST /api/account/settings with `group=<their-no-access-id>`
must succeed (200) with the field unset, AND record exactly one
`fk_attach_denied` event.

See planning/issues/spurious-permission-denied-events-on-list.md
"""
from testit import helpers as th


TEST_USER = "fk_attach_user"
TEST_PWORD = "testit##mojo"
TEST_GROUP_NAME = "fk-attach-target-group"


@th.django_unit_setup()
def setup_fk_attach(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.group import Group
    from mojo.apps.account.models.member import GroupMember
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.incident.models.event import Event

    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(
            username=TEST_USER,
            display_name=TEST_USER,
            email=f"{TEST_USER}@example.com",
        )
        user.save()
    user.is_email_verified = True
    user.save_password(TEST_PWORD)
    user.remove_all_permissions()
    user.is_staff = False
    user.is_superuser = False
    # `manage_settings` lets the user view/save Setting (Setting's
    # VIEW_PERMS / SAVE_PERMS include "manage_settings"). They MUST NOT
    # have any perm in Group.VIEW_PERMS (view_groups/manage_groups/
    # manage_group/groups) — otherwise the FK target would be viewable
    # and the silent-skip path wouldn't fire.
    user.add_permission("manage_settings")
    user.save()

    GroupMember.objects.filter(user=user).delete()

    Group.objects.filter(name=TEST_GROUP_NAME).delete()
    target = Group(name=TEST_GROUP_NAME, kind="default")
    target.save()
    opts.target_group_id = target.id

    Setting.objects.filter(key__startswith="fk-attach-test-").delete()
    Event.objects.filter(uid=user.id, category="fk_attach_denied").delete()
    opts.user_id = user.id


@th.django_unit_test()
def test_fk_attach_to_unviewable_group_emits_audit_event(opts):
    """
    Create a global Setting (group=None) directly via the ORM, then PUT
    via REST with `group=<target>`. The save path on UPDATE goes through
    on_rest_save_related_field for the `group` field — unlike CREATE,
    which auto-stamps `request.group` before the FK-attach branch runs.

    Without VIEW_PERMS on the target Group, the FK is silently skipped
    AND `fk_attach_denied` fires.
    """
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.incident.models.event import Event

    Event.objects.filter(uid=opts.user_id, category="fk_attach_denied").delete()
    Setting.objects.filter(key="fk-attach-test-1").delete()

    seed = Setting.objects.create(
        key="fk-attach-test-1", value="v1", group=None,
    )

    assert opts.client.login(TEST_USER, TEST_PWORD), "login failed"

    resp = opts.client.put(
        f"/api/settings/{seed.id}",
        json={"group": opts.target_group_id, "value": "v2"},
    )
    assert resp.status_code == 200, (
        f"Setting update must succeed (FK silently skipped); got "
        f"{resp.status_code}: {resp.response!r}"
    )

    setting = Setting.objects.filter(pk=seed.id).first()
    assert setting is not None, "Setting row should still exist after update"
    assert setting.group_id is None, (
        f"FK attach should have been silently skipped; group_id is "
        f"{setting.group_id!r}, target was {opts.target_group_id!r}"
    )
    assert setting.value == "v2", (
        f"Non-FK fields should still update; value is {setting.value!r}"
    )

    events = list(
        Event.objects.filter(
            uid=opts.user_id, category="fk_attach_denied",
        ).values("details", "metadata")
    )
    assert len(events) == 1, (
        f"Expected exactly 1 fk_attach_denied event, got "
        f"{len(events)}: {events!r}"
    )
    meta = events[0]["metadata"]
    assert meta.get("field_name") == "group", (
        f"Expected field_name=group, got {meta.get('field_name')!r}"
    )
    assert meta.get("related_model") == "Group", (
        f"Expected related_model=Group, got {meta.get('related_model')!r}"
    )
    assert meta.get("related_id") == opts.target_group_id, (
        f"Expected related_id={opts.target_group_id}, got {meta.get('related_id')!r}"
    )
    assert meta.get("model_name") == "Setting", (
        f"Expected model_name=Setting, got {meta.get('model_name')!r}"
    )

    Setting.objects.filter(pk=seed.id).delete()


@th.django_unit_test()
def test_no_fk_view_check_fields_opt_out(opts):
    """A field listed in NO_FK_VIEW_CHECK_FIELDS must skip the audit
    altogether — assignment proceeds, no event fires.

    Setting doesn't normally exempt `group`, so we monkey-patch
    NO_FK_VIEW_CHECK_FIELDS for this one test. The change is in-process
    only (Setting.RestMeta — which the testit server process won't see),
    so we exercise the relevant path directly via on_rest_save instead
    of going through opts.client.
    """
    from mojo.apps.account.models import User
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.account.models.group import Group
    from mojo.apps.incident.models.event import Event
    import objict

    Setting.objects.filter(key="fk-attach-test-optout").delete()
    user = User.objects.filter(username=TEST_USER).last()
    target = Group.objects.filter(name=TEST_GROUP_NAME).last()

    Event.objects.filter(uid=user.id, category="fk_attach_denied").delete()
    setting = Setting.objects.create(key="fk-attach-test-optout", value="v0", group=None)

    fake_request = objict.objict()
    fake_request.user = user
    fake_request.DATA = objict.objict()
    fake_request.QUERY_PARAMS = objict.objict()
    fake_request.method = "PUT"
    fake_request.group = None
    fake_request.bearer = None
    fake_request.ip = "127.0.0.1"
    fake_request.path = "/api/settings/x"
    fake_request.META = {}
    fake_request.api_key = None

    original = getattr(Setting.RestMeta, "NO_FK_VIEW_CHECK_FIELDS", None)
    setattr(Setting.RestMeta, "NO_FK_VIEW_CHECK_FIELDS", ["group"])
    try:
        setting.on_rest_save(fake_request, {"group": target.id})
        setting.refresh_from_db()
        assert setting.group_id == target.id, (
            f"With NO_FK_VIEW_CHECK_FIELDS=['group'] the FK must be assigned; "
            f"got group_id={setting.group_id!r}"
        )
        events = Event.objects.filter(
            uid=user.id, category="fk_attach_denied",
        ).count()
        assert events == 0, (
            f"NO_FK_VIEW_CHECK_FIELDS opt-out must not emit fk_attach_denied; "
            f"got {events} event(s)"
        )
    finally:
        if original is None:
            if hasattr(Setting.RestMeta, "NO_FK_VIEW_CHECK_FIELDS"):
                delattr(Setting.RestMeta, "NO_FK_VIEW_CHECK_FIELDS")
        else:
            setattr(Setting.RestMeta, "NO_FK_VIEW_CHECK_FIELDS", original)
        Setting.objects.filter(pk=setting.pk).delete()
