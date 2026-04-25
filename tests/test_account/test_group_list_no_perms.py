"""
Regression suite for permission-denied event emission on GET /api/group.

Covers four scenarios:
  1. Authed user, no perms, no memberships → 200 empty list, 0 events.
  2. Authed user, no perms, with a membership → 200 with their group, 0 events.
  3. Authed user, with view_groups perm → 200 list, 0 events.
  4. Anonymous request → 401, exactly 1 `unauthenticated` event.

See planning/issues/spurious-permission-denied-events-on-list.md
"""
from testit import helpers as th


TEST_USER = "group_list_noperm"
TEST_USER_MEMBER = "group_list_member"
TEST_USER_PERM = "group_list_perm"
TEST_PWORD = "testit##mojo"
TEST_GROUP_NAME = "noperm-fixture-group"
DENY_CATEGORIES = [
    "user_permission_denied",
    "view_permission_denied",
    "group_member_permission_denied",
]


def _reset_user(username):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.member import GroupMember

    user = User.objects.filter(username=username).last()
    if user is None:
        user = User(
            username=username,
            display_name=username,
            email=f"{username}@example.com",
        )
        user.save()
    user.is_email_verified = True
    user.save_password(TEST_PWORD)
    user.remove_all_permissions()
    user.is_staff = False
    user.is_superuser = False
    user.save()
    GroupMember.objects.filter(user=user).delete()
    return user


@th.django_unit_setup()
def setup_users(opts):
    from mojo.apps.account.models.group import Group
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    from mojo.apps.incident.models.event import Event

    # Pre-create the loopback geo-IP row to avoid the parallel-login
    # race in GeoLocatedIP.geolocate that races across test modules.
    GeoLocatedIP.objects.get_or_create(
        ip_address="127.0.0.1", defaults={"subnet": "127.0.0.0/8"},
    )

    noperm = _reset_user(TEST_USER)
    member = _reset_user(TEST_USER_MEMBER)
    permed = _reset_user(TEST_USER_PERM)
    permed.add_permission("view_groups")
    permed.save()

    Group.objects.filter(name=TEST_GROUP_NAME).delete()
    group = Group(name=TEST_GROUP_NAME, kind="default")
    group.save()
    group.add_member(member)

    Event.objects.filter(
        uid__in=[noperm.id, member.id, permed.id],
        category__in=DENY_CATEGORIES + ["unauthenticated"],
    ).delete()

    opts.noperm_user_id = noperm.id
    opts.member_user_id = member.id
    opts.permed_user_id = permed.id
    opts.fixture_group_id = group.id


@th.django_unit_test()
def test_no_perms_no_memberships_returns_empty_no_events(opts):
    from mojo.apps.incident.models.event import Event

    assert opts.client.login(TEST_USER, TEST_PWORD), "login failed for noperm user"
    resp = opts.client.get("/api/group", params={"start": 0, "size": 1000})

    assert resp.status_code == 200, (
        f"Expected 200, got {resp.status_code}: {resp.response!r}"
    )
    body = resp.response
    assert body.status is True, f"Expected status=true, got {body!r}"
    data = body["data"]
    assert isinstance(data, list) and data == [], (
        f"Expected empty list for user with no memberships, got {data!r}"
    )

    bogus = list(
        Event.objects.filter(
            uid=opts.noperm_user_id, category__in=DENY_CATEGORIES,
        ).values_list("category", "metadata")
    )
    assert not bogus, (
        f"GET /api/group returned 200 but logged {len(bogus)} spurious "
        f"denial event(s) for noperm user: {bogus!r}"
    )


@th.django_unit_test()
def test_no_perms_with_membership_returns_groups_no_events(opts):
    from mojo.apps.incident.models.event import Event

    assert opts.client.login(TEST_USER_MEMBER, TEST_PWORD), "login failed for member user"
    resp = opts.client.get("/api/group", params={"start": 0, "size": 1000})

    assert resp.status_code == 200, (
        f"Expected 200, got {resp.status_code}: {resp.response!r}"
    )
    data = resp.response["data"]
    assert isinstance(data, list), f"Expected list under 'data', got {data!r}"
    ids = [g.get("id") for g in data]
    assert opts.fixture_group_id in ids, (
        f"Expected member's group {opts.fixture_group_id} in list, got ids={ids}"
    )

    bogus = list(
        Event.objects.filter(
            uid=opts.member_user_id, category__in=DENY_CATEGORIES,
        ).values_list("category", "metadata")
    )
    assert not bogus, (
        f"Member user got their groups (200) but logged {len(bogus)} "
        f"denial event(s): {bogus!r}"
    )


@th.django_unit_test()
def test_user_with_view_groups_perm_no_events(opts):
    from mojo.apps.incident.models.event import Event

    assert opts.client.login(TEST_USER_PERM, TEST_PWORD), "login failed for permed user"
    resp = opts.client.get("/api/group", params={"start": 0, "size": 1000})

    assert resp.status_code == 200, (
        f"Expected 200, got {resp.status_code}: {resp.response!r}"
    )
    data = resp.response["data"]
    assert isinstance(data, list), f"Expected list, got {data!r}"

    bogus = list(
        Event.objects.filter(
            uid=opts.permed_user_id, category__in=DENY_CATEGORIES,
        ).values_list("category", "metadata")
    )
    assert not bogus, (
        f"User with view_groups got allowed list (200) but logged {len(bogus)} "
        f"denial event(s): {bogus!r}"
    )


@th.django_unit_test()
def test_anonymous_request_is_401_with_unauthenticated_event(opts):
    from testit.client import RestClient
    from mojo.apps.incident.models.event import Event

    # Use a fresh client so we have no JWT cookie/header.
    anon = RestClient(host=opts.client.host, logger=opts.client.logger)
    pre_count = Event.objects.filter(
        category="unauthenticated", metadata__http_path="/api/group",
    ).count()

    resp = anon.get("/api/group", params={"start": 0, "size": 1000})
    assert resp.status_code == 401, (
        f"Expected 401 for unauthenticated GET /api/group, got "
        f"{resp.status_code}: {resp.response!r}"
    )

    post_count = Event.objects.filter(
        category="unauthenticated", metadata__http_path="/api/group",
    ).count()
    assert post_count == pre_count + 1, (
        f"Expected exactly 1 new `unauthenticated` event for /api/group, "
        f"saw delta {post_count - pre_count}"
    )
