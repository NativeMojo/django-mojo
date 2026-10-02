"""Maestro item #6350 — who may decide where a tenant's sign-in links land.

#6225 builds password-reset, magic-login and invite links on the
`webapp_base_url` (and `webapp_auth_path`) stored in the metadata of the tenant
that created the account. Anyone who could save a group could store one, so a
manager of any sub-group could move the links of every account in the tenant,
and a group carrying an address could be moved into another tenant's tree.

Over REST, changing either key, or moving a group to a different tree, now
needs a signed-in person holding global `manage_groups` or `groups` (or a
superuser). A group API key never passes.

Fixtures are made by ORM and every test restores them first, so the tests do
not depend on each other's order.
"""
import uuid as _uuid

from testit import helpers as th

TESTIT_TIER = "bug"

PASSWORD = "Gwu##guard99"
PREFIX = "gwu_"
A_BASE = "https://app.gwu-tenant.example"
A_PATH = "/login"
W1_BASE = "https://w1.gwu-outside.example"
EVIL = "https://evil.gwu.example"
KEYS = ("webapp_base_url", "webapp_auth_path")
LOG_KIND = "group:webapp_url_changed"

USERS = ("sub_mgr", "top_mgr", "named_mgr", "global_mg", "global_groups", "super",
         "w_mgr", "k_mgr", "account")


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
    """Put every fixture group back: metadata and parent, straight in the table."""
    from mojo.apps.account.models import Group
    for pk, (parent_id, metadata) in opts.shape.items():
        Group.objects.filter(pk=pk).update(parent_id=parent_id, metadata=metadata)


def _login(opts, name):
    ok = opts.client.login(_email(name), PASSWORD)
    assert ok, f"login failed for {name}: {opts.client.last_response.body}"


def _use_key(opts, token):
    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = token
    opts.client.is_authenticated = True


def _stored(pk):
    from mojo.apps.account.models import Group
    return Group.objects.filter(pk=pk).values_list("metadata", flat=True).first()


def _parent_id(pk):
    from mojo.apps.account.models import Group
    return Group.objects.filter(pk=pk).values_list("parent_id", flat=True).first()


def _post(opts, pk, payload):
    return opts.client.post(f"/api/group/{pk}", payload)


def _refused(opts, pk, payload, what):
    """The save is refused and neither key changed on the row."""
    before = _stored(pk)
    resp = _post(opts, pk, payload)
    assert resp.status_code == 403, \
        f"{what} must be refused with 403, got {resp.status_code}: {opts.client.last_response.body}"
    after = _stored(pk)
    for key in KEYS:
        assert (key in before, before.get(key)) == (key in after, after.get(key)), \
            f"SECURITY: {what} changed {key} from {before.get(key)!r} to {after.get(key)!r}"


def _link(user, group):
    from mojo.apps.account.utils.webapp_url import build_token_url
    return build_token_url("magic_login", "tok", user=user, group=group, operator_origins=[])


