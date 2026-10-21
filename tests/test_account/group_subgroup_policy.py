"""Maestro item #9107 — a parent's administrators may create sub-groups when
the platform opts the parent in.

Group has no tenant field, so a create needs a GLOBAL group permission. A
parent whose metadata["protected"]["allow_member_subgroups"] is exactly true
admits one more caller: a person who manages that parent as a member (a member
row on it or an ancestor with a SAVE_PERMS grant) may create a group directly
under it with POST /api/group. Group API keys and group tokens never take
this path. Only the protected-metadata gate (or server code) can set the key,
it does not pass to descendants, and save() judges the create again under its
row locks.

The tree: tenant T (policy on) with children P (policy on) and Q (policy off),
a separate tenant U (policy on) and a separate root X.

Fixtures are made by ORM and every test restores them first, so the tests do
not depend on each other's order.
"""
import uuid as _uuid

from testit import helpers as th

TESTIT_TIER = "framework"

PASSWORD = "Gsp##policy99"
PREFIX = "gsp_"
KEY = "allow_member_subgroups"
_ABSENT = object()

# the fixture policy of every group, restored before each test
POLICY = {"t": True, "p": True, "q": _ABSENT, "u": True, "x": _ABSENT}


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


def _metadata_for(policy):
    return {} if policy is _ABSENT else {"protected": {KEY: policy}}


def _set_metadata(pk, metadata):
    from mojo.apps.account.models import Group
    Group.objects.filter(pk=pk).update(metadata=metadata)


def _policy(pk):
    """The stored policy value of group `pk`, or _ABSENT."""
    from mojo.apps.account.models import Group
    metadata = Group.objects.filter(pk=pk).values_list("metadata", flat=True).first()
    protected = metadata.get("protected") if isinstance(metadata, dict) else None
    if not isinstance(protected, dict) or KEY not in protected:
        return _ABSENT
    return protected[KEY]


def _restore(opts):
    """Put every fixture group back as it was, straight in the table, and
    remove any group a test created."""
    from mojo.apps.account.models import ApiKey, Group, UserAPIKey
    for _ in range(4):  # children first: parent is a cascading foreign key
        Group.objects.filter(name__startswith=f"{PREFIX}new_", groups__isnull=True).delete()
    for name, pk in opts.ids.items():
        Group.objects.filter(pk=pk).update(
            parent_id=opts.shape[pk], is_active=True, metadata=_metadata_for(POLICY[name]))
    ApiKey.objects.filter(name__startswith=PREFIX).delete()
    UserAPIKey.objects.filter(user__email__startswith=PREFIX).delete()


def _login(opts, name):
    opts.client.logout()
    ok = opts.client.login(_email(name), PASSWORD)
    assert ok, f"login failed for {name}: {opts.client.last_response.body}"


def _made(name):
    from mojo.apps.account.models import Group
    return Group.objects.filter(name=f"{PREFIX}new_{name}").first()


def _create(opts, name, path="/api/group", headers=None, **fields):
    """POST a new group named gsp_new_<name>; returns (response, row or None)."""
    payload = {"name": f"{PREFIX}new_{name}", "kind": "organization"}
    payload.update(fields)
    if headers:
        resp = opts.client.post(path, payload, headers=headers)
    else:
        resp = opts.client.post(path, payload)
    return resp, _made(name)


def _refused(opts, name, what, **fields):
    resp, made = _create(opts, name, **fields)
    assert resp.status_code == 403, \
        f"{what} must be refused with 403, got {resp.status_code}: {opts.client.last_response.body}"
    assert made is None, \
        f"SECURITY: {what} created group {made.pk} under parent {made.parent_id}"


def _created(opts, name, expected_parent, what, **fields):
    resp, made = _create(opts, name, **fields)
    assert resp.status_code == 200, \
        f"{what} must be allowed, got {resp.status_code}: {opts.client.last_response.body}"
    assert made is not None and made.parent_id == expected_parent, \
        f"{what} did not land under {expected_parent}: {getattr(made, 'parent_id', 'no row')}"
    return resp, made


def _logged(kind, pk):
    from mojo.apps.logit.models import Log
    return Log.objects.filter(kind=kind, model_name="account.Group", model_id=pk).exists()


def _rest_request(actor, data):
    from objict import objict
    from mojo.apps.account.models import User
    return objict(
        user=User.objects.get(email=_email(actor)), DATA=data,
        QUERY_PARAMS=objict(), method="POST", group=None, bearer=None,
        ip="127.0.0.1", path="/api/group", META={}, api_key=None,
        group_token=None)


