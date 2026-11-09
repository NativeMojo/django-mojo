"""RestMeta.LIST_REQUIRES_GROUP — a list with no resolved group (maestro #8937).

A model that opts in refuses a non-platform list caller who names no group
with HTTP 400 "group required", instead of serving the union of every group
the caller can see. Naming a group, platform callers, API keys and the
unflagged control behave exactly as before.

Probe models live in the generated test project's ``testit_support`` app
(bin/create_testproject): ListGroupProbe (group FK, flag on),
ListGroupOwnerProbe (owner + group, flag on) and ListGroupProbeOpen (no flag).
A shared model's RestMeta must not be flipped in the parallel ring, so the
probes declare the flag themselves.

Fixture: groups A, A1 (child of A), B, C and an inactive D. Callers: member M
(GroupMember grants in A and B), owner O (no grants; owns probe rows in A and
B), platform user P (global grant), superuser S, ApiKey K (group A) and ApiKey
K2 (group D, so its tenant is suspended).
"""
import uuid as _uuid

from testit import helpers as th

TESTIT_TIER = "core"

PREFIX = "lrg8937"
PERM = "view_listgroup_probe"
PWORD = "Lrg##probe99"

PROBE = "/api/testit_support/list_group_probe"
OWNER_PROBE = "/api/testit_support/list_group_owner_probe"
OPEN_PROBE = "/api/testit_support/list_group_probe_open"


def _models():
    from testit_support.models import ListGroupProbe, ListGroupOwnerProbe, ListGroupProbeOpen
    return ListGroupProbe, ListGroupOwnerProbe, ListGroupProbeOpen


def _mk_user(tag, perms=None, superuser=False):
    from mojo.apps.account.models import User
    email = f"{PREFIX}_{tag}@example.com"
    user = User.objects.create_user(username=email, email=email, password=PWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.is_superuser = superuser
    user.save()
    if perms:
        user.add_permission(list(perms))
        user.save()
    return user


@th.django_unit_setup()
def setup_list_requires_group(opts):
    from mojo.apps.account.models import User, Group, GroupMember, ApiKey

    Probe, OwnerProbe, OpenProbe = _models()
    # Long-lived DB: clear what this setup creates.
    for model in (Probe, OwnerProbe, OpenProbe):
        model.objects.filter(name__startswith=PREFIX).delete()
    ApiKey.objects.filter(name__startswith=PREFIX).delete()
    GroupMember.objects.filter(group__name__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX, parent__isnull=False).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(username__startswith=f"{PREFIX}_").delete()

    tag = _uuid.uuid4().hex[:6]
    a = Group.objects.create(name=f"{PREFIX}_a_{tag}", kind="organization", is_active=True)
    a1 = Group.objects.create(name=f"{PREFIX}_a1_{tag}", kind="team", parent=a, is_active=True)
    b = Group.objects.create(name=f"{PREFIX}_b_{tag}", kind="organization", is_active=True)
    c = Group.objects.create(name=f"{PREFIX}_c_{tag}", kind="organization", is_active=True)
    d = Group.objects.create(name=f"{PREFIX}_d_{tag}", kind="organization", is_active=False)
    opts.lrg = {"a": a.pk, "a1": a1.pk, "b": b.pk, "c": c.pk, "d": d.pk}

    member = _mk_user("member")
    for group in (a, b):
        gm, _ = GroupMember.objects.get_or_create(user=member, group=group)
        gm.is_active = True
        gm.permissions = {PERM: True}
        gm.save()
    owner = _mk_user("owner")
    _mk_user("platform", perms=[PERM])
    _mk_user("super", superuser=True)

    for model in (Probe, OpenProbe):
        for key, group in (("a", a), ("a1", a1), ("b", b), ("c", c)):
            model.objects.create(name=f"{PREFIX}_{key}", group=group)
    OwnerProbe.objects.create(name=f"{PREFIX}_mine_a", user=owner, group=a)
    OwnerProbe.objects.create(name=f"{PREFIX}_mine_b", user=owner, group=b)
    OwnerProbe.objects.create(name=f"{PREFIX}_theirs_a", user=member, group=a)

    _, opts.lrg_key = ApiKey.create_for_group(
        group=a, name=f"{PREFIX}_k", permissions={PERM: True})
    _, opts.lrg_key_suspended = ApiKey.create_for_group(
        group=d, name=f"{PREFIX}_k2", permissions={PERM: True})


def _login(opts, tag):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="login")
    opts.client.logout()
    opts.client.bearer = None
    email = f"{PREFIX}_{tag}@example.com"
    assert opts.client.login(email, PWORD), \
        f"login failed for {email}: {opts.client.last_response.body}"