@th.django_unit_setup()
def setup_group_webapp_url_guard(opts):
    from mojo.apps.account.models import ApiKey, Group, User

    ApiKey.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(email__startswith=PREFIX).delete()
    for _ in range(4):  # children first: parent is a cascading foreign key
        Group.objects.filter(name__startswith=PREFIX, groups__isnull=True).delete()

    def group(name, parent=None, **metadata):
        return Group.objects.create(
            name=f"{PREFIX}{name}", kind="organization", is_active=True,
            parent=parent, metadata=metadata)

    a_top = group("a_top", webapp_base_url=A_BASE, webapp_auth_path=A_PATH, motto="top")
    a_child = group("a_child", a_top, motto="child")
    a_child2 = group("a_child2", a_top)
    a_leaf = group("a_leaf", a_child)
    w_top = group("w_top")
    w1 = group("w1", w_top, webapp_base_url=W1_BASE)
    k_top = group("k_top")
    ctl = group("ctl_top", motto="ctl")
    opts.ids = {"a_top": a_top.pk, "a_child": a_child.pk, "a_child2": a_child2.pk,
                "a_leaf": a_leaf.pk, "w_top": w_top.pk, "w1": w1.pk, "k_top": k_top.pk,
                "ctl": ctl.pk}
    opts.shape = {g.pk: (g.parent_id, dict(g.metadata))
                  for g in (a_top, a_child, a_child2, a_leaf, w_top, w1, k_top, ctl)}

    # Member-level managers. None of them holds a global permission.
    sub_mgr = _mk_user("sub_mgr", org=a_child)
    opts.sub_member_id = _member(a_child, sub_mgr, "manage_group").pk
    opts.sub_mgr_id = sub_mgr.pk
    _member(a_top, _mk_user("top_mgr"), "manage_group")
    _member(a_top, _mk_user("named_mgr"), "manage_groups", "groups")
    # Outside managers who are plain members of tenant A, which is all an
    # attach-by-id asks for.
    w_mgr = _mk_user("w_mgr")
    _member(w_top, w_mgr, "manage_group")
    _member(a_child, w_mgr)
    k_mgr = _mk_user("k_mgr")
    _member(k_top, k_mgr, "manage_group")
    _member(a_child, k_mgr)

    # Global holders. global_mg is also a member of the child, so a key can act as them.
    global_mg = _mk_user("global_mg")
    global_mg.add_permission("manage_groups")
    _member(a_child, global_mg)
    opts.global_mg_id = global_mg.pk
    _mk_user("global_groups").add_permission("groups")
    _mk_user("super", is_superuser=True)

    # The account whose links are at stake: created by tenant A.
    opts.account_id = _mk_user("account", org=a_top).pk


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

@th.django_unit_test("#6350: a sub-group manager cannot store a site address, by any spelling")
def test_sub_group_manager_cannot_set_address(opts):
    _restore(opts)
    child = opts.ids["a_child"]
    _login(opts, "sub_mgr")
    try:
        resp = _post(opts, child, {"metadata": {"motto": "edited"}})
        assert resp.status_code == 200, \
            f"an unrelated metadata edit must still work, got {resp.status_code}: {opts.client.last_response.body}"
        assert _stored(child).get("motto") == "edited", "the unrelated edit did not persist"

        attempts = (
            ("a merge of webapp_base_url", {"metadata": {"webapp_base_url": EVIL}}),
            ("a merge of webapp_auth_path", {"metadata": {"webapp_auth_path": "/steal"}}),
            ("a replace carrying the key", {"metadata": {"__replace": True, "webapp_base_url": EVIL}}),
            ("a dotted key", {"metadata.webapp_base_url": EVIL}),
            ("metadata as a JSON string", {"metadata": '{"webapp_base_url": "%s"}' % EVIL}),
            ("a replace that stores a literal null",
             {"metadata": {"__replace": True, "motto": "edited", "webapp_base_url": None}}),
            ("an empty string where nothing is stored", {"metadata": {"webapp_auth_path": ""}}),
        )
        for what, payload in attempts:
            _refused(opts, child, payload, f"sub-group manager: {what}")

        # A merged null removes a key. Where none is stored that changes nothing.
        resp = _post(opts, child, {"metadata": {"webapp_base_url": None}})
        assert resp.status_code == 200 and "webapp_base_url" not in _stored(child), \
            f"a null merged where nothing is stored changes nothing, got {resp.status_code} and {_stored(child)}"
    finally:
        opts.client.logout()


