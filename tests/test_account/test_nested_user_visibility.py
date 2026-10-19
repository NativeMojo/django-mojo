"""
#8648: a nested User shows its account privileges and sign-in settings only
to a viewer who could GET /api/user/<pk> for that user.

Before the fix GroupMember's `default` graph nested User `default` for every
`view_members` holder, so a plain member read other members' `permissions`,
`is_superuser`, `requires_mfa`, `requires_password_change`, `has_password`,
`has_passkey` and `metadata`. Nested graphs carried no permission (#2102).

The rule now: the user themselves, a holder of the global `view_users`,
`manage_users` or `users`, and a superuser get the nested User graph the
parent names. Everyone else gets that same graph minus User's
NESTED_PRIVATE_FIELDS, whatever graph the parent names. Every other field,
email and names included, is unchanged. Filters and search that cross into
User's NESTED_PRIVATE_LOOKUPS follow the same rule.
"""
import uuid as _uuid

from testit import helpers as th


PREFIX = "nuv8648_"
PASSWORD = "nuv8648_pw_99"
GROUP_NAME = f"{PREFIX}group"

TARGET = f"{PREFIX}target@test.com"        # the member being looked at
VIEWER = f"{PREFIX}viewer@test.com"        # member-level view_members only
ACCOUNT = f"{PREFIX}account@test.com"      # view_members + GLOBAL view_users
MANAGER = f"{PREFIX}manager@test.com"      # member-level manage_group only
MEMBER_VU = f"{PREFIX}member_vu@test.com"  # member-level view_members + view_users
SUPER = f"{PREFIX}super@test.com"          # superuser, not a member
DEVICES = f"{PREFIX}devices@test.com"      # GLOBAL view_devices only
INVITEE = f"{PREFIX}invitee@test.com"      # invited by MANAGER

PRIVATE_KEYS = {"permissions", "is_superuser", "requires_mfa",
                "requires_password_change", "has_password", "has_passkey", "metadata"}


def _login(opts, email):
    from mojo.decorators.limits import clear_rate_limits
    opts.client.logout()
    clear_rate_limits(ip="127.0.0.1", key="login")
    assert opts.client.login(email, PASSWORD), \
        f"login failed for {email}: {opts.client.last_response.body}"


