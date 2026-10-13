"""Who may make a group ApiKey assume a member (maestro #8034).

The rule: only the member themself, or a platform-wide administrator, may make
a key assume a member (override_user=True), and only they may read or replace
that key's token. A group manager may still link a member in reference mode,
which records the member and gives the key no authority.

Every user, group and key here is test-owned (the `akd_` prefix); no shared
setting is patched. REST calls go through the real server so the refusals are
proven at the boundary a manager actually reaches.
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true

PASSWORD = "akd##mojo99"
MANAGER = "akd_manager"          # member-level manage_group in the parent group
TARGET = "akd_target"            # ordinary member holding a perm the manager lacks
OTHER = "akd_other"              # a second ordinary member of the parent group
CHILD_TARGET = "akd_child_target"  # direct member of the child group only
SERVICE = "akd_service"          # service member a platform admin provisions for
PLATFORM_ADMIN = "akd_platform_admin"  # global manage_users/manage_groups, no membership
PARENT_GROUP = "akd_parent"
CHILD_GROUP = "akd_child"
SECOND_GROUP = "akd_second"      # the manager manages it; the target is not a member
# A manager who can also read every user (global view_users). Reading a user
# is what lets a posted foreign key attach, so this is the caller who reaches
# the link by its column name; it grants no authority over any member.
VIEWER_MANAGER = "akd_viewer_manager"
USERS = [MANAGER, TARGET, OTHER, CHILD_TARGET, SERVICE, PLATFORM_ADMIN,
         VIEWER_MANAGER]


def _make_user(username, global_perms=None):
    from mojo.apps.account.models import User
    user = User(username=username, email="%s@example.com" % username)
    user.save()
    user.is_active = True
    user.is_email_verified = True
    user.save_password(PASSWORD)
    if global_perms:
        user.add_permission(global_perms)
    user.save()
    return user


@th.django_unit_setup()
def setup_api_key_delegation(opts):
    from mojo.apps.account.models import ApiKey, Group, GroupMember, User, UserAPIKey
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Long-lived database: remove leftovers before creating anything.
    ApiKey.objects.filter(name__startswith="akd_").delete()
    UserAPIKey.objects.filter(user__username__in=USERS).delete()
    Group.objects.filter(name__in=[CHILD_GROUP, SECOND_GROUP, PARENT_GROUP]).delete()
    User.objects.filter(username__in=USERS).delete()

    parent = Group.objects.create(name=PARENT_GROUP, kind="organization")
    child = Group.objects.create(name=CHILD_GROUP, kind="team", parent=parent)
    second = Group.objects.create(name=SECOND_GROUP, kind="organization")

    manager = _make_user(MANAGER)
    target = _make_user(TARGET)
    other = _make_user(OTHER)
    child_target = _make_user(CHILD_TARGET)
    service = _make_user(SERVICE)
    admin = _make_user(PLATFORM_ADMIN, ["manage_users", "manage_groups"])
    viewer_manager = _make_user(VIEWER_MANAGER, ["view_users"])

    GroupMember.objects.create(user=manager, group=parent, permissions={"manage_group": True})
    GroupMember.objects.create(user=manager, group=second, permissions={"manage_group": True})
    GroupMember.objects.create(user=viewer_manager, group=parent,
                               permissions={"manage_group": True})
    # The permission the manager does NOT hold: an override key linked to the
    # target is what would hand it to the manager.
    GroupMember.objects.create(user=target, group=parent, permissions={"manage_tickets": True})
    GroupMember.objects.create(user=other, group=parent, permissions={})
    GroupMember.objects.create(user=child_target, group=child, permissions={"manage_tickets": True})
    GroupMember.objects.create(user=service, group=parent, permissions={"manage_tickets": True})

    opts.parent_id = parent.pk
    opts.child_id = child.pk
    opts.second_id = second.pk
    opts.manager_id = manager.pk
    opts.target_id = target.pk
    opts.other_id = other.pk
    opts.child_target_id = child_target.pk
    opts.service_id = service.pk
    opts.admin_id = admin.pk
    opts.viewer_manager_id = viewer_manager.pk


def _login(opts, username):
    opts.client.logout()
    opts.client.login(username, PASSWORD)
    assert_true(opts.client.is_authenticated, "%s login failed" % username)


def _key(opts, name, user_id=None, override=False, group_id=None):
    """A test-owned key created in process, the trusted internal path."""
    from mojo.apps.account.models import ApiKey, Group, User
    group = Group.objects.get(pk=group_id or opts.parent_id)
    user = User.objects.get(pk=user_id) if user_id else None
    key, _token = ApiKey.create_for_group(
        group=group, name=name, user=user, override_user=override)
    return key


def _log_count(key, kind):
    from mojo.apps.logit.models import Log
    return Log.objects.filter(kind=kind, model_id=key.pk).count()


def _reload(key):
    from mojo.apps.account.models import ApiKey
    return ApiKey.objects.get(pk=key.pk)


# ---------------------------------------------------------------------------
# Refused: a manager may not make a key assume another member
# ---------------------------------------------------------------------------

@th.django_unit_test("ApiKey delegation: a manager cannot enable override for another member")
def test_manager_cannot_enable_override_for_another_member(opts):
    key = _key(opts, "akd_enable_other", user_id=opts.target_id)
    _login(opts, MANAGER)
    resp = opts.client.post("/api/group/apikey/%s" % key.pk,
                            {"group": opts.parent_id, "override_user": True})
    assert_eq(resp.status_code, 403,
              "a group manager must not switch override on for another member: %r" % resp.response)
    assert_eq(_reload(key).override_user, False,
              "the refused request must leave override off")
    opts.client.logout()


@th.django_unit_test("ApiKey delegation: a manager cannot create an override key for another member in one body")
def test_manager_cannot_create_override_key_for_another_member_in_one_body(opts):
    from mojo.apps.account.models import ApiKey
    _login(opts, MANAGER)
    for name, body in (
            ("akd_one_body_user_first",
             {"user": opts.target_id, "override_user": True}),
            ("akd_one_body_override_first",
             {"override_user": True, "user": opts.target_id})):
        payload = {"name": name, "group": opts.parent_id}
        payload.update(body)
        resp = opts.client.post("/api/group/apikey", payload)
        assert_true(resp.status_code in (400, 403),
                    "%s: a create that makes a key assume another member must be refused, "
                    "got %s %r" % (name, resp.status_code, resp.response))
        assert_eq(ApiKey.objects.filter(name=name, override_user=True).count(), 0,
                  "%s: no key assuming the member may exist after the refusal" % name)
    opts.client.logout()


@th.django_unit_test("ApiKey delegation: a manager cannot re-point a key that already assumes a member")
def test_manager_cannot_repoint_an_override_key(opts):
    # The manager's own override key is legitimate; pointing it at someone
    # else afterwards must not be a way around the rule.
    key = _key(opts, "akd_repoint", user_id=opts.manager_id, override=True)
    _login(opts, MANAGER)
    for body in ({"user": opts.target_id},
                 {"override_user": True, "user": opts.target_id}):
        payload = {"group": opts.parent_id}
        payload.update(body)
        resp = opts.client.post("/api/group/apikey/%s" % key.pk, payload)
        assert_eq(resp.status_code, 403,
                  "re-pointing an override key at another member must be refused: %r"
                  % resp.response)
        fresh = _reload(key)
        assert_eq(fresh.user_id, opts.manager_id,
                  "the refused request must leave the key linked to its member")
        assert_eq(fresh.override_user, True,
                  "the refusal must not silently clear override")
    opts.client.logout()


@th.django_unit_test("ApiKey delegation: user_id cannot re-point a key that already assumes a member")
def test_manager_cannot_repoint_an_override_key_by_user_id(opts):
    # `user_id` is the column name of the same link. The key and its token
    # are the manager's own, issued the legitimate way.
    from mojo.apps.account.models import ApiKey
    _login(opts, VIEWER_MANAGER)
    resp = opts.client.post("/api/group/apikey", {
        "name": "akd_repoint_by_id", "group": opts.parent_id,
        "user": opts.viewer_manager_id, "override_user": True})
    assert_eq(resp.status_code, 200,
              "the manager must be able to issue their own key: %r" % resp.response)
    key = ApiKey.objects.get(pk=resp.response.data.id)
    token = resp.response.data.get("token")
    assert_true(token, "the creator must receive the new token")

    for body in ({"user_id": opts.target_id},
                 {"override_user": True, "user_id": opts.target_id},
                 {"user_id": opts.target_id, "user": opts.viewer_manager_id},
                 {"user": opts.viewer_manager_id, "user_id": opts.target_id}):
        payload = {"group": opts.parent_id}
        payload.update(body)
        resp = opts.client.post("/api/group/apikey/%s" % key.pk, payload)
        assert_eq(resp.status_code, 403,
                  "re-pointing an override key by user_id must be refused, body keys %s: %r"
                  % (sorted(body), resp.response))
        fresh = _reload(key)
        assert_eq(fresh.user_id, opts.viewer_manager_id,
                  "the refused request must leave the key linked to its member, body keys %s"
                  % sorted(body))
        assert_eq(fresh.override_user, True,
                  "the refusal must not silently clear override")
        assert_eq(fresh.override_enabled_by_id, opts.viewer_manager_id,
                  "the refused request must leave the issuer record alone")

    identity, error = ApiKey.validate_token(token, th.get_mock_request())
    assert_eq(error, None, "the manager's own key must still authenticate")
    assert_eq(identity.pk, opts.viewer_manager_id,
              "the manager's token must still act as the manager, never the other member")
    opts.client.logout()


@th.django_unit_test("ApiKey delegation: user_id cannot create an override key for another member in one body")
def test_manager_cannot_create_override_key_for_another_member_by_user_id(opts):
    from mojo.apps.account.models import ApiKey
    _login(opts, VIEWER_MANAGER)
    for name, body in (
            ("akd_id_body_plain",
             {"user_id": opts.target_id, "override_user": True}),
            ("akd_id_body_override_first",
             {"override_user": True, "user_id": opts.target_id}),
            # Approved for themself, then re-pointed by the other name.
            ("akd_id_body_self_then_id",
             {"user": opts.viewer_manager_id, "override_user": True,
              "user_id": opts.target_id})):
        payload = {"name": name, "group": opts.parent_id}
        payload.update(body)
        resp = opts.client.post("/api/group/apikey", payload)
        assert_true(resp.status_code in (400, 403),
                    "%s: a create that makes a key assume another member must be refused, "
                    "got %s %r" % (name, resp.status_code, resp.response))
        assert_eq(ApiKey.objects.filter(name=name).count(), 0,
                  "%s: no key may exist after the refusal" % name)
    opts.client.logout()


@th.django_unit_test("ApiKey delegation: user_id cannot link someone who is not a member of the key's group")
def test_user_id_cannot_link_a_non_member(opts):
    key = _key(opts, "akd_id_non_member")
    _login(opts, VIEWER_MANAGER)
    resp = opts.client.post("/api/group/apikey/%s" % key.pk,
                            {"group": opts.parent_id, "user_id": opts.child_target_id})
    assert_eq(resp.status_code, 403,
              "user_id must not link a user who is not a direct member: %r" % resp.response)
    assert_eq(_reload(key).user_id, None,
              "the refused request must leave the key unlinked")
    opts.client.logout()


@th.django_unit_test("ApiKey delegation: an ancestor group's manager cannot enable override for a child member")
def test_ancestor_manager_cannot_enable_override_for_child_member(opts):
    key = _key(opts, "akd_ancestor", user_id=opts.child_target_id, group_id=opts.child_id)
    _login(opts, MANAGER)
    resp = opts.client.post("/api/group/apikey/%s" % key.pk,
                            {"group": opts.child_id, "override_user": True})
    assert_eq(resp.status_code, 403,
              "a manager above the group must not switch override on for its member: %r"
              % resp.response)
    assert_eq(_reload(key).override_user, False,
              "the refused request must leave override off")
    opts.client.logout()


@th.django_unit_test("ApiKey delegation: a manager cannot read the token of a member's override key")
def test_manager_cannot_read_token_of_members_override_key(opts):
    key = _key(opts, "akd_read_token", user_id=opts.target_id, override=True)
    _login(opts, MANAGER)
    resp = opts.client.get("/api/group/apikey/%s" % key.pk,
                           params={"group": opts.parent_id, "graph": "token"})
    assert_eq(resp.status_code, 200, "the key itself stays readable: %r" % resp.response)
    assert_eq(resp.response.data.get("token"), None,
              "a manager must not receive the token of a key that assumes another member")
    assert_eq(_log_count(key, "api_key:token_read_refused"), 1,
              "the refused token read must be audited")
    assert_eq(_log_count(key, "api_key:token_read"), 0,
              "a refused read must not be recorded as a read")
    opts.client.logout()


@th.django_unit_test("ApiKey delegation: a managing API key cannot read the token of a member's override key")
def test_managing_api_key_cannot_read_token_of_members_override_key(opts):
    from mojo.apps.account.models import ApiKey, Group

    group = Group.objects.get(pk=opts.parent_id)
    target_key = _key(opts, "akd_read_token_by_key", user_id=opts.target_id, override=True)
    plain_key = _key(opts, "akd_read_token_plain")
    _manager_key, manager_token = ApiKey.create_for_group(
        group=group, name="akd_managing_key", permissions={"manage_group": True})

    opts.client.logout()
    opts.client.access_token = manager_token
    opts.client.bearer = "apikey"
    opts.client.is_authenticated = True
    try:
        resp = opts.client.get("/api/group/apikey/%s" % target_key.pk, params={"graph": "token"})
        assert_eq(resp.status_code, 200, "the key itself stays readable: %r" % resp.response)
        assert_eq(resp.response.data.get("token"), None,
                  "an API key must not receive the token of a key that assumes a member")
        resp = opts.client.get("/api/group/apikey/%s" % plain_key.pk, params={"graph": "token"})
        assert_eq(resp.response.data.get("token"), plain_key.get_token(),
                  "a key that assumes nobody keeps today's token read-back")
    finally:
        opts.client.logout()


@th.django_unit_test("ApiKey delegation: a manager cannot rotate a member's override key")
def test_manager_cannot_rotate_members_override_key(opts):
    key = _key(opts, "akd_rotate", user_id=opts.target_id, override=True)
    before = key.token_hash
    _login(opts, MANAGER)
    resp = opts.client.post("/api/account/admin/apikey/action",
                            {"api_key": key.pk, "action": "rotate"})
    assert_eq(resp.status_code, 403,
              "a manager must not rotate, and so receive, the token of a key that "
              "assumes another member: %r" % resp.response)
    assert_eq(_reload(key).token_hash, before, "the refused rotate must not change the token")
    opts.client.logout()


@th.django_unit_test("ApiKey delegation: a per-user API key session cannot enable override")
def test_user_api_key_session_cannot_enable_override(opts):
    from mojo.apps.account.models import User, UserAPIKey

    manager = User.objects.get(pk=opts.manager_id)
    package = UserAPIKey.create_for_user(manager, expire_days=1, label="akd delegation")
    key = _key(opts, "akd_user_api_key", user_id=opts.manager_id)

    opts.client.logout()
    opts.client.access_token = package.token
    opts.client.is_authenticated = True
    try:
        resp = opts.client.post("/api/group/apikey/%s" % key.pk,
                                {"group": opts.parent_id, "override_user": True})
        assert_eq(resp.status_code, 403,
                  "a stored per-user API key must not mint a key that assumes its "
                  "owner: %r" % resp.response)
        assert_eq(_reload(key).override_user, False,
                  "the refused request must leave override off")
    finally:
        opts.client.logout()
        UserAPIKey.objects.filter(user=manager).delete()


@th.django_unit_test("ApiKey delegation: a linked key cannot be moved to a group its member is not in")
def test_linked_key_cannot_move_to_group_without_its_member(opts):
    # A link is only valid for a direct member of the key's own group. Moving
    # the key must not leave it linked to someone the new group never had.
    for name, override in (("akd_move_reference", False), ("akd_move_override", True)):
        key = _key(opts, name, user_id=opts.target_id, override=override)
        _login(opts, MANAGER)
        resp = opts.client.post("/api/group/apikey/%s" % key.pk, {"group": opts.second_id})
        assert_eq(resp.status_code, 403,
                  "%s: moving a linked key to a group its member is not in must be "
                  "refused: %r" % (name, resp.response))
        assert_eq(_reload(key).group_id, opts.parent_id,
                  "%s: the refused move must leave the key in its group" % name)
    opts.client.logout()


@th.django_unit_test("ApiKey delegation: an unlinked key can still be moved between managed groups")
def test_unlinked_key_can_still_move_groups(opts):
    key = _key(opts, "akd_move_unlinked")
    _login(opts, MANAGER)
    resp = opts.client.post("/api/group/apikey/%s" % key.pk, {"group": opts.second_id})
    assert_eq(resp.status_code, 200,
              "a key with no linked member keeps today's behaviour: %r" % resp.response)
    assert_eq(_reload(key).group_id, opts.second_id, "the unlinked key must move")
    opts.client.logout()


# ---------------------------------------------------------------------------
# The issuer record
# ---------------------------------------------------------------------------

@th.django_unit_test("ApiKey delegation: linking a member records who linked it and when")
def test_link_stamps_issuer_and_time(opts):
    key = _key(opts, "akd_link_stamp")
    _login(opts, MANAGER)
    resp = opts.client.post("/api/group/apikey/%s" % key.pk,
                            {"group": opts.parent_id, "user": opts.target_id})
    assert_eq(resp.status_code, 200, "a reference-mode link must succeed: %r" % resp.response)
    fresh = _reload(key)
    assert_eq(fresh.user_linked_by_id, opts.manager_id,
              "the key must record who linked the member")
    assert_true(fresh.user_linked_at is not None, "the key must record when the member was linked")
    assert_eq(resp.response.data.get("user_linked_by", {}).get("id"), opts.manager_id,
              "the response must show who linked the member")

    resp = opts.client.post("/api/group/apikey/%s" % key.pk,
                            {"group": opts.parent_id, "user": None})
    assert_eq(resp.status_code, 200, "clearing the link must succeed: %r" % resp.response)
    fresh = _reload(key)
    assert_eq((fresh.user_id, fresh.user_linked_by_id, fresh.user_linked_at),
              (None, None, None),
              "clearing the link must clear the record that described it")
    opts.client.logout()


@th.django_unit_test("ApiKey delegation: enabling override records who enabled it and when")
def test_override_enable_stamps_issuer_and_time(opts):
    key = _key(opts, "akd_override_stamp", user_id=opts.manager_id)
    _login(opts, MANAGER)
    resp = opts.client.post("/api/group/apikey/%s" % key.pk,
                            {"group": opts.parent_id, "override_user": True})
    assert_eq(resp.status_code, 200,
              "a member must be able to make a key assume themself: %r" % resp.response)
    fresh = _reload(key)
    assert_eq(fresh.override_enabled_by_id, opts.manager_id,
              "the key must record who switched override on")
    assert_true(fresh.override_enabled_at is not None,
                "the key must record when override was switched on")
    assert_eq(_log_count(key, "api_key:override_enabled"), 1,
              "switching override on must leave one audit row naming the issuer")

    resp = opts.client.post("/api/group/apikey/%s" % key.pk,
                            {"group": opts.parent_id, "override_user": False})
    assert_eq(resp.status_code, 200, "switching override off must succeed: %r" % resp.response)
    fresh = _reload(key)
    assert_eq((fresh.override_user, fresh.override_enabled_by_id, fresh.override_enabled_at),
              (False, None, None),
              "switching override off must clear the record that described it")
    opts.client.logout()


@th.django_unit_test("ApiKey delegation: an allowed re-point records the new issuer")
def test_allowed_repoint_stamps_issuer(opts):
    key = _key(opts, "akd_repoint_allowed", user_id=opts.service_id, override=True)
    _login(opts, PLATFORM_ADMIN)
    resp = opts.client.post("/api/group/apikey/%s" % key.pk,
                            {"group": opts.parent_id, "user": opts.other_id})
    assert_eq(resp.status_code, 200,
              "a platform administrator may re-point a key that assumes a member: %r"
              % resp.response)
    key = _reload(key)
    assert_eq((key.user_id, key.override_user, key.user_linked_by_id, key.override_enabled_by_id),
              (opts.other_id, True, opts.admin_id, opts.admin_id),
              "re-pointing must record the administrator as issuer for the new member")
    opts.client.logout()


@th.django_unit_test("ApiKey delegation: the issuer record cannot be written over REST")
def test_issuer_fields_are_not_writable_over_rest(opts):
    key = _key(opts, "akd_not_writable", user_id=opts.manager_id)
    _login(opts, MANAGER)
    resp = opts.client.post("/api/group/apikey/%s" % key.pk, {
        "group": opts.parent_id,
        "name": "akd_not_writable_renamed",
        "user_linked_by": opts.other_id,
        "user_linked_by_id": opts.other_id,
        "override_enabled_by": opts.other_id,
        "override_enabled_by_id": opts.other_id,
        "user_linked_at": "2020-01-01T00:00:00Z",
        "override_enabled_at": "2020-01-01T00:00:00Z",
    })
    assert_eq(resp.status_code, 200, "the ordinary field must still save: %r" % resp.response)
    fresh = _reload(key)
    assert_eq(fresh.name, "akd_not_writable_renamed", "the ordinary field must be saved")
    assert_eq((fresh.user_linked_by_id, fresh.override_enabled_by_id,
               fresh.user_linked_at, fresh.override_enabled_at),
              (None, None, None, None),
              "a posted issuer record must be ignored")
    opts.client.logout()


@th.django_unit_test("ApiKey delegation: create_for_group records the issuer it is given")
def test_create_for_group_stamps_issued_by(opts):
    from mojo.apps.account.models import ApiKey, Group, User

    group = Group.objects.get(pk=opts.parent_id)
    service = User.objects.get(pk=opts.service_id)
    admin = User.objects.get(pk=opts.admin_id)
    key, _token = ApiKey.create_for_group(
        group=group, name="akd_issued_by", user=service, override_user=True,
        issued_by=admin)
    fresh = _reload(key)
    assert_eq((fresh.user_linked_by_id, fresh.override_enabled_by_id),
              (admin.pk, admin.pk), "the given issuer must be recorded for the link and the override")
    assert_true(fresh.user_linked_at is not None and fresh.override_enabled_at is not None,
                "both times must be recorded when an issuer is given")

    key, _token = ApiKey.create_for_group(
        group=group, name="akd_issued_by_reference", user=service, issued_by=admin)
    fresh = _reload(key)
    assert_eq((fresh.user_linked_by_id, fresh.override_enabled_by_id), (admin.pk, None),
              "a reference-mode key must record the link only")


# ---------------------------------------------------------------------------
# Still working: the documented uses
# ---------------------------------------------------------------------------

@th.django_unit_test("ApiKey delegation: a manager links a member in reference mode")
def test_manager_links_member_in_reference_mode(opts):
    from mojo.apps.account.models import ApiKey
    _login(opts, MANAGER)
    resp = opts.client.post("/api/group/apikey", {
        "name": "akd_reference", "group": opts.parent_id, "user": opts.target_id})
    assert_eq(resp.status_code, 200,
              "a manager must still be able to link a member in reference mode: %r" % resp.response)
    key = ApiKey.objects.get(pk=resp.response.data.id)
    assert_eq((key.user_id, key.override_user), (opts.target_id, False),
              "the key must reference the member and assume nobody")
    assert_true(resp.response.data.get("token"), "the creator must still receive the new token")
    opts.client.logout()


@th.django_unit_test("ApiKey delegation: a member self-issues an override key")
def test_member_self_issues_override_key(opts):
    from mojo.apps.account.models import ApiKey
    _login(opts, MANAGER)
    resp = opts.client.post("/api/group/apikey", {
        "name": "akd_self_issue", "group": opts.parent_id,
        "user": opts.manager_id, "override_user": True})
    assert_eq(resp.status_code, 200,
              "a member must be able to make a key assume themself: %r" % resp.response)
    key = ApiKey.objects.get(pk=resp.response.data.id)
    assert_eq((key.user_id, key.override_user), (opts.manager_id, True),
              "the key must assume the member who issued it")
    token = resp.response.data.get("token")
    assert_true(token, "the creator must receive the new token")
    assert_eq(key.override_enabled_by_id, opts.manager_id,
              "a self-issued key must record its issuer")
    assert_eq(_log_count(key, "api_key:override_enabled"), 1,
              "a key created with override on must leave one audit row")

    identity, error = ApiKey.validate_token(token, th.get_mock_request())
    assert_eq(error, None, "the self-issued key must authenticate")
    assert_eq(identity.pk, opts.manager_id, "the self-issued key must act as its member")
    opts.client.logout()


@th.django_unit_test("ApiKey delegation: a manager links a member by user_id in reference mode")
def test_manager_links_member_by_user_id_in_reference_mode(opts):
    key = _key(opts, "akd_id_reference")
    _login(opts, VIEWER_MANAGER)
    resp = opts.client.post("/api/group/apikey/%s" % key.pk,
                            {"group": opts.parent_id, "user_id": opts.target_id})
    assert_eq(resp.status_code, 200,
              "a manager must still be able to link a member by user_id: %r" % resp.response)
    fresh = _reload(key)
    assert_eq((fresh.user_id, fresh.override_user), (opts.target_id, False),
              "the key must be linked for reference only")
    assert_eq(fresh.user_linked_by_id, opts.viewer_manager_id,
              "a link made by user_id must record who made it")
    assert_true(fresh.user_linked_at is not None,
                "a link made by user_id must record when it was made")
    opts.client.logout()


@th.django_unit_test("ApiKey delegation: a platform administrator provisions override for a service member")
def test_platform_admin_provisions_override_for_service_member(opts):
    from mojo.apps.account.models import ApiKey
    _login(opts, PLATFORM_ADMIN)
    resp = opts.client.post("/api/group/apikey", {
        "name": "akd_service_key", "group": opts.parent_id,
        "user": opts.service_id, "override_user": True})
    assert_eq(resp.status_code, 200,
              "a platform administrator must be able to provision a key that assumes "
              "a service member: %r" % resp.response)
    key = ApiKey.objects.get(pk=resp.response.data.id)
    assert_eq((key.user_id, key.override_user), (opts.service_id, True),
              "the key must assume the service member")

    resp = opts.client.get("/api/group/apikey/%s" % key.pk,
                           params={"group": opts.parent_id, "graph": "token"})
    assert_eq(resp.response.data.get("token"), key.get_token(),
              "a platform administrator must still read the token of such a key")
    resp = opts.client.post("/api/account/admin/apikey/action",
                            {"api_key": key.pk, "action": "rotate"})
    assert_eq(resp.status_code, 200,
              "a platform administrator must still rotate such a key: %r" % resp.response)
    opts.client.logout()


@th.django_unit_test("ApiKey delegation: create_for_group still creates an override key in process")
def test_create_for_group_internal_override_still_works(opts):
    from mojo.apps.account.models import ApiKey, Group, User

    group = Group.objects.get(pk=opts.parent_id)
    service = User.objects.get(pk=opts.service_id)
    key, token = ApiKey.create_for_group(
        group=group, name="akd_internal", user=service, override_user=True)
    assert_eq((key.user_id, key.override_user), (service.pk, True),
              "the trusted internal path must still create a key that assumes a member")
    identity, error = ApiKey.validate_token(token, th.get_mock_request())
    assert_eq(error, None, "the internally created key must authenticate")
    assert_eq(identity.pk, service.pk, "the internally created key must act as its member")


@th.django_unit_test("ApiKey delegation: a manager can deactivate and revoke a member's override key")
def test_manager_can_deactivate_and_revoke_members_override_key(opts):
    from mojo.apps.account.models import ApiKey
    key = _key(opts, "akd_stop", user_id=opts.target_id, override=True)
    _login(opts, MANAGER)
    resp = opts.client.post("/api/account/admin/apikey/action",
                            {"api_key": key.pk, "action": "deactivate"})
    assert_eq(resp.status_code, 200,
              "a manager must be able to stop a key that assumes a member: %r" % resp.response)
    assert_eq(_reload(key).is_active, False, "deactivate must switch the key off")
    resp = opts.client.post("/api/account/admin/apikey/action",
                            {"api_key": key.pk, "action": "revoke"})
    assert_eq(resp.status_code, 200,
              "a manager must be able to revoke a key that assumes a member: %r" % resp.response)
    assert_eq(ApiKey.objects.filter(pk=key.pk).count(), 0, "revoke must delete the key")
    opts.client.logout()


@th.django_unit_test("ApiKey delegation: the linked member reads their own override key's token")
def test_linked_member_reads_own_override_key_token(opts):
    key = _key(opts, "akd_own_token", user_id=opts.manager_id, override=True)
    _login(opts, MANAGER)
    resp = opts.client.get("/api/group/apikey/%s" % key.pk,
                           params={"group": opts.parent_id, "graph": "token"})
    assert_eq(resp.status_code, 200, "the member's own key must be readable: %r" % resp.response)
    assert_eq(resp.response.data.get("token"), key.get_token(),
              "the linked member must receive their own key's token")
    opts.client.logout()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

@th.django_unit_setup()
def cleanup_api_key_delegation(opts):
    from mojo.apps.account.models import ApiKey, Group, User, UserAPIKey

    ApiKey.objects.filter(name__startswith="akd_").delete()
    UserAPIKey.objects.filter(user__username__in=USERS).delete()
    Group.objects.filter(name__in=[CHILD_GROUP, SECOND_GROUP, PARENT_GROUP]).delete()
    User.objects.filter(username__in=USERS).delete()