@th.django_unit_test("#6350: a group API key cannot store a site address, whatever it carries")
def test_group_api_keys_cannot_set_address(opts):
    from mojo.apps.account.models import ApiKey, Group, User
    _restore(opts)
    child = Group.objects.get(pk=opts.ids["a_child"])
    top = Group.objects.get(pk=opts.ids["a_top"])
    holder = User.objects.get(pk=opts.global_mg_id)
    tag = _uuid.uuid4().hex[:6]
    keys = (
        ("a key on the sub-group with manage_group", child,
         dict(permissions={"manage_group": True})),
        ("a key on the sub-group that also lists manage_groups and groups", child,
         dict(permissions={"manage_group": True, "manage_groups": True, "groups": True})),
        ("a key on the tenant's top group", top,
         dict(permissions={"manage_group": True})),
        ("a key acting as a member who holds global manage_groups", child,
         dict(permissions={"manage_group": True}, user=holder, override_user=True)),
    )
    try:
        for index, (what, group, kwargs) in enumerate(keys):
            _, token = ApiKey.create_for_group(group=group, name=f"{PREFIX}key_{tag}_{index}", **kwargs)
            _use_key(opts, token)
            resp = _post(opts, group.pk, {"metadata": {"motto": f"key-{index}"}})
            assert resp.status_code == 200, \
                f"{what} must still save its own group, got {resp.status_code}: {opts.client.last_response.body}"
            _refused(opts, group.pk, {"metadata": {"webapp_base_url": EVIL}}, what)
            _refused(opts, group.pk, {"metadata": {"webapp_auth_path": "/steal"}}, what)
    finally:
        opts.client.logout()
        ApiKey.objects.filter(name__startswith=PREFIX).delete()


@th.django_unit_test("#6350: a member-level manager of the top group cannot change or clear the address")
def test_top_group_manager_cannot_change_or_clear(opts):
    _restore(opts)
    top = opts.ids["a_top"]
    attempts = (
        ("a change", {"metadata": {"webapp_base_url": EVIL}}),
        ("a clear with null", {"metadata": {"webapp_base_url": None}}),
        ("a replace that leaves the key out", {"metadata": {"__replace": True, "motto": "wiped"}}),
        ("a change of the auth path", {"metadata": {"webapp_auth_path": "/steal"}}),
    )
    for name in ("top_mgr", "named_mgr"):
        _login(opts, name)
        try:
            for what, payload in attempts:
                _refused(opts, top, payload, f"{name}: {what}")
        finally:
            opts.client.logout()
    stored = _stored(top)
    assert stored.get("webapp_base_url") == A_BASE and stored.get("webapp_auth_path") == A_PATH, \
        f"the tenant's address must be as it was, got {stored}"


@th.django_unit_test("#6350: a nested save through another row cannot store a site address")
def test_nested_saves_cannot_set_address(opts):
    _restore(opts)
    child = opts.ids["a_child"]
    _login(opts, "sub_mgr")
    try:
        # A user's own row carries their org, and a dict there saves the org.
        resp = opts.client.post(f"/api/user/{opts.sub_mgr_id}",
                                {"org": {"metadata": {"motto": "via-user"}}})
        assert _stored(child).get("motto") == "via-user", \
            f"this test needs the nested org save to be reachable, got {resp.status_code}: " \
            f"{opts.client.last_response.body}"
        for key, value in (("webapp_base_url", EVIL), ("webapp_auth_path", "/steal")):
            opts.client.post(f"/api/user/{opts.sub_mgr_id}", {"org": {"metadata": {key: value}}})
            assert key not in _stored(child), \
                f"SECURITY: a save through the user row stored {key}: {_stored(child)}"

        # A member row's `group` is read by the dispatcher as the tenant to act
        # in, so a dict there never reaches a save. Kept as a control.
        resp = opts.client.post(f"/api/group/member/{opts.sub_member_id}",
                                {"group": {"metadata": {"webapp_base_url": EVIL}}})
        assert resp.status_code != 200 and "webapp_base_url" not in _stored(child), \
            f"SECURITY: a save through the member row got {resp.status_code} and left {_stored(child)}"
    finally:
        opts.client.logout()


