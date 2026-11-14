"""GET /api/group shows a plain member only what opening the group shows
(maestro #11001).

Opening a group (`GET /api/group/<id>`) caps a member with no view or manage
grant at the `basic` graph. The list used to serialize every row in the
requested graph, so the same member read the group's `metadata` (operator
settings), `auth_domain` and `parent` there. Each list row is now capped the
same way, filters, sorts and aggregates on the hidden fields never reach such
rows, and the CSV export drops the hidden columns. A caller with view or
manage on a group, an admin, an ApiKey and a group token see what they saw
before.
"""
from testit import helpers as th

TESTIT_TIER = "bug"

PREFIX = "glmgc11001_"
PASSWORD = "glmgc11001##Pass"
SECRET = "glmgc11001-threshold"
TIMEZONE = "Asia/Kathmandu"
AUTH_DOMAIN = "glmgc11001-b.example.com"
HIDDEN = ("metadata", "auth_domain", "parent", "timezone", "short_name", "member_count")
GRAPHS = (None, "default", "simple", "list", "detail", "full")


def _user(name):
    from mojo.apps.account.models import User
    user = User(username=f"{PREFIX}{name}", display_name=name,
                email=f"{PREFIX}{name}@example.com")
    user.is_email_verified = True
    user.save()
    user.save_password(PASSWORD)
    return user


@th.django_unit_setup()
def setup_group_list_member_graph_cap(opts):
    from mojo.apps.account.models import ApiKey, Group, User
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    from mojo.apps.account.services import group_token

    GeoLocatedIP.objects.get_or_create(
        ip_address="127.0.0.1", defaults={"subnet": "127.0.0.0/8"})
    ApiKey.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(username__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX, parent__isnull=False).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()

    parent = Group.objects.create(name=f"{PREFIX}parent", kind="organization")
    group_a = Group.objects.create(
        name=f"{PREFIX}a", kind="organization", parent=parent,
        metadata={"redemption_policy": {"min_trust_level": SECRET}, "timezone": TIMEZONE})
    group_b = Group.objects.create(
        name=f"{PREFIX}b", kind="organization", parent=parent, auth_domain=AUTH_DOMAIN,
        metadata={"redemption_policy": {"min_trust_level": SECRET}, "timezone": TIMEZONE})

    plain = _user("plain")
    group_a.add_member(plain)
    group_b.add_member(plain)

    mixed = _user("mixed")
    group_a.add_member(mixed).add_permission("manage_group")
    group_b.add_member(mixed)

    admin = _user("admin")
    admin.add_permission("view_groups")
    admin.save()

    _, opts.key_token = ApiKey.create_for_group(
        group=group_a, name=f"{PREFIX}key", permissions={"groups": True})
    opts.group_token = group_token.mint(plain, group_a)
    opts.a_id, opts.b_id, opts.parent_id = group_a.pk, group_b.pk, parent.pk


def _login(opts, name):
    assert opts.client.login(f"{PREFIX}{name}", PASSWORD), f"login failed for {name}"


def _list(opts, **params):
    params.setdefault("size", 100)
    params["id__in"] = f"{opts.a_id},{opts.b_id}"
    return opts.client.get("/api/group", params=params)


def _rows(resp, label):
    assert resp.status_code == 200, f"{label}: expected 200, got {resp.status_code}: {resp.response!r}"
    return {row["id"]: row for row in resp.response["data"]}


@th.django_unit_test()
def test_plain_member_list_rows_carry_no_hidden_fields_in_any_graph(opts):
    _login(opts, "plain")
    for graph in GRAPHS:
        params = {} if graph is None else {"graph": graph}
        rows = _rows(_list(opts, **params), f"graph={graph}")
        assert set(rows) == {opts.a_id, opts.b_id}, \
            f"graph={graph}: the plain member must still list both groups, got {sorted(rows)}"
        for gid, row in rows.items():
            leaked = [key for key in HIDDEN if key in row]
            assert not leaked, \
                f"graph={graph}: plain member's list row {gid} carries hidden keys {leaked}: {row!r}"
            assert SECRET not in str(row), \
                f"graph={graph}: group settings leaked into list row {gid}: {row!r}"