def _rest_save(actor, instance, data):
    """The REST model save of `instance` as `actor`, without the HTTP layer,
    so a test can step in at a chosen point of it."""
    from objict import objict
    from mojo.models import rest as mojo_rest
    request = _rest_request(actor, objict(data))
    token = mojo_rest.ACTIVE_REQUEST.set(request)
    try:
        instance.on_rest_save(request, request.DATA)
    finally:
        mojo_rest.ACTIVE_REQUEST.reset(token)


def _elsewhere(work):
    """Run `work()` to its end on another database connection, as another
    request would, and return what it returned."""
    import threading
    from django.db import connections
    out = {}

    def run():
        try:
            out["result"] = work()
        except BaseException as exc:  # reported by the caller's assert
            out["error"] = repr(exc)
        finally:
            connections.close_all()

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(15)
    assert not worker.is_alive(), "the other connection is still waiting: it was blocked"
    assert "error" not in out, f"the other connection failed: {out['error']}"
    return out.get("result")


@th.django_unit_setup()
def setup_group_subgroup_policy(opts):
    from mojo.apps.account.models import ApiKey, Group, User, UserAPIKey

    ApiKey.objects.filter(name__startswith=PREFIX).delete()
    UserAPIKey.objects.filter(user__email__startswith=PREFIX).delete()
    User.objects.filter(email__startswith=PREFIX).delete()
    for _ in range(5):  # children first: parent is a cascading foreign key
        Group.objects.filter(name__startswith=PREFIX, groups__isnull=True).delete()

    def group(name, parent=None):
        return Group.objects.create(
            name=f"{PREFIX}{name}", kind="organization", is_active=True, parent=parent,
            metadata=_metadata_for(POLICY[name]))

    t = group("t")
    p = group("p", t)
    q = group("q", t)
    u = group("u")
    x = group("x")
    opts.ids = {"t": t.pk, "p": p.pk, "q": q.pk, "u": u.pk, "x": x.pk}
    opts.shape = {g.pk: g.parent_id for g in (t, p, q, u, x)}

    # Member-level managers. None of them holds a global permission.
    _member(p, _mk_user("p_mgr"), "manage_group")
    _member(t, _mk_user("t_mgr"), "manage_group")
    _member(q, _mk_user("q_mgr"), "manage_group")
    _member(u, _mk_user("u_mgr"), "manage_group")
    _member(p, _mk_user("plain"))
    shadowed = _mk_user("shadowed")     # manages the tenant, plain member of P
    _member(t, shadowed, "manage_group")
    _member(p, shadowed)

    _mk_user("row_mgr").add_permission("manage_group")
    _mk_user("global_mg").add_permission("manage_groups")
    _mk_user("super", is_superuser=True)


# ---------------------------------------------------------------------------
# Who may create under an opted-in parent
# ---------------------------------------------------------------------------

@th.django_unit_test("#9107: a parent manager creates a sub-group under an opted-in parent")
def test_parent_manager_creates_under_opted_in_parent(opts):
    from mojo.apps.account.models import Group, User
    _restore(opts)
    p = opts.ids["p"]
    p_mgr = User.objects.get(email=_email("p_mgr"))
    assert not p_mgr.has_permission(Group.RestMeta.SAVE_PERMS), \
        "this test needs a manager with no global group permission"
    try:
        _login(opts, "p_mgr")
        _, made = _created(opts, "by_p_mgr", p, "a manager of an opted-in parent creating under it", parent=p)
        assert _logged("group:subgroup_created", made.pk), \
            f"a create admitted by the parent's policy must be logged as group:subgroup_created (pk={made.pk})"
        assert _logged("model:created", made.pk), \
            f"a create admitted by the parent's policy must still log model:created (pk={made.pk})"
        _created(opts, "as_parent_id", p, "the same create spelled parent_id", parent_id=p)
        _created(opts, "as_string", p, "the same create with the id as a string", parent=str(p))
        _created(opts, "both_spellings", p, "the same create with parent and parent_id equal",
                 parent=p, parent_id=p)

        _login(opts, "t_mgr")
        _created(opts, "by_t_mgr", p, "a manager of the parent's tenant creating under it", parent=p)
    finally:
        opts.client.logout()
        _restore(opts)


