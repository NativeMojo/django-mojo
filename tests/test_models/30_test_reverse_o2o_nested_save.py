"""Maestro item #8184 — a nested save on a missing reverse one-to-one record.

POST /api/group/<id> with a dict for `push_config` (PushConfig.group is a
OneToOneField, related_name="push_config") used to read the reverse accessor
with no default. With no PushConfig row, Django raises RelatedObjectDoesNotExist
and the caller got a 500. Now the save is refused with a 400 naming the field,
and nothing is written. An existing row still saves under the related model's
own permission gate, exactly as before.

Fixtures are made by ORM with an r8184_ prefix and each test resets its own.
"""
from testit import helpers as th

TESTIT_TIER = "bug"

PASSWORD = "R8184##nested"
PREFIX = "r8184_"


def _email(name):
    return f"{PREFIX}{name}@models.test"


def _mk_user(name):
    from mojo.apps.account.models import User
    email = _email(name)
    user = User.objects.create_user(username=email, email=email, password=PASSWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    return user


@th.django_unit_setup()
def setup_reverse_o2o(opts):
    from mojo.apps.account.models import User, Group, GroupMember
    from mojo.apps.account.models.push.config import PushConfig

    PushConfig.objects.filter(name__startswith=PREFIX).delete()
    GroupMember.objects.filter(group__name__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(email__startswith=PREFIX).delete()

    opts.r8184_admin = _mk_user("admin")
    opts.r8184_admin.add_permission("manage_groups")
    opts.r8184_member = _mk_user("member")

    opts.r8184_bare = Group.objects.create(name=f"{PREFIX}bare", kind="generic")
    opts.r8184_held = Group.objects.create(name=f"{PREFIX}held", kind="generic")
    for group in (opts.r8184_bare, opts.r8184_held):
        group.add_member(opts.r8184_member)
        GroupMember.objects.get(group=group, user=opts.r8184_member).add_permission("manage_group")


def _reset(opts):
    """The bare group has no config; the held group has one named r8184_cfg."""
    from mojo.apps.account.models import Group
    from mojo.apps.account.models.push.config import PushConfig
    PushConfig.objects.filter(group=opts.r8184_bare).delete()
    PushConfig.objects.filter(name__startswith=PREFIX).exclude(
        group=opts.r8184_held).delete()
    Group.objects.filter(pk=opts.r8184_bare.pk).update(name=f"{PREFIX}bare")
    Group.objects.filter(pk=opts.r8184_held.pk).update(name=f"{PREFIX}held")
    config = PushConfig.objects.filter(group=opts.r8184_held).first()
    if config is None:
        config = PushConfig.objects.create(group=opts.r8184_held, name=f"{PREFIX}cfg")
    else:
        PushConfig.objects.filter(pk=config.pk).update(name=f"{PREFIX}cfg")
    return config


def _login(opts, name):
    opts.client.logout()
    ok = opts.client.login(_email(name), PASSWORD)
    assert ok, f"login failed for {name}: {opts.client.last_response.body}"


def _group_name(pk):
    from mojo.apps.account.models import Group
    return Group.objects.filter(pk=pk).values_list("name", flat=True).first()


def _configs(group):
    from mojo.apps.account.models.push.config import PushConfig
    return list(PushConfig.objects.filter(group=group).values_list("name", flat=True))


def _assert_refused(opts, resp, group, what):
    body = str(opts.client.last_response.body)
    assert resp.status_code == 400, \
        f"{what} must be refused with 400, got {resp.status_code}: {body}"
    assert "push_config" in body, f"the error must name push_config, got {body}"
    assert _group_name(group.pk) == group.name, \
        f"{what} must not save the group, its name is now {_group_name(group.pk)!r}"
    assert _configs(group) == [], f"{what} must not create a push config, got {_configs(group)}"


@th.django_unit_test("#8184: push_config is a writable reverse one-to-one on Group")
def test_fixture_relation_shape(opts):
    from django.db import models as dm
    from mojo.apps.account.models import Group

    field = Group.get_model_field("push_config")
    assert isinstance(field, dm.OneToOneRel), \
        f"the fixture needs push_config to be a reverse one-to-one, got {type(field).__name__}"
    assert "push_config" not in Group.get_no_save_fields(), \
        "the fixture needs push_config to be writable through a Group save"


@th.django_unit_test("#8184 (a): a nested dict for a missing record is refused with 400")
def test_missing_record_refused(opts):
    _reset(opts)
    _login(opts, "admin")
    resp = opts.client.post(f"/api/group/{opts.r8184_bare.pk}", {
        "name": f"{PREFIX}renamed", "push_config": {"name": f"{PREFIX}new"}})
    _assert_refused(opts, resp, opts.r8184_bare, "a nested save on a missing push_config")


@th.django_unit_test("#8184 (b): a nested dict for an existing record still saves")
def test_existing_record_saves(opts):
    config = _reset(opts)
    _login(opts, "admin")
    resp = opts.client.post(f"/api/group/{opts.r8184_held.pk}", {
        "push_config": {"name": f"{PREFIX}updated"}})
    assert resp.status_code == 200, \
        f"an existing push_config must still save, got {resp.status_code}: {opts.client.last_response.body}"
    config.refresh_from_db()
    assert config.name == f"{PREFIX}updated", \
        f"the existing push_config must be updated, its name is {config.name!r}"


@th.django_unit_test("#8184 (b2): a denied update is still skipped; a missing record still refused")
def test_denied_update_skipped_and_missing_refused(opts):
    config = _reset(opts)
    _login(opts, "member")
    resp = opts.client.post(f"/api/group/{opts.r8184_held.pk}", {
        "push_config": {"name": f"{PREFIX}hijack"}})
    assert resp.status_code == 200, \
        f"a member denied by PushConfig keeps today's silent skip, got {resp.status_code}: {opts.client.last_response.body}"
    config.refresh_from_db()
    assert config.name == f"{PREFIX}cfg", \
        f"a member without push-config rights must not change it, its name is {config.name!r}"

    resp = opts.client.post(f"/api/group/{opts.r8184_bare.pk}", {
        "push_config": {"name": f"{PREFIX}new"}})
    _assert_refused(opts, resp, opts.r8184_bare, "the same member on a group with no push_config")


@th.django_unit_test("#8184 (c): a null push_config on a group without one changes nothing")
def test_null_unchanged(opts):
    _reset(opts)
    _login(opts, "admin")
    resp = opts.client.post(f"/api/group/{opts.r8184_bare.pk}", {"push_config": None})
    assert resp.status_code == 200, \
        f"a null push_config must still succeed, got {resp.status_code}: {opts.client.last_response.body}"
    assert _configs(opts.r8184_bare) == [], \
        f"a null push_config must not create one, got {_configs(opts.r8184_bare)}"
    assert _group_name(opts.r8184_bare.pk) == f"{PREFIX}bare", "the group must be unchanged"