@th.django_unit_test("#6350: a group that carries an address cannot be grafted into a tenant")
def test_graft_of_a_group_with_an_address(opts):
    from mojo.apps.account.models import Group, User
    _restore(opts)
    w_top, w1 = opts.ids["w_top"], opts.ids["w1"]
    account = User.objects.get(pk=opts.account_id)
    _login(opts, "w_mgr")
    try:
        resp = _post(opts, w_top, {"name": f"{PREFIX}w_top"})
        assert resp.status_code == 200, \
            f"the outside manager must be able to save their own group, got {resp.status_code}"
        resp = _post(opts, w_top, {"parent": opts.ids["a_child"]})
        assert resp.status_code == 403, \
            f"moving a group into another tenant's tree must be refused, got {resp.status_code}: " \
            f"{opts.client.last_response.body}"
        assert _parent_id(w_top) is None, \
            f"SECURITY: the outside group was attached under group {_parent_id(w_top)}"
    finally:
        opts.client.logout()
    url = _link(account, Group.objects.get(pk=w1))
    assert url.startswith(f"{A_BASE}/") and "gwu-outside" not in url, \
        f"SECURITY: a link for the tenant's account, built with the outside group, went to {url}"


@th.django_unit_test("#6350: a group with no address cannot be moved into or out of a tenant either")
def test_tree_moves_need_the_global_permission(opts):
    _restore(opts)
    k_top, leaf = opts.ids["k_top"], opts.ids["a_leaf"]
    _login(opts, "k_mgr")
    try:
        resp = _post(opts, k_top, {"parent": opts.ids["a_child"]})
        assert resp.status_code == 403, \
            f"attaching a whole tree under another tenant must be refused, got {resp.status_code}"
        assert _parent_id(k_top) is None, f"SECURITY: the tree was attached under {_parent_id(k_top)}"
    finally:
        opts.client.logout()
    _login(opts, "top_mgr")
    try:
        for what, payload in (("null", {"parent": None}), ("0", {"parent": 0})):
            resp = _post(opts, leaf, payload)
            assert resp.status_code == 403, \
                f"detaching a sub-group from its tenant with {what} must be refused, got {resp.status_code}"
            assert _parent_id(leaf) == opts.ids["a_child"], \
                f"SECURITY: the sub-group left its tenant, parent is {_parent_id(leaf)}"
    finally:
        opts.client.logout()


