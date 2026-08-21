"""
FK-attach NO_FK_VIEW_CHECK_FIELDS opt-out (moved from
tests/test_models/fk_attach_audit.py — maestro item #2558).

The test mutates Setting.RestMeta.NO_FK_VIEW_CHECK_FIELDS, a process-global
attribute on a shared production model that dozens of parallel modules read,
and the contract needs a real persisted Setting row (on_rest_save assigns and
saves the FK), so a table-less local probe model cannot carry it. It runs in
the opt-in serial tier instead.

The companion audit-event test (FK silently skipped + fk_attach_denied fires)
stays in tests/test_models/fk_attach_audit.py — it mutates nothing shared and
is the default-tier representative of this security contract.
"""
from testit import helpers as th


TEST_USER = "fk_attach_optout_user"
TEST_GROUP_NAME = "fk-attach-optout-target-group"


@th.django_unit_setup()
def setup_fk_attach_optout(opts):
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
    user.remove_all_permissions()
    user.is_staff = False
    user.is_superuser = False
    # `manage_settings` lets the user view/save Setting. They MUST NOT have
    # any perm in Group.VIEW_PERMS — otherwise the FK target would be viewable
    # and the opt-out would not be what makes the assignment succeed.
    user.add_permission("manage_settings")
    user.save()

    GroupMember.objects.filter(user=user).delete()

    Group.objects.filter(name=TEST_GROUP_NAME).delete()
    target = Group(name=TEST_GROUP_NAME, kind="default")
    target.save()
    opts.target_group_id = target.id

    Setting.objects.filter(key__startswith="fk-attach-optout-test-").delete()
    Event.objects.filter(uid=user.id, category="fk_attach_denied").delete()
    opts.user_id = user.id


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

    Setting.objects.filter(key="fk-attach-optout-test-1").delete()
    user = User.objects.filter(username=TEST_USER).last()
    target = Group.objects.filter(name=TEST_GROUP_NAME).last()

    Event.objects.filter(uid=user.id, category="fk_attach_denied").delete()
    setting = Setting.objects.create(key="fk-attach-optout-test-1", value="v0", group=None)

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