@th.django_unit_test()
def test_unknown_graph_is_still_refused(opts):
    _login(opts, "plain")
    resp = _list(opts, graph="glmgc11001_bogus")
    assert resp.status_code == 400, \
        f"an unknown graph name must stay a 400, got {resp.status_code}: {resp.response!r}"


@th.django_unit_test()
def test_list_rows_match_detail_rows_for_the_same_user(opts):
    for name in ("plain", "mixed"):
        _login(opts, name)
        for graph in (None, "default"):
            params = {} if graph is None else {"graph": graph}
            rows = _rows(_list(opts, **params), f"{name} graph={graph}")
            for gid in (opts.a_id, opts.b_id):
                detail = opts.client.get(f"/api/group/{gid}", params=params)
                assert detail.status_code == 200, \
                    f"{name}: detail {gid} expected 200, got {detail.status_code}: {detail.response!r}"
                assert set(rows[gid]) == set(detail.response["data"]), (
                    f"{name} graph={graph}: list row {gid} keys {sorted(rows[gid])} differ from "
                    f"detail keys {sorted(detail.response['data'])}")


@th.django_unit_test()
def test_mixed_caller_gets_full_row_where_it_manages_and_basic_where_plain(opts):
    _login(opts, "mixed")
    rows = _rows(_list(opts, graph="default"), "mixed")
    assert rows[opts.a_id].get("metadata", {}).get("timezone") == TIMEZONE, \
        f"manage_group on A must keep A's full row, got {rows[opts.a_id]!r}"
    leaked = [key for key in HIDDEN if key in rows[opts.b_id]]
    assert not leaked, f"plain on B must cap B at basic, got hidden keys {leaked}: {rows[opts.b_id]!r}"


@th.django_unit_test()
def test_view_groups_admin_keeps_full_rows(opts):
    _login(opts, "admin")
    rows = _rows(_list(opts, graph="default"), "admin")
    assert set(rows) == {opts.a_id, opts.b_id}, f"admin must list both groups, got {sorted(rows)}"
    assert rows[opts.b_id].get("auth_domain") == AUTH_DOMAIN, \
        f"a view_groups admin must still see auth_domain, got {rows[opts.b_id]!r}"
    assert rows[opts.b_id].get("parent", {}).get("id") == opts.parent_id, \
        f"a view_groups admin must still see the parent, got {rows[opts.b_id]!r}"
    assert rows[opts.a_id].get("metadata", {}).get("timezone") == TIMEZONE, \
        f"a view_groups admin must still see metadata, got {rows[opts.a_id]!r}"


