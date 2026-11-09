"""Maestro item #9250 — transport keys must not trigger a stale full-row save.

The request parser merges the query string into request.DATA, so `?graph=`,
`?group=`, `group_uuid` and `_mtab` reach MojoModel.on_rest_save. Each one used
to count as field data: an action-only request then did a full save of the
loaded (unlocked, possibly stale) row before running the action, undoing a
concurrent change. These tests load a stale copy, commit newer state beside
it, then run the real save path, which reproduces the race deterministically.

Controls prove what must not change: a mixed field + action body still saves
then runs the action, a different FK `group` still re-parents, creates still
create, and a plain field save with `?graph=` still saves.

Fixtures are made by ORM with a t9250_ prefix and each test resets its own.
"""
import objict
from testit import helpers as th

PASSWORD = "T9250##transport"
PREFIX = "t9250_"


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
def setup_transport_action_save(opts):
    from mojo.apps.account.models import User, Group, GroupMember
    from mojo.apps.account.models.notification import Notification

    Notification.objects.filter(title__startswith=PREFIX).delete()
    GroupMember.objects.filter(group__name__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(email__startswith=PREFIX).delete()

    opts.t9250_admin = _mk_user("admin")
    opts.t9250_admin.add_permission(["manage_groups", "view_groups"])
    opts.t9250_owner = _mk_user("owner")
    opts.t9250_owner.add_permission("view_groups")

    opts.t9250_group = Group.objects.create(name=f"{PREFIX}group", kind="generic")
    opts.t9250_other = Group.objects.create(name=f"{PREFIX}other", kind="generic")
    opts.t9250_http = Group.objects.create(name=f"{PREFIX}http", kind="generic")


def _request(user, data, path="/api/group"):
    """Synthetic request just rich enough for on_rest_save."""
    req = objict.objict()
    req.user = user
    req.DATA = objict.objict(data)
    req.QUERY_PARAMS = objict.objict()
    req.method = "POST"
    req.group = None
    req.bearer = None
    req.ip = "127.0.0.1"
    req.path = path
    req.META = {}
    req.api_key = None
    return req


def _save(instance, user, data):
    req = _request(user, data)
    return instance.on_rest_save(req, req.DATA)


def _fresh_group(opts, attr="t9250_group"):
    """Reset the fixture group and return a copy loaded BEFORE the next write."""
    from mojo.apps.account.models import Group
    pk = getattr(opts, attr).pk
    Group.objects.filter(pk=pk).update(
        is_active=True, name=f"{PREFIX}{attr[6:]}", metadata={})
    return Group.objects.get(pk=pk)


@th.django_unit_test("#9250: a stale action request cannot re-enable a disabled group")
def test_stale_action_keeps_concurrent_disable(opts):
    from mojo.apps.account.models import Group

    stale = _fresh_group(opts)
    Group.objects.filter(pk=stale.pk).update(is_active=False)

    resp = _save(stale, opts.t9250_admin, {"graph": "basic", "realtime_message": {}})

    assert Group.objects.get(pk=stale.pk).is_active is False, \
        "a transport `graph` key made the action re-save the stale row and re-enable the group"
    assert resp == {"status": True}, f"the action's own response must be returned, got {resp!r}"


@th.django_unit_test("#9250: a stale action request cannot rewind the group token epoch")
def test_stale_action_keeps_token_epoch(opts):
    from mojo.apps.account.models import Group

    stale = _fresh_group(opts)
    concurrent = Group.objects.get(pk=stale.pk)
    epoch = concurrent.bump_group_token_epoch()

    _save(stale, opts.t9250_admin, {
        "graph": "basic", "group_uuid": "x", "_mtab": "t", "realtime_message": {}})

    current = Group.objects.get(pk=stale.pk).get_group_token_epoch()
    assert current == epoch, \
        f"transport keys rewound the revoked-token epoch from {epoch} to {current}"


@th.django_unit_test("#9250: a stale action request keeps a concurrent metadata key")
def test_stale_action_keeps_concurrent_metadata(opts):
    from mojo.apps.account.models import Group

    stale = _fresh_group(opts)
    concurrent = Group.objects.get(pk=stale.pk)
    concurrent.metadata = {"t9250_concurrent": "kept"}
    concurrent.save(update_fields=["metadata", "modified"])

    _save(stale, opts.t9250_admin, {"graph": "basic", "realtime_message": {}})

    metadata = Group.objects.get(pk=stale.pk).metadata or {}
    assert metadata.get("t9250_concurrent") == "kept", \
        f"the stale pre-save dropped a concurrently written metadata key: {metadata!r}"


def _fresh_notification(opts):
    from mojo.apps.account.models.notification import Notification
    Notification.objects.filter(title__startswith=PREFIX).delete()
    note = Notification.objects.create(
        user=opts.t9250_owner, group=opts.t9250_group,
        title=f"{PREFIX}original", data={"v": 1})
    return Notification.objects.get(pk=note.pk)


@th.django_unit_test("#9250: an equal `group` FK in the query does not force a stale save")
def test_stale_action_with_equal_group_fk(opts):
    from mojo.apps.account.models.notification import Notification

    stale = _fresh_notification(opts)
    Notification.objects.filter(pk=stale.pk).update(
        title=f"{PREFIX}concurrent", data={"v": 2})

    _save(stale, opts.t9250_owner, {
        "group": opts.t9250_group.pk, "graph": "default", "mark_read": True})

    row = Notification.objects.get(pk=stale.pk)
    assert row.title == f"{PREFIX}concurrent", \
        f"an unchanged `group` FK re-saved the stale title: {row.title!r}"
    assert row.data == {"v": 2}, f"an unchanged `group` FK re-saved stale data: {row.data!r}"
    assert row.is_unread is False, "the mark_read action did not run"


@th.django_unit_test("#9250: HTTP action with transport query keys writes nothing else")
def test_http_action_with_transport_query(opts):
    from mojo.apps.account.models import Group

    group = _fresh_group(opts, "t9250_http")
    before = Group.objects.filter(pk=group.pk).values_list("modified", flat=True).first()

    opts.client.logout()
    assert opts.client.login(_email("admin"), PASSWORD), "admin login failed"
    # `?group=` names another group: the dispatcher stamps activity on the
    # group it binds (Group.touch), which would move `modified` on its own.
    resp = opts.client.post(
        f"/api/group/{group.pk}?graph=basic&group={opts.t9250_other.pk}&_mtab=t",
        {"realtime_message": {}})
    body = str(opts.client.last_response.body)

    assert resp.status_code == 200, f"the action failed: {resp.status_code} {body}"
    assert resp.json.get("status") is True, f"the action's envelope changed: {body}"
    after = Group.objects.filter(pk=group.pk).values_list("modified", flat=True).first()
    assert after == before, \
        f"transport query keys made the action save the whole row (modified {before} -> {after})"


# ---------------------------------------------------------------------------
# Controls: behaviour that must stay exactly as it was.
# ---------------------------------------------------------------------------

@th.django_unit_test("#9250 control: a mixed field + action body saves, then acts")
def test_mixed_field_and_action_still_saves(opts):
    from mojo.apps.account.models import Group

    stale = _fresh_group(opts)
    resp = _save(stale, opts.t9250_admin, {
        "graph": "basic", "name": f"{PREFIX}renamed", "realtime_message": {}})

    assert Group.objects.get(pk=stale.pk).name == f"{PREFIX}renamed", \
        "a mixed body must still save its fields"
    assert resp == {"status": True}, f"the mixed body must still run the action, got {resp!r}"


@th.django_unit_test("#9250 control: a different `group` FK still re-parents")
def test_different_group_fk_still_reparents(opts):
    from mojo.apps.account.models.notification import Notification

    stale = _fresh_notification(opts)
    _save(stale, opts.t9250_owner, {
        "group": opts.t9250_other.pk, "graph": "default", "mark_read": True})

    row = Notification.objects.get(pk=stale.pk)
    assert row.group_id == opts.t9250_other.pk, \
        f"a different `group` must still move the record, got group {row.group_id}"
    assert row.is_unread is False, "the action did not run after the re-parent"


@th.django_unit_test("#9250 control: a create with transport keys still creates")
def test_create_with_transport_keys(opts):
    from mojo.apps.account.models import Group

    Group.objects.filter(name=f"{PREFIX}created").delete()
    group = Group()
    _save(group, opts.t9250_admin, {"graph": "basic", "_mtab": "t", "name": f"{PREFIX}created"})

    assert group.pk is not None, "a create carrying transport keys did not create the row"
    assert Group.objects.filter(pk=group.pk, name=f"{PREFIX}created").exists(), \
        "the created row was not stored"


@th.django_unit_test("#9250 control: a plain field save with `graph` still saves")
def test_field_save_with_graph(opts):
    from mojo.apps.account.models import Group

    stale = _fresh_group(opts)
    _save(stale, opts.t9250_admin, {"graph": "basic", "name": f"{PREFIX}plain"})

    assert Group.objects.get(pk=stale.pk).name == f"{PREFIX}plain", \
        "a field save carrying `graph` must still save"
