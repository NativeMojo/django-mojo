"""#8680: a list filter, count, `_stats`, search, sort or `_agg` path may walk
into a related model only when the caller could list that model in full, the
model's author declared the walk, or it is the model's own group-scoping path.

Before the fix the walk was guarded only by deny-lists, so the list `count`
answered questions about related rows the caller cannot see:

    A  a plain member reads outsiders' contact-form messages to their group
       (/api/group?public_messages__email__startswith=...)
    B  a member-level view_fileman holder recovers uploaders' email and phone
       (/api/fileman/file?user__email__startswith=...)
    C  a plain member reaches staff-only ticket notes through a hidden link
       (/api/group?avatar__+__note__startswith=...)

T1-T4, T6, T7 and the sort test fail on the pre-fix code. T5 and the allowed
rows of T8 pass before and after.
"""
import json

from testit import helpers as th

TESTIT_TIER = "core"

P = "rfw8680_"
PW = "rfw8680##mojo99x"
MEMBER = P + "member"        # plain member of the group, no permissions
BRAND = P + "brand"          # member-level view_fileman only
VIEWER = P + "viewer"        # member-level view_members (allowed walks)
GLOBAL = P + "global"        # system support + view_groups
GROUPS = P + "groups"        # system view_groups only (no support)
TARGET = P + "target"        # member whose file and phone are probed
NOTE_TEXT = P + "CONFIDENTIAL: avatar flagged"
PHONE = "+15550104477"


def _make_user(User, uname):
    user = User(username=uname, email=uname + "@test.com")
    user.save()
    user.is_active = True
    user.is_email_verified = True
    user.save_password(PW)
    user.save()
    return user


@th.django_unit_setup()
@th.requires_app("mojo.apps.fileman")
@th.requires_app("mojo.apps.incident")
def setup_rest_filter_walks(opts):
    from mojo.apps.account.models import User, Group, GroupMember, PublicMessage
    from mojo.apps.fileman.models import FileManager, File
    from mojo.apps.incident.models import Ticket, TicketNote

    TicketNote.objects.filter(note=NOTE_TEXT).delete()
    Ticket.objects.filter(title__startswith=P).delete()
    PublicMessage.objects.filter(email__startswith=P).delete()
    File.objects.filter(filename__startswith=P).delete()
    FileManager.objects.filter(name__startswith=P).delete()
    User.objects.filter(username__startswith=P).delete()
    Group.objects.filter(name__startswith=P).delete()

    group = Group.objects.create(name=P + "brand", kind="organization")
    member, brand, viewer, glob, groups_only, target = (
        _make_user(User, name) for name in (MEMBER, BRAND, VIEWER, GLOBAL, GROUPS, TARGET))
    User.objects.filter(pk=target.pk).update(phone_number=PHONE)
    glob.add_permission(["support", "view_groups"])
    groups_only.add_permission("view_groups")
    GroupMember(user=member, group=group).save()
    brand_member = GroupMember(user=brand, group=group)
    brand_member.save()
    brand_member.add_permission("view_fileman")
    viewer_member = GroupMember(user=viewer, group=group)
    viewer_member.save()
    viewer_member.add_permission("view_members")
    GroupMember(user=target, group=group).save()

    manager = FileManager.objects.create(
        name=P + "fm", backend_type="file", backend_url=f"filesystem:///tmp/{P}files",
        is_active=True, group=group)
    upload = File.objects.create(
        filename=P + "upload.pdf", user=target, group=group, file_manager=manager,
        upload_status=File.COMPLETED, is_active=True, content_type="application/pdf")
    Group.objects.filter(pk=group.pk).update(avatar=upload)
    ticket = Ticket.objects.create(title=P + "ticket", group=group, category="security")
    TicketNote.objects.create(parent=ticket, group=group, note=NOTE_TEXT, media=upload)
    for index in (1, 2):
        PublicMessage.objects.create(
            group=group, kind="contact", name="Jane", email=f"{P}jane{index}@example.org",
            subject="s", message="card 4242", ip_address="203.0.113.7")

    opts.group_id = group.pk
    opts.member_id = member.pk
    opts.target_username = target.username