def _make_user(email, permissions=None, **fields):
    from mojo.apps.account.models import User
    user = User.objects.create_user(username=email, email=email, password=PASSWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.permissions = dict(permissions or {})
    for key, value in fields.items():
        setattr(user, key, value)
    user.save()
    return user


def _join(user, group, permissions=None):
    from mojo.apps.account.models import GroupMember
    member = GroupMember(user=user, group=group)
    member.save()
    if permissions:
        GroupMember.objects.filter(pk=member.pk).update(
            permissions={p: True for p in permissions})
    return member


def _list_members(opts, extra=""):
    resp = opts.client.get(f"/api/group/member?group={opts.group_id}&size=100{extra}")
    assert resp.status_code == 200, \
        f"member list must succeed, got {resp.status_code}: {opts.client.last_response.body}"
    return {row["user"]["id"]: row["user"] for row in resp.response.data}


def _graph_keys(user_id, graph):
    """The keys User `graph` renders, with no request bound (unchanged output)."""
    from mojo.apps.account.models import User
    return set(User.objects.get(pk=user_id).to_dict(graph=graph).keys())


def _assert_stripped(user_dict, who, graph="default"):
    keys = set(user_dict.keys())
    expected = _graph_keys(user_dict["id"], graph) - PRIVATE_KEYS
    leaked = keys & PRIVATE_KEYS
    assert not leaked, f"{who}: the nested user must not carry {sorted(leaked)}, got keys {sorted(keys)}"
    assert keys == expected, \
        f"{who}: the nested user must be User {graph} minus the private keys; " \
        f"missing {sorted(expected - keys)}, extra {sorted(keys - expected)}"


def _assert_full(user_dict, who, graph="default"):
    keys = set(user_dict.keys())
    expected = _graph_keys(user_dict["id"], graph)
    assert keys == expected, \
        f"{who}: the full nested user must be User {graph} unchanged; " \
        f"missing {sorted(expected - keys)}, extra {sorted(keys - expected)}"
    assert PRIVATE_KEYS & expected, f"{who}: User {graph} must carry private keys for this test to mean anything"


@th.django_unit_setup()
def setup_nested_user_visibility(opts):
    from mojo.apps.account.models import User, Group, ApiKey
    from mojo.apps.account.models.push.device import RegisteredDevice
    from mojo.apps.docit.models import Book, Page

    User.objects.filter(email__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    ApiKey.objects.filter(name__startswith=PREFIX).delete()

    group = Group.objects.create(name=GROUP_NAME, kind="organization")
    opts.group_id = group.pk

    target = _make_user(
        TARGET, permissions={"view_logs": True}, metadata={"note": "private"},
        phone_number="+15555550148", requires_password_change=True)
    # requires_mfa is the filter-oracle probe: only TARGET has it set.
    User.objects.filter(pk=target.pk).update(requires_mfa=True)
    opts.target_id = target.pk
    opts.target_member_id = _join(target, group).pk

    viewer = _make_user(VIEWER)
    opts.viewer_id = viewer.pk
    _join(viewer, group, ["view_members"])

    account = _make_user(ACCOUNT, permissions={"view_users": True})
    opts.account_id = account.pk
    _join(account, group, ["view_members"])

    manager = _make_user(MANAGER)
    opts.manager_id = manager.pk
    _join(manager, group, ["manage_group"])

    member_vu = _make_user(MEMBER_VU)
    _join(member_vu, group, ["view_members", "view_users"])

    _make_user(SUPER, is_superuser=True)

    devices = _make_user(DEVICES, permissions={"view_devices": True})
    opts.devices_id = devices.pk

    RegisteredDevice.objects.create(
        user=target, device_id=f"{PREFIX}target_dev", device_token=f"{PREFIX}tok_t",
        platform="web", device_name=f"{PREFIX}target device")
    # A second device of the same user: two rows nesting one user in one
    # response exercise the shared request cache.
    RegisteredDevice.objects.create(
        user=target, device_id=f"{PREFIX}target_dev2", device_token=f"{PREFIX}tok_t2",
        platform="web", device_name=f"{PREFIX}target device 2")
    RegisteredDevice.objects.create(
        user=devices, device_id=f"{PREFIX}devices_dev", device_token=f"{PREFIX}tok_d",
        platform="web", device_name=f"{PREFIX}devices device")

    _key, opts.apikey_token = ApiKey.create_for_group(
        group=group, name=f"{PREFIX}key",
        permissions={"view_members": True, "view_users": True, "manage_users": True})

    book = Book.objects.create(
        title=f"{PREFIX}book", slug=f"{PREFIX}book-{_uuid.uuid4().hex[:6]}",
        group=group, user=target, created_by=target, modified_by=target)
    Page.objects.create(
        book=book, title=f"{PREFIX}page", slug=f"{PREFIX}page",
        content="body", user=target, created_by=target, modified_by=target)
    opts.book_id = book.pk


@th.django_unit_test("case 1: a view_members-only member gets other members without the private fields")
def test_list_view_members_only(opts):
    _login(opts, VIEWER)
    users = _list_members(opts)
    assert opts.target_id in users, f"the target member must be listed, got ids {sorted(users)}"
    _assert_stripped(users[opts.target_id], "view_members viewer, list")
    resp = opts.client.get(f"/api/group/member?group={opts.group_id}&size=100")
    row = next(r for r in resp.response.data if r["user"]["id"] == opts.target_id)
    assert "permissions" in row and "metadata" in row, \
        f"the member row's own permissions and metadata stay, got keys {sorted(row.keys())}"


@th.django_unit_test("case 2: the single-row read withholds the private fields from a view_members-only member")
def test_single_row_view_members_only(opts):
    _login(opts, VIEWER)
    resp = opts.client.get(f"/api/group/member/{opts.target_member_id}?group={opts.group_id}")
    assert resp.status_code == 200, \
        f"single-row read must succeed, got {resp.status_code}: {opts.client.last_response.body}"
    _assert_stripped(resp.response.data["user"], "view_members viewer, single row")


@th.django_unit_test("case 3: one response holds the caller's own full row and others stripped")
def test_self_full_others_stripped(opts):
    _login(opts, VIEWER)
    users = _list_members(opts)
    _assert_full(users[opts.viewer_id], "the viewer's own row")
    _assert_stripped(users[opts.target_id], "another member's row in the same response")


@th.django_unit_test("case 4: a global view_users holder and a superuser keep the full nested user")
def test_global_view_users_full(opts):
    _login(opts, ACCOUNT)
    users = _list_members(opts)
    _assert_full(users[opts.target_id], "global view_users viewer")
    assert users[opts.target_id]["permissions"].get("view_logs") is True, \
        f"the account viewer must see the target's real permissions, got {users[opts.target_id]['permissions']!r}"

    _login(opts, SUPER)
    users = _list_members(opts)
    _assert_full(users[opts.target_id], "superuser")


@th.django_unit_test("case 5: a manage_group holder gets others stripped on the list and on invite")
def test_manage_group_list_and_invite(opts):
    from mojo.apps.account.models import User
    _login(opts, MANAGER)
    users = _list_members(opts)
    _assert_stripped(users[opts.target_id], "manage_group viewer, list")

    User.objects.filter(email=INVITEE).delete()
    resp = opts.client.post("/api/group/member/invite",
                            {"email": INVITEE, "group": opts.group_id})
    assert resp.status_code == 200, \
        f"invite must succeed for a manage_group holder, got {resp.status_code}: {opts.client.last_response.body}"
    _assert_stripped(resp.response.data["user"], "manage_group viewer, invite response")


@th.django_unit_test("case 6: member-level view_users does not give the full view")
def test_member_level_view_users_stripped(opts):
    _login(opts, MEMBER_VU)
    users = _list_members(opts)
    _assert_stripped(users[opts.target_id], "member-level view_users viewer")


@th.django_unit_test("case 7: RegisteredDevice ?graph=full: owner full, two rows of another user stripped")
def test_registered_device_graph_full(opts):
    _login(opts, DEVICES)
    resp = opts.client.get(f"/api/account/devices/push?graph=full&size=100&search={PREFIX}")
    assert resp.status_code == 200, \
        f"device list must succeed for view_devices, got {resp.status_code}: {opts.client.last_response.body}"
    rows = resp.response.data
    by_owner = {row["user"]["id"]: row["user"] for row in rows}
    assert opts.target_id in by_owner and opts.devices_id in by_owner, \
        f"both test devices must be listed, got owners {sorted(by_owner)}"
    _assert_full(by_owner[opts.devices_id], "the caller's own device")
    target_rows = [row["user"] for row in rows if row["user"]["id"] == opts.target_id]
    assert len(target_rows) == 2, f"both of the target's devices must be listed, got {len(target_rows)}"
    for index, user in enumerate(target_rows):
        _assert_stripped(user, f"another user's device {index + 1} under ?graph=full")


PRIVATE_FILTERS = ("user__is_superuser=True", "user__requires_mfa=True",
                   "user__requires_password_change=True", "user__passkeys__isnull=False",
                   "user__totp__is_enabled=True")


def _member_count(opts, extra=""):
    resp = opts.client.get(f"/api/group/member?group={opts.group_id}&_mode=count{extra}")
    assert resp.status_code == 200, \
        f"member count must succeed, got {resp.status_code}: {opts.client.last_response.body}"
    return resp.response["count"]


@th.django_unit_test("case 8: filters into the private user fields are ignored for view_members, applied for account viewers")
def test_filter_oracle(opts):
    _login(opts, VIEWER)
    everyone = _list_members(opts)
    for probe in PRIVATE_FILTERS:
        filtered = _list_members(opts, f"&{probe}")
        assert set(filtered) == set(everyone), \
            f"{probe} must be dropped for a view_members viewer: {sorted(filtered)} vs {sorted(everyone)}"
    total = _member_count(opts)
    assert _member_count(opts, "&user__is_superuser=True") == total, \
        "_mode=count must drop user__is_superuser for a view_members viewer"
    by_email = _list_members(opts, f"&user__email={TARGET}")
    assert set(by_email) == {opts.target_id}, \
        f"an email filter still applies for a view_members viewer, got {sorted(by_email)}"
    searched = _list_members(opts, f"&search={TARGET}")
    assert opts.target_id in searched and len(searched) < len(everyone), \
        f"search by email still works for a view_members viewer, got {sorted(searched)}"
    resp = opts.client.get(f"/api/group?id={opts.group_id}&members__user__is_superuser=True")
    assert resp.status_code == 200, \
        f"group list must succeed for a member, got {resp.status_code}: {opts.client.last_response.body}"
    assert [g["id"] for g in resp.response.data] == [opts.group_id], \
        "the reverse form members__user__is_superuser must be dropped for a non-account viewer"

    _login(opts, ACCOUNT)
    filtered = _list_members(opts, "&user__requires_mfa=True")
    assert set(filtered) == {opts.target_id}, \
        f"the account viewer's requires_mfa filter must apply, got {sorted(filtered)}"
    assert _list_members(opts, "&user__is_superuser=True") == {}, \
        "the account viewer's is_superuser filter must apply"
    assert _member_count(opts, "&user__is_superuser=True") == 0, \
        "the account viewer's _mode=count filter must apply"


@th.django_unit_test("case 9: an ApiKey gets others stripped; a group token gets its own row full")
def test_apikey_and_group_token(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.account.services import group_token

    opts.client.logout()
    resp = opts.client.get(f"/api/group/member?group={opts.group_id}&size=100",
                           headers={"Authorization": f"apikey {opts.apikey_token}"})
    assert resp.status_code == 200, \
        f"the ApiKey must list members, got {resp.status_code}: {opts.client.last_response.body}"
    users = {row["user"]["id"]: row["user"] for row in resp.response.data}
    _assert_stripped(users[opts.target_id], "ApiKey holding view_users/manage_users")

    token = group_token.mint(User.objects.get(pk=opts.viewer_id), Group.objects.get(pk=opts.group_id))
    resp = opts.client.get(f"/api/group/member?group={opts.group_id}&size=100",
                           headers={"Authorization": f"grouptoken {token}"})
    assert resp.status_code == 200, \
        f"the group token must list members, got {resp.status_code}: {opts.client.last_response.body}"
    users = {row["user"]["id"]: row["user"] for row in resp.response.data}
    _assert_full(users[opts.viewer_id], "group token, its own row")
    _assert_stripped(users[opts.target_id], "group token, another member")


@th.django_unit_test("case 10: a docit Page read by a plain member nests its people stripped")
def test_docit_page_member_viewer(opts):
    _login(opts, VIEWER)
    resp = opts.client.get(f"/api/docit/page?group={opts.group_id}&book={opts.book_id}&graph=default")
    assert resp.status_code == 200, \
        f"a member must list the group's pages, got {resp.status_code}: {opts.client.last_response.body}"
    rows = resp.response.data
    assert rows, "the test page must be listed"
    for field in ("user", "created_by", "modified_by"):
        _assert_stripped(rows[0][field], f"docit Page.{field} (nested basic)", graph="basic")


@th.django_unit_test("case 11: a CSV dotted path into the private user fields is written empty for a non-account viewer")
def test_csv_dotted_user_path(opts):
    import csv
    import io
    from objict import objict
    from mojo.apps.account.models import User, GroupMember
    from mojo.models.rest import ACTIVE_REQUEST
    from mojo.serializers.formats.csv import CsvFormatter

    fields = ["id", "user.username", "user.requires_mfa", "user.metadata.note", "user.email"]
    queryset = GroupMember.objects.filter(pk=opts.target_member_id)

    def export(request):
        token = ACTIVE_REQUEST.set(request)
        try:
            text = CsvFormatter().serialize_queryset(queryset, fields=fields, raw_data=True)
        finally:
            ACTIVE_REQUEST.reset(token)
        return list(csv.reader(io.StringIO(text)))[1]

    def request_for(user_id):
        return objict(user=User.objects.get(pk=user_id), method="GET",
                      api_key=None, group_token=None)

    row = export(request_for(opts.viewer_id))
    assert row[1] == TARGET and row[4] == TARGET, \
        f"the username and email stay in the export, got row {row!r}"
    assert row[2] == "" and row[3] == "", \
        f"requires_mfa and metadata.note must be written empty for a view_members viewer, got row {row!r}"

    row = export(request_for(opts.account_id))
    assert row[2] == "True" and row[3] == "private" and row[4] == TARGET, \
        f"a global view_users viewer keeps the full export, got row {row!r}"

    row = export(None)
    assert row[2] == "True" and row[3] == "private", \
        f"no request (a job or the shell) leaves the export unchanged, got row {row!r}"


@th.django_unit_test("case 12: with no request bound a nested user keeps every field")
def test_no_request_unchanged(opts):
    from mojo.apps.account.models import GroupMember
    from mojo.models.rest import ACTIVE_REQUEST

    token = ACTIVE_REQUEST.set(None)
    try:
        data = GroupMember.objects.get(pk=opts.target_member_id).to_dict()
    finally:
        ACTIVE_REQUEST.reset(token)
    _assert_full(data["user"], "no request (a job, an email, the shell)")


def _stream_csv(queryset, fields, captured, ambient):
    """A streamed CSV export created while `captured` is bound, read while
    `ambient` is bound: the body is written after the request ends."""
    import csv
    import io
    from mojo.models.rest import ACTIVE_REQUEST
    from mojo.serializers.formats.csv import CsvFormatter

    token = ACTIVE_REQUEST.set(captured)
    try:
        response = CsvFormatter(streaming_threshold=0).serialize_queryset(queryset, fields=fields)
    finally:
        ACTIVE_REQUEST.reset(token)
    assert response.streaming, "the export must stream for this test to mean anything"
    token = ACTIVE_REQUEST.set(ambient)
    try:
        body = b"".join(response.streaming_content).decode()
    finally:
        ACTIVE_REQUEST.reset(token)
    return list(csv.reader(io.StringIO(body)))[1:]


def _request_for(user_id):
    from objict import objict
    from mojo.apps.account.models import User
    return objict(user=User.objects.get(pk=user_id), method="GET",
                  api_key=None, group_token=None)


STREAM_FIELDS = ["user.email", "user.requires_mfa", "user.metadata.note"]


@th.django_unit_test("case 13: a streamed CSV captured for a restricted viewer stays stripped when read under the target's request")
def test_csv_stream_other_ambient(opts):
    from mojo.apps.account.models import GroupMember

    queryset = GroupMember.objects.filter(pk=opts.target_member_id)
    rows = _stream_csv(queryset, STREAM_FIELDS,
                       _request_for(opts.viewer_id), _request_for(opts.target_id))
    assert rows[0][0] == TARGET, f"the email stays in the export, got row {rows[0]!r}"
    assert rows[0][1:] == ["", ""], \
        f"the target's requires_mfa and metadata.note must stay empty for the captured viewer, " \
        f"whoever is bound when the body is written; got row {rows[0]!r}"


@th.django_unit_test("case 14: a streamed CSV captured for a restricted viewer stays stripped when read with no request")
def test_csv_stream_no_ambient(opts):
    from mojo.apps.account.models import GroupMember

    queryset = GroupMember.objects.filter(pk=opts.target_member_id)
    rows = _stream_csv(queryset, STREAM_FIELDS, _request_for(opts.viewer_id), None)
    assert rows[0][1:] == ["", ""], \
        f"the target's requires_mfa and metadata.note must stay empty, got row {rows[0]!r}"


@th.django_unit_test("case 15: a streamed CSV keeps the viewer's own row full when read with no request")
def test_csv_stream_self_full(opts):
    from mojo.apps.account.models import GroupMember, User

    User.objects.filter(pk=opts.viewer_id).update(requires_mfa=True)
    try:
        queryset = GroupMember.objects.filter(user_id=opts.viewer_id, group_id=opts.group_id)
        rows = _stream_csv(queryset, ["user.email", "user.requires_mfa"],
                           _request_for(opts.viewer_id), None)
    finally:
        User.objects.filter(pk=opts.viewer_id).update(requires_mfa=False)
    assert rows == [[VIEWER, "True"]], f"the viewer's own row must stay full, got {rows!r}"


@th.django_unit_test("case 16: an ApiKey acting as its own user still gets its own nested row stripped")
def test_apikey_acting_as_self(opts):
    from objict import objict
    from mojo.apps.account.models import GroupMember
    from mojo.models.rest import ACTIVE_REQUEST

    request = _request_for(opts.viewer_id)
    request.api_key = objict(has_permission=lambda perms: True)
    member = GroupMember.objects.get(user_id=opts.viewer_id, group_id=opts.group_id)
    token = ACTIVE_REQUEST.set(request)
    try:
        user = member.to_dict()["user"]
    finally:
        ACTIVE_REQUEST.reset(token)
    _assert_stripped(user, "ApiKey request whose user is the nested user")


@th.django_unit_test("case 17: _stats drops a private user predicate for a view_members viewer and keeps the email one")
def test_stats_bundles(opts):
    import json
    _login(opts, VIEWER)
    total = _member_count(opts)
    resp = opts.client.get("/api/group/member", params={
        "group": opts.group_id, "_mode": "count",
        "_stats": json.dumps({"hidden": {"user__is_superuser": True},
                              "email": {"user__email": TARGET}}),
    })
    assert resp.status_code == 200, \
        f"the count must succeed, got {resp.status_code}: {opts.client.last_response.body}"
    assert resp.response.get("stats") == {"hidden": total, "email": 1}, \
        f"the hidden bundle must count every member and the email bundle one, got {resp.response!r}"