def _use_key(opts, token):
    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = token
    opts.client.is_authenticated = True


def _get(opts, path, **params):
    params.setdefault("size", 100)
    return opts.client.get(path, params=params)


def _names(resp):
    data = (resp.json or {}).get("data") or []
    return sorted(row.get("name") for row in data if str(row.get("name", "")).startswith(PREFIX))


def _expect_group_required(resp, label):
    body = resp.json or {}
    assert resp.status_code == 400, \
        f"{label}: expected 400 group required, got {resp.status_code}: {body}"
    assert "group required" in str(body.get("error", "")), \
        f"{label}: expected the 'group required' error, got {body}"
    assert not body.get("data"), f"{label}: a refused list must carry no rows: {body}"
    assert "count" not in body and "stats" not in body, \
        f"{label}: a refused list must carry no count or stats: {body}"
    assert PREFIX not in str(resp.get("text", "") or ""), \
        f"{label}: refused response leaked probe rows"


# ---------------------------------------------------------------------------
# Member, flagged probe
# ---------------------------------------------------------------------------

@th.django_unit_test("1: member, no group, flagged list -> 400 group required")
def test_member_no_group_refused(opts):
    _login(opts, "member")
    _expect_group_required(_get(opts, PROBE), "member list without group")


@th.django_unit_test("2: member with ?group=A lists only A")
def test_member_named_group(opts):
    _login(opts, "member")
    resp = _get(opts, PROBE, group=opts.lrg["a"])
    assert resp.status_code == 200, f"member ?group=A must list, got {resp.status_code}: {resp.json}"
    assert _names(resp) == [f"{PREFIX}_a"], f"?group=A must return exactly A's row, got {_names(resp)}"


@th.django_unit_test("3: member with ?group=C (not a member) gets no C rows")
def test_member_foreign_group(opts):
    _login(opts, "member")
    resp = _get(opts, PROBE, group=opts.lrg["c"])
    assert resp.status_code in (200, 403), \
        f"?group=<not a member> must stay refused or empty, got {resp.status_code}: {resp.json}"
    assert _names(resp) == [], f"SECURITY: naming a foreign group returned rows: {_names(resp)}"


@th.django_unit_test("4: member with ?group=<inactive D> -> 400, same as no group")
# Before the change this answered 200 with no rows (the unresolved `group`
# param stayed on as a filter); either way it is now the same 400 as no group,
# so an inactive id and an unknown id cannot be told apart.
def test_member_inactive_group(opts):
    _login(opts, "member")
    _expect_group_required(_get(opts, PROBE, group=opts.lrg["d"]), "member ?group=<inactive>")


@th.django_unit_test("5-6: platform user and superuser still list every group with no group")
def test_platform_callers_unchanged(opts):
    want = sorted(f"{PREFIX}_{k}" for k in ("a", "a1", "b", "c"))
    for tag in ("platform", "super"):
        _login(opts, tag)
        resp = _get(opts, PROBE)
        assert resp.status_code == 200, f"{tag}: platform list must stay 200, got {resp.status_code}: {resp.json}"
        assert _names(resp) == want, f"{tag}: platform list must see every group, got {_names(resp)}"


@th.django_unit_test("7: member on the unflagged control keeps the union")
def test_unflagged_control(opts):
    _login(opts, "member")
    resp = _get(opts, OPEN_PROBE)
    assert resp.status_code == 200, f"unflagged list must stay 200, got {resp.status_code}: {resp.json}"
    assert _names(resp) == sorted(f"{PREFIX}_{k}" for k in ("a", "a1", "b")), \
        f"unflagged list must keep today's union of A, A1, B, got {_names(resp)}"