@th.django_unit_test()
def test_hidden_field_filters_sorts_and_aggregates_never_reach_plain_rows(opts):
    import json
    _login(opts, "plain")
    rows = _rows(_list(opts, **{"metadata__redemption_policy__min_trust_level": SECRET}), "filter")
    assert not rows, f"a metadata filter must not match plain-member rows, got {sorted(rows)}"
    rows = _rows(_list(opts, auth_domain=AUTH_DOMAIN), "auth_domain filter")
    assert not rows, f"an auth_domain filter must not match plain-member rows, got {sorted(rows)}"
    rows = _rows(_list(opts, sort="metadata"), "sort")
    assert not rows, f"sort=metadata must not order plain-member rows, got {sorted(rows)}"

    rows = _rows(_list(opts, parent=opts.parent_id), "parent filter")
    assert not rows, f"a parent filter must not match plain-member rows, got {sorted(rows)}"
    rows = _rows(_list(opts, sort="parent"), "parent sort")
    assert not rows, f"sort=parent must not order plain-member rows, got {sorted(rows)}"
    resp = _list(opts, _mode="count", parent=opts.parent_id)
    assert resp.status_code == 200 and resp.response.get("count") == 0, \
        f"a parent filter must not count plain-member rows, got {resp.response!r}"
    for key, value in (("parent_id", None), ("parent_id__in", None), ("parent__id", None), ("parent_id__isnull", "false")):
        rows = _rows(_list(opts, **{key: value or opts.parent_id}), key)
        assert not rows, f"?{key}= must not match plain-member rows, got {sorted(rows)}"
    resp = _list(opts, _mode="distinct", _field="parent__id")
    assert str(opts.parent_id) not in json.dumps(resp.response.get("data", [])), \
        f"distinct on parent__id must not show a plain-member group's parent, got {resp.response!r}"
    parent_stats = {"hit": {"parent_id": opts.parent_id}, "hit2": {"parent": opts.parent_id}}
    resp = _list(opts, _mode="count", _stats=json.dumps(parent_stats))
    assert resp.status_code == 200, f"parent _stats: expected 200, got {resp.status_code}: {resp.response!r}"
    for name in parent_stats:
        assert resp.response.get("stats", {}).get(name) in (0, None), \
            f"a parent _stats bundle ({name}) must not count plain-member rows, got {resp.response!r}"

    stats = {"hit": {"metadata__redemption_policy__min_trust_level": SECRET}}
    resp = _list(opts, _mode="count", _stats=json.dumps(stats))
    assert resp.status_code == 200, f"count: expected 200, got {resp.status_code}: {resp.response!r}"
    assert resp.response.get("stats", {}).get("hit") in (0, None), \
        f"a metadata _stats bundle must not count plain-member rows, got {resp.response!r}"

    resp = _list(opts, _mode="distinct", _field="auth_domain")
    assert AUTH_DOMAIN not in json.dumps(resp.response), \
        f"distinct on auth_domain must not show a plain-member group's domain, got {resp.response!r}"


@th.django_unit_test()
def test_mixed_caller_parent_query_reaches_only_full_view_rows(opts):
    _login(opts, "mixed")
    rows = _rows(_list(opts, parent=opts.parent_id), "mixed parent filter")
    assert set(rows) == {opts.a_id}, \
        f"a parent filter must match A (manage_group) and never B (plain), got {sorted(rows)}"
    rows = _rows(_list(opts, parent_id=opts.parent_id), "mixed parent_id filter")
    assert set(rows) == {opts.a_id}, \
        f"a parent_id filter must match A (manage_group) and never B (plain), got {sorted(rows)}"
    rows = _rows(_list(opts, sort="parent"), "mixed parent sort")
    assert set(rows) == {opts.a_id}, \
        f"sort=parent must order A (manage_group) and never B (plain), got {sorted(rows)}"


@th.django_unit_test()
def test_plain_member_csv_has_no_hidden_columns(opts):
    _login(opts, "plain")
    resp = _list(opts, download_format="csv")
    assert resp.status_code == 200, f"csv: expected 200, got {resp.status_code}"
    text = resp.response if isinstance(resp.response, str) else str(resp.response)
    assert f"{PREFIX}b" in text, f"the export must still list the plain member's groups, got {text!r}"
    assert TIMEZONE not in text, f"the export leaked metadata.timezone: {text!r}"
    assert f"{PREFIX}parent" not in text, f"the export leaked the parent's name: {text!r}"
    header = text.splitlines()[0].lower()
    for column in ("timezone", "parent"):
        assert column not in header, f"the export header still names {column}: {header!r}"


@th.django_unit_test()
def test_api_key_and_group_token_lists_are_unchanged(opts):
    resp = opts.client.get("/api/group", params={"graph": "default", "size": 100},
                           headers={"Authorization": f"apikey {opts.key_token}"})
    rows = _rows(resp, "apikey")
    assert opts.a_id in rows, f"the ApiKey must still list its own group, got {sorted(rows)}"
    assert rows[opts.a_id].get("metadata", {}).get("timezone") == TIMEZONE, \
        f"the ApiKey must still get its group's full row, got {rows[opts.a_id]!r}"

    resp = opts.client.get("/api/group", params={"size": 100},
                           headers={"Authorization": f"grouptoken {opts.group_token}"})
    rows = _rows(resp, "grouptoken")
    assert not rows, f"a group token must still list no groups, got {sorted(rows)}"
