"""Maestro item #6364 — who may decide where a group sits inside its tree.

#6350 made every move that leaves a tree, joins one or detaches a group a
platform-level act. It left the moves inside one tree: a manager of one
sub-group could move it under any other branch they could merely view, out
from under the settings, sign-in rules, administrators and API keys of the
branch it was in. A new group could also be placed under any parent.

Over REST, a move inside one tree now needs save rights, held as a member, on
both the parent the group leaves and the parent it joins. A new group under a
parent needs them on that parent. A `manage_group` on the caller's own user
row is not such a right. Global `manage_groups` or `groups` (or a superuser)
passes everywhere.

The tree: T with children A and B, S under A, and a separate root X.

Fixtures are made by ORM and every test restores them first, so the tests do
not depend on each other's order.
"""
import uuid as _uuid

from testit import helpers as th

TESTIT_TIER = "bug"

PASSWORD = "Gpg##guard99"
PREFIX = "gpg_"


def _email(name):
    return f"{PREFIX}{name}@account.test"


def _mk_user(name, **attrs):
    from mojo.apps.account.models import User
    email = _email(name)
    user = User.objects.create_user(username=email, email=email, password=PASSWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    for key, value in attrs.items():
        setattr(user, key, value)
    user.save()
    return user


def _member(group, user, *perms):
    from mojo.apps.account.models import GroupMember
    group.add_member(user)
    member = GroupMember.objects.get(group=group, user=user)
    for perm in perms:
        member.add_permission(perm)
    return member


def _restore(opts):
    """Put every fixture group back under its parent, straight in the table,
    and remove any group a test created."""
    from mojo.apps.account.models import Group
    for pk, parent_id in opts.shape.items():
        Group.objects.filter(pk=pk).update(parent_id=parent_id)
    Group.objects.filter(name__startswith=f"{PREFIX}new_").delete()


def _login(opts, name):
    ok = opts.client.login(_email(name), PASSWORD)
    assert ok, f"login failed for {name}: {opts.client.last_response.body}"


def _use_key(opts, token):
    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = token
    opts.client.is_authenticated = True


def _parent_id(pk):
    from mojo.apps.account.models import Group
    return Group.objects.filter(pk=pk).values_list("parent_id", flat=True).first()


def _post(opts, pk, payload):
    return opts.client.post(f"/api/group/{pk}", payload)


def _refused(opts, pk, payload, what):
    """The save is refused and the group stays under the parent it had."""
    before = _parent_id(pk)
    resp = _post(opts, pk, payload)
    assert resp.status_code == 403, \
        f"{what} must be refused with 403, got {resp.status_code}: {opts.client.last_response.body}"
    assert _parent_id(pk) == before, \
        f"SECURITY: {what} moved the group from under {before} to under {_parent_id(pk)}"


def _stays(opts, pk, payload, what):
    """The group stays where it was. A key cannot view a parent outside its
    own tree, and the relation save drops such an id without an error, so the
    answer is a 403 or a save that moved nothing."""
    before = _parent_id(pk)
    resp = _post(opts, pk, payload)
    assert resp.status_code in (200, 403), \
        f"{what} must be refused or change nothing, got {resp.status_code}: {opts.client.last_response.body}"
    assert _parent_id(pk) == before, \
        f"SECURITY: {what} moved the group from under {before} to under {_parent_id(pk)}"


def _moved(opts, pk, payload, parent, what):
    resp = _post(opts, pk, payload)
    assert resp.status_code == 200, \
        f"{what} must be allowed, got {resp.status_code}: {opts.client.last_response.body}"
    assert _parent_id(pk) == parent, \
        f"{what} did not persist: the parent is {_parent_id(pk)}, expected {parent}"


@th.django_unit_setup()
def setup_group_parent_guard(opts):
    from mojo.apps.account.models import ApiKey, Group, User

    ApiKey.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(email__startswith=PREFIX).delete()
    for _ in range(4):  # children first: parent is a cascading foreign key
        Group.objects.filter(name__startswith=PREFIX, groups__isnull=True).delete()

    def group(name, parent=None):
        return Group.objects.create(
            name=f"{PREFIX}{name}", kind="organization", is_active=True, parent=parent)

    t = group("t")
    a = group("a", t)
    b = group("b", t)
    s = group("s", a)
    x = group("x")
    opts.ids = {"t": t.pk, "a": a.pk, "b": b.pk, "s": s.pk, "x": x.pk}
    opts.shape = {g.pk: g.parent_id for g in (t, a, b, s, x)}

    # Member-level managers. None of them holds a global permission.
    s_mgr = _mk_user("s_mgr")           # manages S only; can view B
    _member(s, s_mgr, "manage_group")
    _member(b, s_mgr)
    a_mgr = _mk_user("a_mgr")           # manages the branch S is in; can view B
    _member(a, a_mgr, "manage_group")
    _member(b, a_mgr)
    b_mgr = _mk_user("b_mgr")           # manages the other branch and S itself
    _member(b, b_mgr, "manage_group")
    _member(s, b_mgr, "manage_group")
    ab_mgr = _mk_user("ab_mgr")         # manages both branches
    _member(a, ab_mgr, "manage_group")
    _member(b, ab_mgr, "manage_group")
    _member(t, _mk_user("t_mgr"), "manage_group")
    shadowed = _mk_user("shadowed")     # manages the tenant, plain member of A
    _member(t, shadowed, "manage_group")
    _member(a, shadowed)

    # manage_group on the user row: saves every group, manages no parent.
    row_mgr = _mk_user("row_mgr")
    row_mgr.add_permission("manage_group")
    row_member = _mk_user("row_member")  # the same, and a manager of A by member row
    row_member.add_permission("manage_group")
    _member(a, row_member, "manage_group")

    _mk_user("global_mg").add_permission("manage_groups")
    _mk_user("global_groups").add_permission("groups")
    _mk_user("super", is_superuser=True)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

@th.django_unit_test("#6364: a manager of one sub-group cannot move it to another branch of its tenant")
def test_sub_group_manager_cannot_move_inside_the_tree(opts):
    _restore(opts)
    s, b = opts.ids["s"], opts.ids["b"]
    _login(opts, "s_mgr")
    try:
        resp = _post(opts, s, {"name": f"{PREFIX}s"})
        assert resp.status_code == 200, \
            f"the sub-group manager must still save their own group, got {resp.status_code}: " \
            f"{opts.client.last_response.body}"
        _refused(opts, s, {"parent": b}, "a sub-group manager moving their group under a branch they only view")
    finally:
        opts.client.logout()


@th.django_unit_test("#6364: managing one of the two branches is not enough, whichever it is")
def test_manager_of_one_branch_cannot_move_into_another(opts):
    _restore(opts)
    s, b = opts.ids["s"], opts.ids["b"]
    try:
        _login(opts, "a_mgr")
        _refused(opts, s, {"parent": b}, "a manager of the branch a group leaves, with no rights where it lands,")
        opts.client.logout()
        _login(opts, "b_mgr")
        _refused(opts, s, {"parent": b}, "a manager of the branch a group joins, with no rights where it was,")
    finally:
        opts.client.logout()


@th.django_unit_test("#6364: a tenant manager with a plain member row on the branch is refused")
def test_tenant_manager_with_a_plain_row_on_a_branch_is_refused(opts):
    # The nearest member row decides, and on A that row carries no grant.
    _restore(opts)
    s, b = opts.ids["s"], opts.ids["b"]
    _login(opts, "shadowed")
    try:
        _refused(opts, s, {"parent": b}, "a tenant manager whose own row on the old branch holds no grant")
    finally:
        opts.client.logout()


@th.django_unit_test("#6364: manage_group on the user row is not rights on a parent")
def test_user_row_manage_group_is_not_enough(opts):
    _restore(opts)
    s, b = opts.ids["s"], opts.ids["b"]
    _login(opts, "row_mgr")
    try:
        resp = _post(opts, s, {"name": f"{PREFIX}s"})
        assert resp.status_code == 200, \
            f"a user-row manage_group holder must still save a group, got {resp.status_code}: " \
            f"{opts.client.last_response.body}"
        _refused(opts, s, {"parent": b}, "a user-row manage_group holder moving a group between branches")
    finally:
        opts.client.logout()


@th.django_unit_test("#6364: a new group needs rights on the parent it is created under")
def test_user_row_manage_group_cannot_create_under_any_parent(opts):
    from mojo.apps.account.models import Group
    _restore(opts)
    a, b = opts.ids["a"], opts.ids["b"]

    def create(name, parent=None):
        payload = {"name": f"{PREFIX}new_{name}", "kind": "organization"}
        if parent is not None:
            payload["parent"] = parent
        resp = opts.client.post("/api/group", payload)
        return resp, Group.objects.filter(name=payload["name"]).first()

    try:
        _login(opts, "row_mgr")
        resp, made = create("loose")
        assert resp.status_code == 200 and made is not None and made.parent_id is None, \
            f"this test needs a user-row manage_group holder to reach a create, got {resp.status_code}: " \
            f"{opts.client.last_response.body}"
        resp, made = create("under_b", b)
        assert resp.status_code == 403, \
            f"creating a group under a parent the creator has no rights on must be refused, " \
            f"got {resp.status_code}: {opts.client.last_response.body}"
        assert made is None, f"SECURITY: a group was created under parent {made.parent_id} without rights on it"
        opts.client.logout()

        _login(opts, "row_member")
        resp, made = create("under_a", a)
        assert resp.status_code == 200 and made is not None and made.parent_id == a, \
            f"creating a group under a parent the creator manages as a member must work, " \
            f"got {resp.status_code}: {opts.client.last_response.body}"
        resp, made = create("under_b2", b)
        assert resp.status_code == 403 and made is None, \
            f"SECURITY: rights on one parent let the creator place a group under another, " \
            f"got {resp.status_code}"
        opts.client.logout()

        _login(opts, "global_mg")
        resp, made = create("by_operator", b)
        assert resp.status_code == 200 and made is not None and made.parent_id == b, \
            f"a global holder must be able to create a group under any parent, got {resp.status_code}: " \
            f"{opts.client.last_response.body}"
    finally:
        opts.client.logout()
        _restore(opts)


@th.django_unit_test("#6364: every spelling of the parent is judged the same")
def test_every_spelling(opts):
    from mojo.apps.account.models import User
    _restore(opts)
    s, a, b = opts.ids["s"], opts.ids["a"], opts.ids["b"]
    _login(opts, "s_mgr")
    try:
        for what, payload in (
                ("null", {"parent": None}),
                ("an empty string", {"parent": ""}),
                ("spaces", {"parent": "  "}),
                ("0", {"parent": 0}),
                ("parent_id null", {"parent_id": None}),
                ("an id", {"parent": b}),
                ("an id as a string", {"parent": str(b)}),
                ("parent_id", {"parent_id": b})):
            _refused(opts, s, payload, f"a sub-group manager posting the parent as {what}")
    finally:
        opts.client.logout()

    # A dict saves the parent group itself. A manager of A alone, whose own
    # row carries A as their org, reaches A's parent through that row.
    a_mgr = User.objects.get(email=_email("a_mgr"))
    User.objects.filter(pk=a_mgr.pk).update(org_id=a)
    _login(opts, "a_mgr")
    try:
        opts.client.post(f"/api/group/{s}", {"parent": {"parent": b}})
        assert _parent_id(a) == opts.ids["t"], \
            f"SECURITY: a nested parent save moved the branch itself under {_parent_id(a)}"
        opts.client.post(f"/api/user/{a_mgr.pk}", {"org": {"parent": b}})
        assert _parent_id(a) == opts.ids["t"], \
            f"SECURITY: a save through the user row moved the branch under {_parent_id(a)}"
    finally:
        opts.client.logout()
        User.objects.filter(pk=a_mgr.pk).update(org_id=None)
        _restore(opts)


@th.django_unit_test("#6364: a group API key moves groups only inside its own tree")
def test_group_api_key(opts):
    from mojo.apps.account.models import ApiKey, Group
    _restore(opts)
    s, a, b, x = (opts.ids[name] for name in ("s", "a", "b", "x"))
    tag = _uuid.uuid4().hex[:6]
    perms = {"manage_group": True, "manage_groups": True, "groups": True}

    def key(name, group_id):
        _, token = ApiKey.create_for_group(
            group=Group.objects.get(pk=group_id), name=f"{PREFIX}key_{tag}_{name}",
            permissions=dict(perms))
        return token

    try:
        _use_key(opts, key("s", s))
        resp = _post(opts, s, {"name": f"{PREFIX}s"})
        assert resp.status_code == 200, \
            f"a key on the sub-group must still save it, got {resp.status_code}: {opts.client.last_response.body}"
        _stays(opts, s, {"parent": b}, "a key on the sub-group moving its own group")
        _refused(opts, s, {"parent": None}, "a key on the sub-group detaching its own group")

        _use_key(opts, key("a", a))
        _stays(opts, s, {"parent": b}, "a key on one branch moving a group into another")
        _stays(opts, s, {"parent": x}, "a key on one branch moving a group to another tree")

        _use_key(opts, key("t", opts.ids["t"]))
        _stays(opts, s, {"parent": x}, "a key on the tenant moving a group to another tree")
        _refused(opts, s, {"parent": None}, "a key on the tenant detaching a group")
        _moved(opts, s, {"parent": b}, b, "a key on the tenant moving a group between its own branches")
    finally:
        opts.client.logout()
        ApiKey.objects.filter(name__startswith=PREFIX).delete()
        _restore(opts)


@th.django_unit_test("#6364: a parent that left the tenant during the save is judged again under the lock")
def test_stale_new_parent_is_judged_again_under_the_lock(opts):
    from objict import objict
    from mojo import errors as merrors
    from mojo.apps.account.models import Group, User
    from mojo.models import rest as mojo_rest
    _restore(opts)
    s, b, x = opts.ids["s"], opts.ids["b"], opts.ids["x"]

    def overlapping_move(actor, meanwhile):
        """The REST save of S as `actor`, with `meanwhile()` run after its
        permission check and just before its write."""
        stale = Group.objects.get(pk=s)
        request = objict(
            user=User.objects.get(email=_email(actor)), DATA=objict({"parent": b}),
            QUERY_PARAMS=objict(), method="POST", group=stale, bearer=None,
            ip="127.0.0.1", path=f"/api/group/{s}", META={}, api_key=None,
            group_token=None)
        write = stale.atomic_save

        def write_after_the_other_save():
            meanwhile()
            return write()

        stale.atomic_save = write_after_the_other_save
        token = mojo_rest.ACTIVE_REQUEST.set(request)
        try:
            stale.on_rest_save(request, request.DATA)
        finally:
            mojo_rest.ACTIVE_REQUEST.reset(token)

    try:
        # control: with nothing in between, the manager of both branches moves it
        overlapping_move("ab_mgr", lambda: None)
        assert _parent_id(s) == b, f"this test needs the plain move to work, parent is {_parent_id(s)}"

        # the new parent is moved to another tree while the save is in flight
        _restore(opts)
        refused = False
        try:
            overlapping_move("ab_mgr", lambda: Group.objects.filter(pk=b).update(parent_id=x))
        except merrors.PermissionDeniedException:
            refused = True
        assert refused and _parent_id(s) == opts.ids["a"], \
            f"SECURITY: a move judged before the new parent left the tenant was written after it: " \
            f"refused={refused}, parent is {_parent_id(s)}"
    finally:
        _restore(opts)


# ---------------------------------------------------------------------------
# What must keep working
# ---------------------------------------------------------------------------

@th.django_unit_test("#6364: a manager of both branches can move a group between them")
def test_manager_of_both_branches_can_move(opts):
    _restore(opts)
    s, a, b = opts.ids["s"], opts.ids["a"], opts.ids["b"]
    _login(opts, "ab_mgr")
    try:
        _moved(opts, s, {"parent": b}, b, "a manager of both branches moving a group")
        _moved(opts, s, {"parent_id": a}, a, "the same manager moving it back with parent_id")
    finally:
        opts.client.logout()
        _restore(opts)


@th.django_unit_test("#6364: a tenant manager can move a group inside their tenant")
def test_tenant_manager_can_move_inside_their_tenant(opts):
    _restore(opts)
    s, b = opts.ids["s"], opts.ids["b"]
    _login(opts, "t_mgr")
    try:
        _moved(opts, s, {"parent": b}, b, "a manager of the tenant's top group moving a sub-group")
        _moved(opts, s, {"parent": opts.ids["t"]}, opts.ids["t"],
               "the same manager moving it directly under the top group")
    finally:
        opts.client.logout()
        _restore(opts)


@th.django_unit_test("#6364: posting the parent a group already has is not a move")
def test_unchanged_parent_is_not_a_move(opts):
    _restore(opts)
    s, a = opts.ids["s"], opts.ids["a"]
    try:
        for actor in ("s_mgr", "row_mgr"):
            _login(opts, actor)
            for what, payload in (("parent", {"parent": a}), ("parent as a string", {"parent": str(a)}),
                                  ("parent_id", {"parent_id": a}),
                                  ("the whole form", {"name": f"{PREFIX}s", "parent": a})):
                _moved(opts, s, payload, a, f"{actor} posting the unchanged {what}")
            opts.client.logout()
    finally:
        opts.client.logout()


@th.django_unit_test("#6364: a holder of the global permission can detach and move any group")
def test_platform_manager_can_detach_and_move_anywhere(opts):
    s, a, b, x = (opts.ids[name] for name in ("s", "a", "b", "x"))
    try:
        for actor in ("global_mg", "global_groups", "super"):
            _restore(opts)
            _login(opts, actor)
            _moved(opts, s, {"parent": b}, b, f"{actor} moving a group inside a tenant")
            _moved(opts, s, {"parent": x}, x, f"{actor} moving a group to another tree")
            _moved(opts, s, {"parent": None}, None, f"{actor} detaching a group")
            _moved(opts, s, {"parent": a}, a, f"{actor} attaching a group to a tenant")
            opts.client.logout()
    finally:
        opts.client.logout()
        _restore(opts)


@th.django_unit_test("#6364: server code with no request: save() is not checked, update_from_dict is refused")
def test_server_code_without_a_request(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import Group
    _restore(opts)
    s, a, b = opts.ids["s"], opts.ids["a"], opts.ids["b"]
    try:
        refused = False
        try:
            Group.objects.get(pk=s).update_from_dict({"parent": b})
        except merrors.PermissionDeniedException:
            refused = True
        assert refused and _parent_id(s) == a, \
            f"update_from_dict with no request must not move a group: refused={refused}, " \
            f"parent is {_parent_id(s)}"
        group = Group.objects.get(pk=s)
        group.parent_id = b
        group.save()
        assert _parent_id(s) == b, \
            f"a plain save() from server code must still move a group, parent is {_parent_id(s)}"
    finally:
        _restore(opts)