def _login(opts, uname):
    opts.client.logout()
    assert opts.client.login(username=uname, password=PW), f"{uname} could not sign in"


def _count(opts, path, params):
    resp = opts.client.get(path, params=params)
    assert resp.status_code == 200, f"GET {path} {params} failed: {resp.status_code} {resp.response}"
    return resp.response.count


def _group_count(opts, extra):
    return _count(opts, "/api/group", dict({"name__startswith": P}, **extra))


@th.django_unit_test("T1 a plain member's group count ignores public_messages walks")
def test_member_public_messages_walk_dropped(opts):
    _login(opts, MEMBER)
    base = _group_count(opts, {})
    assert base == 1, f"fixture: the member must see their one group, got {base}"
    probes = [
        ("public_messages__email__startswith", P + "jane", P + "zzz"),
        ("public_messages__ip_address", "203.0.113.7", "198.51.100.1"),
        ("public_messages__message__contains", "4242", "9999"),
    ]
    for key, match, miss in probes:
        hit = _group_count(opts, {key: match})
        none = _group_count(opts, {key: miss})
        assert hit == base and none == base, (
            f"{key} must not change a plain member's count: base {base}, "
            f"match {hit}, non-match {none} (the count is reading PublicMessage)")


@th.django_unit_test("T1 _mode=count and _stats ignore public_messages walks for a plain member")
def test_member_public_messages_walk_dropped_count_and_stats(opts):
    _login(opts, MEMBER)
    key = "public_messages__email__startswith"
    resp = opts.client.get("/api/group", params={
        "name__startswith": P, key: P + "zzz", "_mode": "count"})
    assert resp.status_code == 200, f"_mode=count failed: {resp.response}"
    assert resp.response["count"] == 1, (
        f"_mode=count must ignore {key}, got {resp.response['count']}")
    stats = json.dumps({"hit": {key: P + "jane"}, "miss": {key: P + "zzz"}})
    resp = opts.client.get("/api/group", params={
        "name__startswith": P, "_mode": "count", "_stats": stats})
    assert resp.status_code == 200, f"_stats failed: {resp.response}"
    got = resp.response.get("stats") or {}
    assert got.get("hit") == got.get("miss") == 1, (
        f"_stats bundles must both equal the base count when {key} is dropped, got {got}")


@th.django_unit_test("T2 member-level view_fileman cannot read uploaders' email or phone by count")
def test_brand_admin_file_user_walk_dropped(opts):
    _login(opts, BRAND)
    base = _count(opts, "/api/fileman/file", {"group": opts.group_id, "filename__startswith": P})
    assert base == 1, f"fixture: the brand admin must see the one upload, got {base}"
    probes = [
        ("user__email__startswith", TARGET, "zzz"),
        ("user__phone_number__startswith", PHONE[:8], "+1555099"),
    ]
    for key, match, miss in probes:
        params = {"group": opts.group_id, "filename__startswith": P}
        hit = _count(opts, "/api/fileman/file", dict(params, **{key: match}))
        none = _count(opts, "/api/fileman/file", dict(params, **{key: miss}))
        assert hit == base and none == base, (
            f"{key} must not change the brand admin's count: base {base}, "
            f"match {hit}, non-match {none}")
    resp = opts.client.get("/api/fileman/file", params={"group": opts.group_id, "filename__startswith": P})
    row = (resp.response.get("data") or [{}])[0]
    served = row.get("user") if isinstance(row.get("user"), dict) else {}
    assert "email" not in served and "phone_number" not in served, (
        f"the served File user graph must not show email or phone, got {sorted(served)}")


