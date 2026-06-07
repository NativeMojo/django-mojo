"""
Blank-string FK coercion (regression).

A REST save that assigns a relation field a blank string ("" or
whitespace) must treat it as "not provided" and set the relation to
None — NOT crash. Before the fix, `on_rest_save_related_field` ran
`int(field_value)` before its falsy check, so `int("")` raised
`ValueError: invalid literal for int() with base 10: ''`. A frontend
form that submits an unset optional FK as "" (the common case) would
500 the save.

Exercised in-process via `on_rest_save` with a fake request — the
relevant branch is `on_rest_save_related_field`, reachable for any
relation field. `Setting.group` is a nullable FK and a convenient
target.
"""
from testit import helpers as th


TEST_USER = "fk_blank_user"
TEST_PWORD = "testit##mojo"
TEST_GROUP_NAME = "fk-blank-target-group"


@th.django_unit_setup()
def setup_fk_blank(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.group import Group
    from mojo.apps.account.models.member import GroupMember
    from mojo.apps.account.models.setting import Setting

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
    user.add_permission("manage_settings")
    user.save()
    GroupMember.objects.filter(user=user).delete()
    opts.user_id = user.id

    Group.objects.filter(name=TEST_GROUP_NAME).delete()
    target = Group(name=TEST_GROUP_NAME, kind="default")
    target.save()
    opts.target_group_id = target.id

    # Setup must clean up before creating — tests run on a long-lived DB.
    Setting.objects.filter(key__startswith="fk-blank-test-").delete()


def _fake_request(user):
    import objict

    req = objict.objict()
    req.user = user
    req.DATA = objict.objict()
    req.QUERY_PARAMS = objict.objict()
    req.method = "PUT"
    req.group = None
    req.bearer = None
    req.ip = "127.0.0.1"
    req.path = "/api/settings/x"
    req.META = {}
    req.api_key = None
    return req


@th.django_unit_test()
def test_blank_string_fk_clears_to_none(opts):
    """An empty-string FK on update clears the relation to None and does
    not raise; non-FK fields in the same save still update."""
    from mojo.apps.account.models import User
    from mojo.apps.account.models.setting import Setting

    Setting.objects.filter(key="fk-blank-test-1").delete()
    setting = Setting.objects.create(
        key="fk-blank-test-1", value="v1", group_id=opts.target_group_id,
    )
    assert setting.group_id == opts.target_group_id, (
        f"precondition: FK should start set; got group_id={setting.group_id!r}"
    )

    user = User.objects.filter(pk=opts.user_id).last()
    setting.on_rest_save(_fake_request(user), {"group": "", "value": "v2"})

    setting.refresh_from_db()
    assert setting.group_id is None, (
        f"blank-string FK must coerce to None; got group_id={setting.group_id!r}"
    )
    assert setting.value == "v2", (
        f"non-FK fields must still update alongside the cleared FK; "
        f"value={setting.value!r}"
    )

    Setting.objects.filter(pk=setting.pk).delete()


@th.django_unit_test()
def test_whitespace_string_fk_clears_to_none(opts):
    """A whitespace-only FK string is also treated as 'not provided'."""
    from mojo.apps.account.models import User
    from mojo.apps.account.models.setting import Setting

    Setting.objects.filter(key="fk-blank-test-2").delete()
    setting = Setting.objects.create(
        key="fk-blank-test-2", value="v1", group_id=opts.target_group_id,
    )

    user = User.objects.filter(pk=opts.user_id).last()
    setting.on_rest_save(_fake_request(user), {"group": "   "})

    setting.refresh_from_db()
    assert setting.group_id is None, (
        f"whitespace-only FK must coerce to None; got group_id={setting.group_id!r}"
    )

    Setting.objects.filter(pk=setting.pk).delete()