@th.django_unit_test("#9107: an absent, false or malformed policy admits nothing")
def test_policy_off_or_malformed_admits_nothing(opts):
    _restore(opts)
    p, q = opts.ids["p"], opts.ids["q"]
    try:
        _login(opts, "q_mgr")
        _refused(opts, "under_q", "a manager of a parent with no policy creating under it", parent=q)
        _login(opts, "p_mgr")
        for label, metadata in (
                ("false", {"protected": {KEY: False}}),
                ("the string true", {"protected": {KEY: "true"}}),
                ("1", {"protected": {KEY: 1}}),
                ("a dict", {"protected": {KEY: {}}}),
                ("protected as a string", {"protected": "allow_member_subgroups"}),
                ("the key outside protected", {KEY: True}),
                ("a literal dotted key", {f"protected.{KEY}": True})):
            _set_metadata(p, metadata)
            _refused(opts, "malformed", f"a create under a parent whose policy is {label}", parent=p)
    finally:
        opts.client.logout()
        _restore(opts)


@th.django_unit_test("#9107: nobody else borrows an opted-in parent's policy")
def test_others_are_refused(opts):
    _restore(opts)
    p, q, x = opts.ids["p"], opts.ids["q"], opts.ids["x"]
    try:
        for actor, what in (("plain", "a plain member of the parent"),
                            ("shadowed", "a tenant manager whose own row on the parent has no grant"),
                            ("u_mgr", "a manager of another opted-in tenant"),
                            ("q_mgr", "a manager of a sibling parent"),
                            ("row_mgr", "a user-row manage_group holder with no member grant")):
            _login(opts, actor)
            _refused(opts, f"by_{actor}", f"{what} creating under an opted-in parent", parent=p)

        _login(opts, "p_mgr")
        _refused(opts, "q_via_group", "a create under Q with group= naming the opted-in parent",
                 path=f"/api/group?group={p}", parent=q)
        _refused(opts, "under_x", "a create under a parent in another tree", parent=x)
        _refused(opts, "aliases", "a create whose parent and parent_id disagree", parent=p, parent_id=q)
        _refused(opts, "aliases_back", "the same disagreement the other way round", parent=q, parent_id=p)
        _refused(opts, "no_parent", "a create with no parent")
        for label, value in (("a dict", {"id": p}), ("true", True), ("blank", ""),
                             ("a superscript digit", "²"), ("an id out of range", "9" * 30)):
            _refused(opts, "bad_parent", f"a create with the parent posted as {label}", parent=value)
    finally:
        opts.client.logout()
        _restore(opts)


@th.django_unit_test("#9107: a deactivated parent or tenant admits nothing")
def test_deactivated_parent_or_tenant(opts):
    from mojo.apps.account.models import Group
    _restore(opts)
    p, t = opts.ids["p"], opts.ids["t"]
    try:
        _login(opts, "p_mgr")
        Group.objects.filter(pk=p).update(is_active=False)
        _refused(opts, "inactive_parent", "a create under a deactivated parent", parent=p)
        Group.objects.filter(pk=p).update(is_active=True)
        Group.objects.filter(pk=t).update(is_active=False)
        _refused(opts, "inactive_tenant", "a create under a parent whose tenant is deactivated", parent=p)
    finally:
        opts.client.logout()
        _restore(opts)


# ---------------------------------------------------------------------------
# Who may set the policy, and what a new child may carry
# ---------------------------------------------------------------------------

@th.django_unit_test("#9107: a parent's administrators cannot turn the policy on or off")
def test_parent_admins_cannot_set_the_policy(opts):
    _restore(opts)
    p, q = opts.ids["p"], opts.ids["q"]

    def post(pk, payload, what, must_refuse):
        before = _policy(pk)
        resp = opts.client.post(f"/api/group/{pk}", payload)
        if must_refuse:
            assert resp.status_code == 403, \
                f"{what} must be refused with 403, got {resp.status_code}: {opts.client.last_response.body}"
        else:
            assert resp.status_code in (200, 403), \
                f"{what} must be refused or change nothing, got {resp.status_code}: " \
                f"{opts.client.last_response.body}"
        assert _policy(pk) == before, \
            f"SECURITY: {what} changed the stored policy from {before!r} to {_policy(pk)!r}"

    try:
        for actor, pk, value in (("p_mgr", p, False), ("q_mgr", q, True)):
            _login(opts, actor)
            post(pk, {"metadata": {"protected": {KEY: value}}}, f"{actor} merging the policy", True)
            post(pk, {"metadata": {f"protected.{KEY}": value}},
                 f"{actor} writing the policy as a nested dotted key", True)
            post(pk, {f"metadata.protected.{KEY}": value},
                 f"{actor} writing the policy as a top-level dotted key", True)
            post(pk, {"metadata": {"protected": None}}, f"{actor} wiping the protected subtree", True)
        _login(opts, "p_mgr")
        post(p, {"metadata": {"__replace": True, "note": "x"}}, "p_mgr replacing the metadata", True)
        post(p, {"metadata": "plain"}, "p_mgr overwriting the metadata with a string", True)

        _login(opts, "super")
        resp = opts.client.post(f"/api/group/{q}", {"metadata": {"protected": {KEY: True}}})
        assert resp.status_code == 200 and _policy(q) is True, \
            f"a superuser must be able to opt a parent in, got {resp.status_code}, policy {_policy(q)!r}"
        _login(opts, "q_mgr")
        _created(opts, "q_after_opt_in", q, "a manager of a newly opted-in parent", parent=q)
        _login(opts, "p_mgr")
        _refused(opts, "p_into_q", "a manager of another parent creating under the newly opted-in one",
                 parent=q)
    finally:
        opts.client.logout()
        _restore(opts)