@th.django_unit_test("T3 a hidden '+' link mid-path is dropped")
def test_hidden_link_mid_path_dropped(opts):
    _login(opts, MEMBER)
    base = _group_count(opts, {})
    for key, match, miss in (
            ("avatar__+__note__startswith", P + "CONFIDENTIAL: a", P + "CONFIDENTIAL: b"),
            ("avatar__+__isnull", "false", "true")):
        hit = _group_count(opts, {key: match})
        none = _group_count(opts, {key: miss})
        assert hit == base and none == base, (
            f"{key} must be dropped: base {base}, match {hit}, non-match {none}")


@th.django_unit_test("T4 _agg distinct on a reverse relation is refused for a plain member")
def test_member_reverse_agg_refused(opts):
    _login(opts, MEMBER)
    resp = opts.client.get("/api/group", params={
        "name__startswith": P, "_mode": "distinct", "_field": "public_messages__id"})
    assert resp.status_code == 400, (
        f"_field=public_messages__id must be 400 for a plain member, got "
        f"{resp.status_code} {resp.response}")


@th.django_unit_test("sort by a reverse relation is ignored; sort by a forward FK works")
def test_sort_by_relation(opts):
    _login(opts, MEMBER)
    resp = opts.client.get("/api/group", params={"name__startswith": P, "sort": "public_messages"})
    assert resp.status_code == 200, f"sort request failed: {resp.response}"
    ids = [row.get("id") for row in resp.response.get("data") or []]
    assert len(ids) == len(set(ids)) == 1, (
        f"sort=public_messages must not join the messages (rows duplicated), got ids {ids}")
    _login(opts, BRAND)
    resp = opts.client.get("/api/fileman/file", params={
        "group": opts.group_id, "filename__startswith": P, "sort": "-user"})
    assert resp.status_code == 200, f"sort by a forward FK failed: {resp.response}"
    assert resp.response.count == 1, f"sort=-user must keep the one upload, got {resp.response.count}"


@th.django_unit_test("T5 allowed walks keep working")
def test_allowed_walks_kept(opts):
    _login(opts, VIEWER)
    path = "/api/group/member"
    base = {"group": opts.group_id}
    assert _count(opts, path, dict(base, user__email=TARGET + "@test.com")) == 1, \
        "a view_members viewer must still filter members by user email"
    assert _count(opts, path, dict(base, user__email="nobody@test.com")) == 0, \
        "the user email filter must still apply"
    assert _count(opts, path, dict(base, search=opts.target_username)) == 1, \
        "a view_members viewer must still search members by username"
    assert _count(opts, path, dict(base, group__name__icontains="zzz-none")) == 0, \
        "the group name filter must still apply"
    _login(opts, GLOBAL)
    hit = _group_count(opts, {"public_messages__email__startswith": P + "jane1"})
    none = _group_count(opts, {"public_messages__email__startswith": P + "zzz"})
    assert (hit, none) == (1, 0), (
        f"a support + view_groups holder may still filter groups by message email, "
        f"got match {hit}, non-match {none}")


@th.django_unit_test("T6 a view_members viewer cannot filter members by phone")
def test_viewer_member_phone_walk_dropped(opts):
    _login(opts, VIEWER)
    path = "/api/group/member"
    base = _count(opts, path, {"group": opts.group_id})
    hit = _count(opts, path, {"group": opts.group_id, "user__phone_number__startswith": PHONE[:8]})
    none = _count(opts, path, {"group": opts.group_id, "user__phone_number__startswith": "+1555099"})
    assert hit == base and none == base, (
        f"user__phone_number is not a declared walk: base {base}, match {hit}, non-match {none}")


