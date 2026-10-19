"""
#8648 — a nested User shows its account fields only to a viewer who could
GET /api/user/<pk> for that user.

Before the fix GroupMember's `default` graph nested User `default` for every
`view_members` holder, so a plain member read other members' `permissions`,
`is_superuser`, `requires_mfa`, `requires_password_change`, `has_password`,
`has_passkey` and `metadata`. Nested graphs carried no permission (#2102).

The rule now: the user themselves, a holder of the global `view_users`,
`manage_users` or `users`, and a superuser get the nested User graph the
parent names. Everyone else gets User's NESTED_VIEW_GRAPH (identity only),
whatever graph the parent names (`default`, `basic`, ...). Filters, search
and sort that cross into User follow the same rule.
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
DEVICES = f"{PREFIX}devices@test.com"      # GLOBAL view_devices only
INVITEE = f"{PREFIX}invitee@test.com"      # invited by MANAGER

# The identity graph with Ian's choices 1-5 applied (see the plan on #8648).
IDENTITY_KEYS = {"id", "uuid", "username", "display_name", "is_active", "avatar"}
NEVER_KEYS = {"permissions", "is_superuser", "requires_mfa",
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


def _assert_identity(user_dict, who):
    keys = set(user_dict.keys())
    leaked = keys & NEVER_KEYS
    assert not leaked, f"{who}: the nested user must not carry {sorted(leaked)}, got keys {sorted(keys)}"
    assert keys == IDENTITY_KEYS, \
        f"{who}: the nested user must be exactly the identity graph {sorted(IDENTITY_KEYS)}, got {sorted(keys)}"


def _assert_full(user_dict, who):
    keys = set(user_dict.keys())
    missing = NEVER_KEYS - keys
    assert not missing, f"{who}: the full nested user must keep {sorted(missing)}, got keys {sorted(keys)}"


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

    devices = _make_user(DEVICES, permissions={"view_devices": True})
    opts.devices_id = devices.pk

    RegisteredDevice.objects.create(
        user=target, device_id=f"{PREFIX}target_dev", device_token=f"{PREFIX}tok_t",
        platform="web", device_name=f"{PREFIX}target device")
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


@th.django_unit_test("case 1: a view_members-only member gets other members' identity only")
def test_list_view_members_only(opts):
    _login(opts, VIEWER)
    users = _list_members(opts)
    assert opts.target_id in users, f"the target member must be listed, got ids {sorted(users)}"
    _assert_identity(users[opts.target_id], "view_members viewer, list")


@th.django_unit_test("case 2: the single-row read gives a view_members-only member identity only")
def test_single_row_view_members_only(opts):
    _login(opts, VIEWER)
    resp = opts.client.get(f"/api/group/member/{opts.target_member_id}?group={opts.group_id}")
    assert resp.status_code == 200, \
        f"single-row read must succeed, got {resp.status_code}: {opts.client.last_response.body}"
    _assert_identity(resp.response.data["user"], "view_members viewer, single row")


@th.django_unit_test("case 3: one response holds the caller's own full row and others' identity")
def test_self_full_others_identity(opts):
    _login(opts, VIEWER)
    users = _list_members(opts)
    _assert_full(users[opts.viewer_id], "the viewer's own row")
    _assert_identity(users[opts.target_id], "another member's row in the same response")


@th.django_unit_test("case 4: a global view_users holder keeps the full nested user")
def test_global_view_users_full(opts):
    _login(opts, ACCOUNT)
    users = _list_members(opts)
    _assert_full(users[opts.target_id], "global view_users viewer")
    assert users[opts.target_id]["permissions"].get("view_logs") is True, \
        f"the account viewer must see the target's real permissions, got {users[opts.target_id]['permissions']!r}"


@th.django_unit_test("case 5: a manage_group holder gets identity on the list and on invite")
def test_manage_group_list_and_invite(opts):
    from mojo.apps.account.models import User
    _login(opts, MANAGER)
    users = _list_members(opts)
    _assert_identity(users[opts.target_id], "manage_group viewer, list")

    User.objects.filter(email=INVITEE).delete()
    resp = opts.client.post("/api/group/member/invite",
                            {"email": INVITEE, "group": opts.group_id})
    assert resp.status_code == 200, \
        f"invite must succeed for a manage_group holder, got {resp.status_code}: {opts.client.last_response.body}"
    _assert_identity(resp.response.data["user"], "manage_group viewer, invite response")


@th.django_unit_test("case 6: member-level view_users does not give the full view")
def test_member_level_view_users_identity(opts):
    _login(opts, MEMBER_VU)
    users = _list_members(opts)
    _assert_identity(users[opts.target_id], "member-level view_users viewer")


@th.django_unit_test("case 7: RegisteredDevice ?graph=full — owner full, others identity")
def test_registered_device_graph_full(opts):
    _login(opts, DEVICES)
    resp = opts.client.get(f"/api/account/devices/push?graph=full&size=100&search={PREFIX}")
    assert resp.status_code == 200, \
        f"device list must succeed for view_devices, got {resp.status_code}: {opts.client.last_response.body}"
    by_owner = {row["user"]["id"]: row["user"] for row in resp.response.data}
    assert opts.target_id in by_owner and opts.devices_id in by_owner, \
        f"both test devices must be listed, got owners {sorted(by_owner)}"
    _assert_full(by_owner[opts.devices_id], "the caller's own device")
    _assert_identity(by_owner[opts.target_id], "another user's device under ?graph=full")


@th.django_unit_test("case 8: filters into User fields outside identity are ignored for view_members, applied for account viewers")
def test_filter_oracle(opts):
    _login(opts, VIEWER)
    everyone = _list_members(opts)
    filtered = _list_members(opts, "&user__requires_mfa=True")
    assert set(filtered) == set(everyone), \
        f"user__requires_mfa must be dropped for a view_members viewer: {sorted(filtered)} vs {sorted(everyone)}"
    sorted_by_login = _list_members(opts, "&sort=-user__last_login")
    assert set(sorted_by_login) == set(everyone), "a sort into User must not change the rows"

    _login(opts, ACCOUNT)
    filtered = _list_members(opts, "&user__requires_mfa=True")
    assert set(filtered) == {opts.target_id}, \
        f"the account viewer's filter must apply, got {sorted(filtered)}"


@th.django_unit_test("case 9: an ApiKey gets identity; a group token gets its own row full")
def test_apikey_and_group_token(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.account.services import group_token

    opts.client.logout()
    resp = opts.client.get(f"/api/group/member?group={opts.group_id}&size=100",
                           headers={"Authorization": f"apikey {opts.apikey_token}"})
    assert resp.status_code == 200, \
        f"the ApiKey must list members, got {resp.status_code}: {opts.client.last_response.body}"
    users = {row["user"]["id"]: row["user"] for row in resp.response.data}
    _assert_identity(users[opts.target_id], "ApiKey holding view_users/manage_users")

    token = group_token.mint(User.objects.get(pk=opts.viewer_id), Group.objects.get(pk=opts.group_id))
    resp = opts.client.get(f"/api/group/member?group={opts.group_id}&size=100",
                           headers={"Authorization": f"grouptoken {token}"})
    assert resp.status_code == 200, \
        f"the group token must list members, got {resp.status_code}: {opts.client.last_response.body}"
    users = {row["user"]["id"]: row["user"] for row in resp.response.data}
    _assert_full(users[opts.viewer_id], "group token, its own row")
    _assert_identity(users[opts.target_id], "group token, another member")


@th.django_unit_test("case 10: a docit Page read by a plain member nests its people as identity")
def test_docit_page_member_viewer(opts):
    _login(opts, VIEWER)
    resp = opts.client.get(f"/api/docit/page?group={opts.group_id}&book={opts.book_id}&graph=default")
    assert resp.status_code == 200, \
        f"a member must list the group's pages, got {resp.status_code}: {opts.client.last_response.body}"
    rows = resp.response.data
    assert rows, "the test page must be listed"
    for field in ("user", "created_by", "modified_by"):
        _assert_identity(rows[0][field], f"docit Page.{field} (nested basic)")


@th.django_unit_test("case 11: a CSV dotted path into User outside identity is written empty for a non-account viewer")
def test_csv_dotted_user_path(opts):
    import csv
    import io
    from objict import objict
    from mojo.apps.account.models import User, GroupMember
    from mojo.models.rest import ACTIVE_REQUEST
    from mojo.serializers.formats.csv import CsvFormatter

    fields = ["id", "user.username", "user.requires_mfa", "user.email"]
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
    assert row[1] == TARGET, f"the username stays in the export, got row {row!r}"
    assert row[2] == "" and row[3] == "", \
        f"requires_mfa and email must be written empty for a view_members viewer, got row {row!r}"

    row = export(request_for(opts.account_id))
    assert row[2] == "True" and row[3] == TARGET, \
        f"a global view_users viewer keeps the full export, got row {row!r}"

    row = export(None)
    assert row[3] == TARGET, f"no request (a job or the shell) leaves the export unchanged, got row {row!r}"