@th.django_unit_test("#9107: a new child cannot carry more authority than its creator has")
def test_new_child_cannot_carry_more(opts):
    _restore(opts)
    p = opts.ids["p"]
    try:
        _login(opts, "p_mgr")
        _refused(opts, "with_policy", "a new child carrying the policy itself", parent=p,
                 metadata={"protected": {KEY: True}})
        _refused(opts, "with_dotted_policy", "a new child carrying the policy as a dotted key", parent=p,
                 metadata={f"protected.{KEY}": True})
        _refused(opts, "with_webapp_url", "a new child carrying a sign-in link address", parent=p,
                 metadata={"webapp_base_url": "https://elsewhere.account.test"})
        _, child = _created(opts, "child", p, "a plain child of the opted-in parent", parent=p)
        _refused(opts, "grandchild", "a create under a child of the opted-in parent", parent=child.pk)
    finally:
        opts.client.logout()
        _restore(opts)


# ---------------------------------------------------------------------------
# The decision holds to the write
# ---------------------------------------------------------------------------

@th.django_unit_test("#9107: a policy switched off or a parent deactivated in flight stops the create")
def test_create_is_judged_again_inside_its_save(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import Group
    _restore(opts)
    p = opts.ids["p"]

    def create(name, meanwhile):
        """The create the policy admitted, with `meanwhile()` run after its
        first check and just before its save."""
        name = f"{PREFIX}new_{name}"
        fresh = Group(name=name)
        fresh._subgroup_policy_parent_id = p
        write = fresh.atomic_save

        def write_after_the_other_save():
            meanwhile()
            return write()

        fresh.atomic_save = write_after_the_other_save
        refused = False
        try:
            _rest_save("p_mgr", fresh, {"name": name, "kind": "organization", "parent": p})
        except merrors.PermissionDeniedException:
            refused = True
        return refused, Group.objects.filter(name=name).first()

    try:
        refused, made = create("in_flight_control", lambda: None)
        assert not refused and made is not None and made.parent_id == p, \
            f"this test needs the plain create to work: refused={refused}"

        refused, made = create("policy_off", lambda: _set_metadata(p, {}))
        assert refused and made is None, \
            f"SECURITY: a create was written after the parent's policy was switched off: " \
            f"refused={refused}, created={getattr(made, 'pk', None)}"

        _restore(opts)
        refused, made = create(
            "deactivated", lambda: Group.objects.filter(pk=p).update(is_active=False))
        assert refused and made is None, \
            f"SECURITY: a create was written after the parent was deactivated: " \
            f"refused={refused}, created={getattr(made, 'pk', None)}"
    finally:
        _restore(opts)


@th.django_unit_test("#9107: the policy cannot change between the locked decision and the write")
def test_policy_stays_locked_until_the_write(opts):
    from django.db import DatabaseError, transaction
    from mojo.apps.account.models import Group
    _restore(opts)
    p = opts.ids["p"]

    def switch_off_unless_locked():
        def attempt():
            try:
                with transaction.atomic():
                    Group.objects.select_for_update(nowait=True).filter(pk=p).first()
                    Group.objects.filter(pk=p).update(metadata={})
                return True
            except DatabaseError:
                return False
        return _elsewhere(attempt)

    try:
        name = f"{PREFIX}new_locked"
        fresh = Group(name=name)
        fresh._subgroup_policy_parent_id = p
        judge = fresh._may_manage_tree_position
        seen = {"on_p": 0}

        def judge_then_the_other_request(group):
            answer = judge(group)
            if group is not None and group.pk == p:
                seen["on_p"] += 1
                if seen["on_p"] == 2:
                    # the second answer on P is the one inside save()
                    seen["switched"] = switch_off_unless_locked()
            return answer

        fresh._may_manage_tree_position = judge_then_the_other_request
        _rest_save("p_mgr", fresh, {"name": name, "kind": "organization", "parent": p})
        assert seen["on_p"] == 2, \
            f"the create must be judged before the save and again inside it, P was judged {seen['on_p']} time(s)"
        assert seen["switched"] is False and _policy(p) is True, \
            f"SECURITY: the policy changed between the locked decision and the write: " \
            f"switched={seen['switched']}, policy {_policy(p)!r}"
        assert Group.objects.filter(name=name, parent_id=p).exists(), "the create itself must land"
    finally:
        _restore(opts)


@th.django_unit_test("#9107: the REST entry point binds the create to the parent it admitted")
def test_entry_point_binds_the_create(opts):
    from objict import objict
    from mojo import errors as merrors
    from mojo.apps.account.models import Group
    from mojo.models import rest as mojo_rest
    _restore(opts)
    p = opts.ids["p"]
    switched = {}

    class SwitchOffOnSave(objict):
        """The body of the request. on_rest_save is the first to iterate it,
        after the preflight admitted the create: the policy goes off then."""
        def items(self):
            if not switched:
                switched["done"] = True
                _set_metadata(p, {})
            return super().items()

    def create(name, data_type):
        name = f"{PREFIX}new_{name}"
        request = _rest_request("p_mgr", data_type(
            {"name": name, "kind": "organization", "parent": p}))
        token = mojo_rest.ACTIVE_REQUEST.set(request)
        try:
            Group.on_rest_handle_create(request)
        finally:
            mojo_rest.ACTIVE_REQUEST.reset(token)
        return Group.objects.filter(name=name).first()

    try:
        made = create("entry_control", objict)
        assert made is not None and made.parent_id == p, \
            "this test needs the in-process create to work while the policy holds"

        refusal = None
        try:
            create("entry_switched", SwitchOffOnSave)
        except merrors.PermissionDeniedException as exc:
            refusal = exc
        assert switched.get("done"), "the policy must have been switched off during the save"
        assert refusal is not None and "Creating a group under this parent" in str(refusal.reason), \
            f"SECURITY: a create admitted by a policy switched off before its save was not refused " \
            f"by the save-time check: {getattr(refusal, 'reason', refusal)!r}"
        assert _made("entry_switched") is None, "SECURITY: the refused create left a row behind"
    finally:
        _restore(opts)


# ---------------------------------------------------------------------------
# What must keep working, and who never takes this path
# ---------------------------------------------------------------------------

@th.django_unit_test("#9107: global holders, user-row holders and keys keep today's behaviour")
def test_compatibility(opts):
    from mojo.apps.account.models import ApiKey, Group, User, UserAPIKey
    _restore(opts)
    p, q = opts.ids["p"], opts.ids["q"]
    try:
        _login(opts, "global_mg")
        global_resp, _ = _created(opts, "global_under_q", q, "a global holder creating under a parent "
                                  "with no policy", parent=q)
        _created(opts, "global_top", None, "a global holder creating a top-level group")
        _login(opts, "super")
        _created(opts, "super_under_q", q, "a superuser creating under a parent with no policy", parent=q)
        _login(opts, "row_mgr")
        _created(opts, "row_top", None, "a user-row manage_group holder creating a top-level group")

        _login(opts, "p_mgr")
        policy_resp, made = _created(opts, "policy_shape", p, "a policy create", parent=p)
        assert set(policy_resp.json.data.keys()) == set(global_resp.json.data.keys()), \
            f"a policy create must answer with the same graph as any other create: " \
            f"{sorted(policy_resp.json.data.keys())} vs {sorted(global_resp.json.data.keys())}"
        opts.client.logout()

        _, token = ApiKey.create_for_group(
            group=Group.objects.get(pk=p), name=f"{PREFIX}key_{_uuid.uuid4().hex[:6]}",
            permissions={"manage_group": True})
        _refused(opts, "by_group_key", "a group API key on the opted-in parent",
                 headers={"Authorization": f"apikey {token}"}, parent=p)

        user_key = UserAPIKey.create_for_user(User.objects.get(email=_email("p_mgr")), label=PREFIX)
        resp, made = _create(opts, "by_user_key", headers={"Authorization": f"Bearer {user_key.token}"},
                             parent=p)
        assert resp.status_code == 200 and made is not None and made.parent_id == p, \
            f"the manager's own per-user API key must act as the manager, got {resp.status_code}: " \
            f"{opts.client.last_response.body}"
    finally:
        opts.client.logout()
        _restore(opts)