@th.django_unit_test("T7 the Assistant refuses a denied walk and accepts a declared one")
@th.requires_app("mojo.apps.assistant")
def test_assistant_walk_rule(opts):
    from mojo.apps.account.models import User
    from mojo.apps.assistant.services.tools.models import _tool_query_model

    # view_groups may query Group and GroupMember, but cannot list
    # PublicMessage or every User.
    groups_only = User.objects.get(username=GROUPS)
    result = _tool_query_model({
        "app_name": "account", "model_name": "Group",
        "filters": {"name__startswith": P},
    }, groups_only)
    assert "error" not in result, f"fixture: view_groups must query Group: {result.get('error')}"
    result = _tool_query_model({
        "app_name": "account", "model_name": "Group",
        "filters": {"name__startswith": P, "public_messages__email__startswith": P + "jane"},
    }, groups_only)
    assert "error" in result, f"query_model must refuse public_messages__email, got {result}"

    result = _tool_query_model({
        "app_name": "account", "model_name": "GroupMember",
        "filters": {"group": opts.group_id, "user__email": TARGET + "@test.com"},
    }, groups_only)
    assert "error" not in result, f"a declared walk must be accepted: {result.get('error')}"
    assert len(result.get("results") or []) == 1, \
        f"the declared user__email filter must still apply, got {result.get('results')}"


@th.django_unit_test("T9 a not / not_in inside the key cannot disguise a walk")
def test_disguised_exclusion_walk_dropped(opts):
    _login(opts, BRAND)
    params = {"group": opts.group_id, "filename__startswith": P}
    base = _count(opts, "/api/fileman/file", params)
    assert base == 1, f"fixture: the brand admin must see the one upload, got {base}"
    probes = [
        ("user__not__email__startswith__not", TARGET, "zzz"),
        ("user__not__phone_number__startswith__not", PHONE[:8], "+1555099"),
        ("user__not__email__startswith", TARGET, "zzz"),
        ("user__not_in__email__startswith__not_in", TARGET, "zzz"),
    ]
    for key, match, miss in probes:
        hit = _count(opts, "/api/fileman/file", dict(params, **{key: match}))
        none = _count(opts, "/api/fileman/file", dict(params, **{key: miss}))
        assert hit == base and none == base, (
            f"{key} must not change the brand admin's count: base {base}, "
            f"match {hit}, non-match {none}")
    key = "user__not__email__startswith__not"
    stats = json.dumps({"hit": {key: TARGET}, "miss": {key: "zzz"}})
    resp = opts.client.get("/api/fileman/file", params=dict(params, _mode="count", _stats=stats))
    assert resp.status_code == 200, f"_stats failed: {resp.response}"
    got = resp.response.get("stats") or {}
    assert got.get("hit") == got.get("miss") == base, (
        f"_stats bundles must both equal the base count when {key} is dropped, got {got}")

    # Ordinary exclusions keep working: a trailing __not / __not_in only.
    assert _count(opts, "/api/fileman/file", dict(params, filename__not=P + "upload.pdf")) == 0, \
        "filename__not must still exclude the one upload"
    assert _count(opts, "/api/fileman/file", dict(params, filename__not=P + "other.pdf")) == 1, \
        "filename__not must keep a non-matching upload"
    target_id = _target_id()
    assert _count(opts, "/api/fileman/file", dict(params, user__not=target_id)) == 0, \
        "user__not (the local column) must still exclude the uploader's file"
    assert _count(opts, "/api/fileman/file", dict(params, user__not_in=f"{target_id},0")) == 0, \
        "user__not_in (the local column) must still exclude the uploader's file"

    _login(opts, MEMBER)
    group_base = _group_count(opts, {})
    for key, match, miss in (
            ("avatar__not__+__note__startswith__not", P + "CONFIDENTIAL: a", P + "CONFIDENTIAL: b"),
            ("public_messages__not__email__startswith__not", P + "jane", P + "zzz")):
        hit = _group_count(opts, {key: match})
        none = _group_count(opts, {key: miss})
        assert hit == group_base and none == group_base, (
            f"{key} must be dropped: base {group_base}, match {hit}, non-match {none}")