@th.django_unit_test("8-10: count, _stats and CSV download without a group -> 400")
def test_derived_surfaces_refused(opts):
    _login(opts, "member")
    _expect_group_required(_get(opts, PROBE, _mode="count"), "_mode=count")
    _expect_group_required(
        _get(opts, PROBE, _mode="count", _stats='{"a": {"name": "%s_a"}}' % PREFIX), "_stats")
    _expect_group_required(_get(opts, PROBE, download_format="csv"), "download_format=csv")


@th.django_unit_test("11: member count with ?group=A counts A only")
def test_count_with_group(opts):
    _login(opts, "member")
    resp = _get(opts, PROBE, _mode="count", group=opts.lrg["a"])
    assert resp.status_code == 200, f"count with group must be 200, got {resp.status_code}: {resp.json}"
    assert resp.json.get("count") == 1, f"count for A must be 1, got {resp.json}"


# ---------------------------------------------------------------------------
# API keys pass through unchanged
# ---------------------------------------------------------------------------

@th.django_unit_test("12: ApiKey with no group param lists its own group, not 400")
def test_key_unchanged(opts):
    _use_key(opts, opts.lrg_key)
    try:
        resp = _get(opts, PROBE)
        assert resp.status_code == 200, f"key list must stay 200, got {resp.status_code}: {resp.json}"
        assert _names(resp) == [f"{PREFIX}_a"], f"key must list exactly its group A, got {_names(resp)}"
    finally:
        opts.client.logout()


@th.django_unit_test("13: ApiKey naming another group is still refused at the dispatcher")
def test_key_foreign_group(opts):
    _use_key(opts, opts.lrg_key)
    try:
        resp = _get(opts, PROBE, group=opts.lrg["b"])
        assert resp.status_code == 403, f"key ?group=B must stay 403, got {resp.status_code}: {resp.json}"
        assert _names(resp) == [], f"SECURITY: key read another group's rows: {_names(resp)}"
    finally:
        opts.client.logout()


@th.django_unit_test("14: ApiKey on a suspended tenant keeps its 403, not 400")
def test_suspended_key(opts):
    _use_key(opts, opts.lrg_key_suspended)
    try:
        resp = _get(opts, PROBE)
        assert resp.status_code == 403, \
            f"suspended key must keep today's 403, got {resp.status_code}: {resp.json}"
        assert _names(resp) == [], f"SECURITY: suspended key read rows: {_names(resp)}"
    finally:
        opts.client.logout()


# ---------------------------------------------------------------------------
# Owner-scoped probe
# ---------------------------------------------------------------------------

@th.django_unit_test("15: owner list with no group -> 400")
def test_owner_no_group(opts):
    _login(opts, "owner")
    _expect_group_required(_get(opts, OWNER_PROBE), "owner list without group")


@th.django_unit_test("16: owner list with ?group=A returns own rows in A")
def test_owner_named_group(opts):
    _login(opts, "owner")
    resp = _get(opts, OWNER_PROBE, group=opts.lrg["a"])
    assert resp.status_code == 200, f"owner ?group=A must list, got {resp.status_code}: {resp.json}"
    assert _names(resp) == [f"{PREFIX}_mine_a"], \
        f"owner ?group=A must return only the caller's A row, got {_names(resp)}"


# ---------------------------------------------------------------------------
# The predicate, in process
# ---------------------------------------------------------------------------

def _probe_class(view_perms, group_scoped=True):
    from mojo.models import MojoModel

    class RestMeta:
        VIEW_PERMS = view_perms
        LIST_REQUIRES_GROUP = True

    attrs = {"RestMeta": RestMeta, "__module__": __name__}
    if group_scoped:
        attrs["group"] = None
    return type("LrgPredicateProbe", (MojoModel,), attrs)


def _user(tag):
    from mojo.apps.account.models import User
    return User.objects.get(username=f"{PREFIX}_{tag}@example.com")


