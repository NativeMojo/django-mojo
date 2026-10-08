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
relation field. `Group.parent` is a nullable FK and a convenient
target. (`Setting.group` was the target until #7149 made a setting's
scope immutable; that refusal is covered in
test_account.test_setting_scope.)

Clearing the parent moves the child out of its tree, which #6350 allows
only to a person holding global manage_groups in the active request. The
test user holds it, so the save runs with the fake request bound as the
active one, as a real REST save has it.
"""
from testit import helpers as th

TESTIT_TIER = "bug"  # #2792 tier curation


TEST_USER = "fk_blank_user"
TEST_PWORD = "testit##mojo"
TEST_GROUP_NAME = "fk-blank-target-group"
CHILD_PREFIX = "fk-blank-test-"


@th.django_unit_setup()
def setup_fk_blank(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.group import Group
    from mojo.apps.account.models.member import GroupMember

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
    user.add_permission("manage_groups")
    user.save()
    GroupMember.objects.filter(user=user).delete()
    opts.user_id = user.id

    # Setup must clean up before creating — tests run on a long-lived DB.
    # Children first: Group.parent cascades, but be explicit.
    Group.objects.filter(name__startswith=CHILD_PREFIX).delete()
    Group.objects.filter(name=TEST_GROUP_NAME).delete()
    target = Group(name=TEST_GROUP_NAME, kind="default")
    target.save()
    opts.target_group_id = target.id


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
    req.path = "/api/group/x"
    req.META = {}
    req.api_key = None
    return req


def _rest_save(instance, user, data):
    from mojo.models import rest as mojo_rest

    request = _fake_request(user)
    token = mojo_rest.ACTIVE_REQUEST.set(request)
    try:
        instance.on_rest_save(request, data)
    finally:
        mojo_rest.ACTIVE_REQUEST.reset(token)


@th.django_unit_test()
def test_blank_string_fk_clears_to_none(opts):
    """An empty-string FK on update clears the relation to None and does
    not raise; non-FK fields in the same save still update."""
    from mojo.apps.account.models import User
    from mojo.apps.account.models.group import Group

    Group.objects.filter(name__startswith=f"{CHILD_PREFIX}1").delete()
    child = Group(
        name=f"{CHILD_PREFIX}1", kind="default",
        parent_id=opts.target_group_id,
    )
    child.save()
    assert child.parent_id == opts.target_group_id, (
        f"precondition: FK should start set; got parent_id={child.parent_id!r}"
    )

    user = User.objects.filter(pk=opts.user_id).last()
    _rest_save(child, user, {"parent": "", "name": f"{CHILD_PREFIX}1-v2"})

    child.refresh_from_db()
    assert child.parent_id is None, (
        f"blank-string FK must coerce to None; got parent_id={child.parent_id!r}"
    )
    assert child.name == f"{CHILD_PREFIX}1-v2", (
        f"non-FK fields must still update alongside the cleared FK; "
        f"name={child.name!r}"
    )

    Group.objects.filter(pk=child.pk).delete()


@th.django_unit_test()
def test_whitespace_string_fk_clears_to_none(opts):
    """A whitespace-only FK string is also treated as 'not provided'."""
    from mojo.apps.account.models import User
    from mojo.apps.account.models.group import Group

    Group.objects.filter(name__startswith=f"{CHILD_PREFIX}2").delete()
    child = Group(
        name=f"{CHILD_PREFIX}2", kind="default",
        parent_id=opts.target_group_id,
    )
    child.save()

    user = User.objects.filter(pk=opts.user_id).last()
    _rest_save(child, user, {"parent": "   "})

    child.refresh_from_db()
    assert child.parent_id is None, (
        f"whitespace-only FK must coerce to None; got parent_id={child.parent_id!r}"
    )

    Group.objects.filter(pk=child.pk).delete()