@th.django_unit_test("T9 the parser executes exactly the key the guards judged")
def test_disguised_exclusion_parser(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.fileman.models import File
    from testit.helpers import get_mock_request

    brand = get_mock_request(user=User.objects.get(username=BRAND))
    root = User.objects.get(username=GLOBAL)
    root.is_superuser = True  # in memory only: passes every hop the walk allows
    superuser = get_mock_request(user=root)
    for request, model, key in (
            (brand, File, "user__not__email__startswith__not"),
            (brand, File, "user__not_in__email__startswith__not_in"),
            (superuser, File, "user__not__auth_key__startswith__not"),
            (superuser, File, "user__not__password__startswith"),
            (superuser, Group, "avatar__not__+__note__startswith__not")):
        filters, excludes = model.build_rest_filters(request, {key: P})
        assert not filters and not excludes, (
            f"{key} must be dropped before the parser rewrites it, "
            f"got filters {filters}, excludes {excludes}")
    filters, excludes = File.build_rest_filters(
        brand, {"filename__not": "a", "user__not": "1", "user__not_in": "1,2"})
    assert filters == {} and excludes.get("filename") == "a" and excludes.get("user") == "1" \
            and list(excludes.get("user__in") or []) == ["1", "2"], (
        f"a trailing __not / __not_in must still become an exclusion, got {excludes}")


def _target_id():
    from mojo.apps.account.models import User
    return User.objects.get(username=TARGET).pk


@th.django_unit_test("T8 walk_verdict matrix")
def test_walk_verdict_matrix(opts):
    from mojo.apps.account.models import User, Group, GroupMember
    from mojo.apps.fileman.models import File
    from mojo.apps.dnsman.models import Certificate
    from mojo.models.rest import walk_verdict
    from testit.helpers import get_mock_request

    member = User.objects.get(pk=opts.member_id)
    glob = User.objects.get(username=GLOBAL)
    plain = get_mock_request(user=member)
    support = get_mock_request(user=glob)

    rows = [
        # (model, key, request, expected)
        (File, "user", plain, None),
        (File, "user_id", plain, None),
        (File, "user__id", plain, None),
        (File, "user__in", plain, None),
        (File, "user__isnull", plain, None),
        (File, "user__email__startswith", plain, "denied"),
        (Group, "public_messages__email", plain, "denied"),
        (Group, "public_messages__id", plain, "denied"),
        (Group, "public_messages__isnull", plain, "denied"),
        (Group, "public_messages__email", support, None),
        (Group, "+__note", support, "hidden"),
        (Group, "avatar__+__note", support, "hidden"),
        (Certificate, "domain__group", plain, None),
        (Certificate, "domain__group__in", plain, None),
        (GroupMember, "user__email__icontains", plain, None),
        (GroupMember, "group__name__icontains", plain, None),
        (GroupMember, "user__phone_number", plain, "denied"),
        (File, "user__email", None, None),
        (File, "user__not", plain, None),
        (File, "user__not_in", plain, None),
        (File, "filename__not", plain, None),
        (File, "user__email__startswith__not", plain, "denied"),
        (File, "user__not__email__startswith__not", plain, "denied"),
        (File, "user__not_in__email__startswith__not_in", plain, "denied"),
        (File, "user__not__id", plain, "denied"),
        (Group, "public_messages__not__email", support, "denied"),
        (Group, "avatar__not__+__note", support, "denied"),
    ]
    for model, key, request, expected in rows:
        got = walk_verdict(model, key, request)
        assert got == expected, (
            f"walk_verdict({model.__name__}, {key!r}, "
            f"{'no request' if request is None else request.user.username}) = {got!r}, "
            f"expected {expected!r}")

    keyed = get_mock_request(user=glob)
    keyed.api_key = _AllPermsKey()
    assert walk_verdict(Group, "public_messages__email", keyed) == "denied", \
        "an ApiKey without ALLOW_API_KEY_GLOBAL on PublicMessage must be denied the hop"
    tokened = get_mock_request(user=glob)
    tokened.group_token = object()
    assert walk_verdict(Group, "public_messages__email", tokened) == "denied", \
        "a group token must be denied every undeclared hop"


class _AllPermsKey:
    def has_permission(self, perms):
        return True