@th.django_unit_test("#6350: a save with no request cannot change the address or the tree")
def test_save_outside_a_request_is_refused(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import Group
    _restore(opts)
    child = Group.objects.get(pk=opts.ids["a_child"])
    for what, payload in (
            ("the address", {"metadata": {"webapp_base_url": EVIL}}),
            ("the auth path", {"metadata": {"webapp_auth_path": "/steal"}}),
            ("the tree", {"parent": None})):
        refused = False
        try:
            Group.objects.get(pk=child.pk).update_from_dict(payload)
        except merrors.PermissionDeniedException:
            refused = True
        assert refused, f"update_from_dict with no request must not change {what}"
    stored = _stored(child.pk)
    assert "webapp_base_url" not in stored and "webapp_auth_path" not in stored, \
        f"SECURITY: a save with no request stored {stored}"
    assert _parent_id(child.pk) == opts.ids["a_top"], "SECURITY: a save with no request moved the group"
    Group.objects.get(pk=child.pk).update_from_dict({"metadata": {"motto": "from-a-job"}})
    assert _stored(child.pk).get("motto") == "from-a-job", \
        "a save with no request must still change an ordinary setting"


@th.django_unit_test("#6350: a broken settings value on a parent does not break the lookup")
def test_lookup_skips_metadata_that_is_not_a_dict(opts):
    from mojo.apps.account.models import Group
    _restore(opts)
    try:
        for broken in (0, [], "text"):
            Group.objects.filter(pk=opts.ids["a_child"]).update(metadata=broken)
            leaf = Group.objects.get(pk=opts.ids["a_leaf"])
            value = leaf.get_metadata_value("webapp_base_url")
            assert value == A_BASE, \
                f"with metadata {broken!r} on the parent, the lookup should reach the top group's value, got {value!r}"
    finally:
        _restore(opts)


# ---------------------------------------------------------------------------
# What keeps working
# ---------------------------------------------------------------------------

@th.django_unit_test("#6350: a manager can still edit other settings beside a stored address")
def test_unchanged_address_in_the_payload_is_fine(opts):
    _restore(opts)
    top = opts.ids["a_top"]
    _login(opts, "top_mgr")
    try:
        resp = _post(opts, top, {"metadata": {"motto": "edited"}})
        assert resp.status_code == 200, \
            f"an unrelated edit beside a stored address must work, got {resp.status_code}: {opts.client.last_response.body}"
        whole = {"__replace": True, "webapp_base_url": A_BASE, "webapp_auth_path": A_PATH, "motto": "resent"}
        resp = _post(opts, top, {"metadata": whole})
        assert resp.status_code == 200, \
            f"resending the whole metadata unchanged must work, got {resp.status_code}: {opts.client.last_response.body}"
    finally:
        opts.client.logout()
    stored = _stored(top)
    assert stored == {"webapp_base_url": A_BASE, "webapp_auth_path": A_PATH, "motto": "resent"}, \
        f"the stored metadata is not what was sent: {stored}"


@th.django_unit_test("#6350: a holder of the global permission can set, change and clear, and each change is logged")
def test_global_holders_can_set_change_and_clear(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.logit.models import Log
    ctl = opts.ids["ctl"]
    for name in ("global_mg", "global_groups", "super"):
        _restore(opts)
        logs = Log.objects.filter(kind=LOG_KIND, model_id=ctl)
        before = logs.count()
        _login(opts, name)
        try:
            steps = (
                ("set", {"metadata": {"webapp_base_url": A_BASE}}, A_BASE),
                ("change", {"metadata": {"webapp_base_url": W1_BASE}}, W1_BASE),
                ("clear", {"metadata": {"webapp_base_url": None}}, None),
            )
            for what, payload, expected in steps:
                resp = _post(opts, ctl, payload)
                assert resp.status_code == 200, \
                    f"{name} must be able to {what} the address, got {resp.status_code}: {opts.client.last_response.body}"
                assert _stored(ctl).get("webapp_base_url") == expected, \
                    f"{name}: after {what} the stored address should be {expected!r}, got {_stored(ctl)}"
            assert logs.count() == before + 3, \
                f"{name}: three changes must leave three {LOG_KIND} rows, got {logs.count() - before}"
            resp = _post(opts, ctl, {"metadata": {"motto": "no change of address"}})
            assert resp.status_code == 200, f"{name}: an unrelated edit must work, got {resp.status_code}"
            assert logs.count() == before + 3, "an edit that leaves the address alone must not be logged as a change"
        finally:
            opts.client.logout()

    _login(opts, "global_mg")
    try:
        name = f"{PREFIX}made_{_uuid.uuid4().hex[:6]}"
        resp = opts.client.post("/api/group", {
            "name": name, "kind": "organization", "metadata": {"webapp_base_url": A_BASE}})
        assert resp.status_code == 200, \
            f"a global holder must be able to create a group with an address, got {resp.status_code}: " \
            f"{opts.client.last_response.body}"
        made = Group.objects.get(name=name)
        assert made.metadata.get("webapp_base_url") == A_BASE, f"the created group lost its address: {made.metadata}"
        made.delete()
    finally:
        opts.client.logout()


@th.django_unit_test("#6350: a tenant manager can still move a sub-group inside the tenant")
def test_move_inside_one_tenant_still_works(opts):
    _restore(opts)
    leaf = opts.ids["a_leaf"]
    _login(opts, "top_mgr")
    try:
        resp = _post(opts, leaf, {"parent": opts.ids["a_child2"]})
        assert resp.status_code == 200, \
            f"moving a sub-group to another parent in the same tenant must work, got {resp.status_code}: " \
            f"{opts.client.last_response.body}"
        assert _parent_id(leaf) == opts.ids["a_child2"], f"the move did not persist, parent is {_parent_id(leaf)}"
    finally:
        opts.client.logout()
        _restore(opts)


@th.django_unit_test("#6350: a holder of the global permission can move a group between trees")
def test_global_holder_can_move_between_trees(opts):
    _restore(opts)
    k_top = opts.ids["k_top"]
    _login(opts, "global_mg")
    try:
        resp = _post(opts, k_top, {"parent": opts.ids["a_child"]})
        assert resp.status_code == 200, \
            f"a global holder must be able to attach a group to a tenant, got {resp.status_code}: " \
            f"{opts.client.last_response.body}"
        assert _parent_id(k_top) == opts.ids["a_child"], f"the attach did not persist, parent is {_parent_id(k_top)}"
        resp = _post(opts, k_top, {"parent": None})
        assert resp.status_code == 200, f"a global holder must be able to detach it again, got {resp.status_code}"
        assert _parent_id(k_top) is None, f"the detach did not persist, parent is {_parent_id(k_top)}"
    finally:
        opts.client.logout()
        _restore(opts)


# ---------------------------------------------------------------------------
# Overlapping saves: a save that began before an operator's change must not
# write the old address or the old parent back.
# ---------------------------------------------------------------------------

def _overlapping_save(opts, pk, actor, payload, meanwhile):
    """Run the REST save of group `pk` as `actor` in this process, and run
    `meanwhile()` after its permission check, just before its write.

    The row is loaded first, as a request loads it, so it carries what was
    stored before `meanwhile()` runs. The hook is on this one instance only.
    """
    from objict import objict
    from mojo.apps.account.models import Group, User
    from mojo.models import rest as mojo_rest
    stale = Group.objects.get(pk=pk)
    request = objict(
        user=User.objects.get(email=_email(actor)), DATA=objict(payload),
        QUERY_PARAMS=objict(), method="POST", group=stale, bearer=None,
        ip="127.0.0.1", path=f"/api/group/{pk}", META={}, api_key=None,
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


def _operator_posts(opts, pk, payload, what):
    _login(opts, "global_mg")
    try:
        resp = _post(opts, pk, payload)
        assert resp.status_code == 200, \
            f"the operator must be able to {what}, got {resp.status_code}: {opts.client.last_response.body}"
    finally:
        opts.client.logout()


@th.django_unit_test("#6350: a manager's save already in flight cannot undo an operator's change of address")
def test_overlapping_manager_save_keeps_the_operators_address(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.logit.models import Log
    top = opts.ids["a_top"]
    account = User.objects.get(pk=opts.account_id)
    logs = Log.objects.filter(kind=LOG_KIND, model_id=top)
    cases = (
        # the manager edits something else while the operator corrects the address
        ("corrects", {"metadata": {"motto": "overlap"}},
         {"metadata": {"webapp_base_url": W1_BASE}}, W1_BASE),
        # the manager's form sends the whole settings back, old address included,
        # while the operator removes that address
        ("removes", {"metadata": {"motto": "overlap", "webapp_base_url": A_BASE}},
         {"metadata": {"webapp_base_url": None}}, None),
    )
    try:
        for what, manager_payload, operator_payload, expected in cases:
            _restore(opts)
            before = logs.count()

            def operator_saves():
                _operator_posts(opts, top, operator_payload, f"{what} the address")
                assert _stored(top).get("webapp_base_url") == expected, \
                    f"the operator's save did not land: {_stored(top)}"

            _overlapping_save(opts, top, "top_mgr", manager_payload, operator_saves)
            stored = _stored(top)
            assert stored.get("webapp_base_url") == expected, \
                f"SECURITY: the operator {what} the address and a manager's overlapping save put " \
                f"{stored.get('webapp_base_url')!r} back"
            assert stored.get("motto") == "overlap", \
                f"the manager's own edit must still land beside the operator's, got {stored}"
            assert logs.count() == before + 1, \
                f"only the operator's change is a change of address, got {logs.count() - before} log rows"
            if expected:
                link = _link(account, Group.objects.get(pk=top))
                assert link.startswith(expected), \
                    f"SECURITY: after the overlap the tenant's link is built on {link}, not {expected}"
    finally:
        _restore(opts)


@th.django_unit_test("#6350: a manager's save already in flight cannot undo an operator's move of a group")
def test_overlapping_manager_save_keeps_the_operators_move(opts):
    child2 = opts.ids["a_child2"]
    _restore(opts)
    try:
        def operator_moves():
            _operator_posts(opts, child2, {"parent": opts.ids["w_top"]}, "move the group to another tree")
            assert _parent_id(child2) == opts.ids["w_top"], "the operator's move did not land"

        _overlapping_save(opts, child2, "top_mgr", {"metadata": {"motto": "overlap"}}, operator_moves)
        assert _parent_id(child2) == opts.ids["w_top"], \
            f"SECURITY: the operator moved the group out of the tenant and a manager's overlapping save " \
            f"moved it back under {_parent_id(child2)}"
        assert _stored(child2).get("motto") == "overlap", \
            f"the manager's own edit must still land, got {_stored(child2)}"
    finally:
        _restore(opts)


@th.django_unit_test("#6350: an operator's save that arrives during a manager's write waits for it, then stands")
def test_operator_save_waits_for_a_write_in_progress(opts):
    """Two connections at once: this process holds the manager's row lock while
    the server takes the operator's request."""
    import threading
    from mojo.apps.account.models import Group
    top = opts.ids["a_top"]
    _restore(opts)
    try:
        stale = Group.objects.get(pk=top)
        stale.metadata["motto"] = "overlap"
        outcome = {}

        def operator_saves():
            try:
                _operator_posts(opts, top, {"metadata": {"webapp_base_url": W1_BASE}}, "change the address")
                outcome["ok"] = True
            except AssertionError as err:
                outcome["error"] = str(err)

        worker = threading.Thread(target=operator_saves)

        def between_the_lock_and_the_write():
            worker.start()
            worker.join(timeout=1.5)
            outcome["finished_early"] = not worker.is_alive()

        # save_secrets() runs inside save(), after the row is locked and before
        # it is written. Hooked on this one instance.
        stale.save_secrets = between_the_lock_and_the_write
        stale.save()
        worker.join(timeout=20)
        assert not worker.is_alive(), "the operator's save never returned after the manager's write finished"
        assert outcome.get("ok"), f"the operator's save failed: {outcome.get('error')}"
        assert not outcome["finished_early"], \
            "SECURITY: the operator's save finished while a manager's write of the same row was in " \
            "progress, so that write could replace it"
        assert _stored(top).get("webapp_base_url") == W1_BASE, \
            f"SECURITY: after both saves the address is {_stored(top).get('webapp_base_url')!r}, " \
            f"not the operator's"
    finally:
        _restore(opts)


@th.django_unit_test("#6350: a save outside REST that did not touch the address does not write an old one back")
def test_stale_save_outside_rest_keeps_the_address(opts):
    from mojo.apps.account.models import Group
    top = opts.ids["a_top"]
    writers = (
        ("touch()", lambda group: (setattr(group, "last_activity", None), group.touch())),
        ("save()", lambda group: (group.metadata.update(motto="job"), group.save())),
        ("set_protected_metadata()", lambda group: group.set_protected_metadata("gwu_flag", True)),
    )
    try:
        for name, write in writers:
            _restore(opts)
            stale = Group.objects.get(pk=top)
            _operator_posts(opts, top, {"metadata": {"webapp_base_url": W1_BASE}}, "change the address")
            write(stale)
            assert _stored(top).get("webapp_base_url") == W1_BASE, \
                f"SECURITY: {name} on a row loaded before the operator's change put " \
                f"{_stored(top).get('webapp_base_url')!r} back"

        # Server code that sets the address itself still does: assign and save.
        _restore(opts)
        mine = Group.objects.get(pk=top)
        _operator_posts(opts, top, {"metadata": {"webapp_base_url": W1_BASE}}, "change the address")
        mine.metadata["webapp_base_url"] = EVIL.replace("evil", "job")
        mine.save()
        assert _stored(top).get("webapp_base_url") == EVIL.replace("evil", "job"), \
            f"an address assigned by server code and saved must be stored, got {_stored(top)}"
    finally:
        _restore(opts)