@th.django_unit_test("predicate: authenticated/all/owner/member grants never make a caller platform")
def test_predicate_special_perms(opts):
    from mojo import errors as me
    from testit.helpers import get_mock_request

    platform = _user("platform")
    for perms in (["authenticated"], ["all"], ["owner"], ["member"]):
        cls = _probe_class(perms)
        req = get_mock_request(user=platform)
        assert not cls.rest_is_platform_list_caller(req), \
            f"VIEW_PERMS={perms} must not make a caller platform"
        try:
            cls.rest_check_list_group(req)
        except me.ValueException as err:
            assert "group required" in str(err.reason), f"wrong refusal for {perms}: {err.reason}"
        else:
            raise AssertionError(f"VIEW_PERMS={perms}: a groupless list must be refused")


@th.django_unit_test("predicate: superuser and global grant are platform; key-backed superuser is not")
def test_predicate_identities(opts):
    from testit.helpers import get_mock_request

    cls = _probe_class([PERM])
    assert cls.rest_is_platform_list_caller(get_mock_request(user=_user("super"))), "superuser is platform"
    assert cls.rest_is_platform_list_caller(get_mock_request(user=_user("platform"))), \
        "a global VIEW_PERMS grant is platform"
    assert not cls.rest_is_platform_list_caller(get_mock_request(user=_user("member"))), \
        "member-only grants are never platform"

    keyed = get_mock_request(user=_user("super"))
    keyed.api_key = object()
    assert not cls.rest_is_platform_list_caller(keyed), \
        "a key-backed request carrying a superuser must not be platform"
    tokened = get_mock_request(user=_user("super"))
    tokened.group_token = object()
    assert not cls.rest_is_platform_list_caller(tokened), \
        "a group-token request must not be platform"


@th.django_unit_test("predicate: the flag on a model that is not group-scoped refuses even with a group")
def test_predicate_not_group_scoped(opts):
    from mojo import errors as me
    from mojo.apps.account.models import Group
    from testit.helpers import get_mock_request

    cls = _probe_class([PERM], group_scoped=False)
    req = get_mock_request(user=_user("member"))
    req.group = Group.objects.get(pk=opts.lrg["a"])
    try:
        cls.rest_check_list_group(req)
    except me.ValueException:
        pass
    else:
        raise AssertionError("a flagged model that is not group-scoped must fail closed")


# ---------------------------------------------------------------------------
# Assistant list tools
# ---------------------------------------------------------------------------

@th.django_unit_test("assistant: list tools refuse a flagged model for a member and serve a platform user")
def test_assistant_tools(opts):
    from mojo.apps.assistant.services.tools import models as tools

    params = {"app_name": "testit_support", "model_name": "ListGroupProbe"}
    member, platform = _user("member"), _user("platform")
    Probe = _models()[0]

    refusal = tools._list_group_refusal(Probe, tools._build_request(member), "testit_support.ListGroupProbe")
    assert refusal and "error" in refusal, f"member must be refused, got {refusal}"
    assert tools._list_group_refusal(
        Probe, tools._build_request(platform), "testit_support.ListGroupProbe") is None, \
        "platform user must not be refused"
    assert tools._list_group_refusal(
        _models()[2], tools._build_request(member), "testit_support.ListGroupProbeOpen") is None, \
        "an unflagged model must not be refused"

    for name, fn, extra in (
            ("query_model", tools._tool_query_model, {}),
            ("aggregate_model", tools._tool_aggregate_model, {"aggregations": [{"field": "id", "func": "count"}]}),
            ("export_data", tools._tool_export_data, {"fields": ["id", "name"]})):
        result = fn({**params, **extra}, member)
        assert isinstance(result, dict) and result.get("error"), \
            f"{name}: member must get an error, got {str(result)[:300]}"
        assert PREFIX not in str(result), f"{name}: member refusal leaked rows: {str(result)[:300]}"

    result = tools._tool_query_model({**params, "filters": {"name__startswith": PREFIX}}, platform)
    assert not result.get("error"), f"query_model must serve a platform user, got {result}"
    assert PREFIX in str(result), f"query_model must return probe rows to a platform user: {str(result)[:300]}"
