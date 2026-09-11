"""Consumer-declared roles on the group-<pk> metrics gate (maestro WMWX #4195).

A deployment may nominate extra permission keys — METRICS_GROUP_VIEW_ROLES /
METRICS_GROUP_WRITE_ROLES — that satisfy a read/write of ITS OWN group
accounts. The hook lives only inside ``_check_group_account_permission``:
``global``, ``user-<pk>``, ``public`` and custom accounts never see the list,
and the tokens ``GroupMember.has_permission`` answers True for unconditionally
(``all`` / ``authenticated`` / ``member`` / ``full_member``) are refused so a
settings typo cannot open a brand's counters to every member.

In-process, no HTTP client. The default tier may not mutate
``django.conf.settings``, so the resolved role list is INJECTED into the gate
(the ``extra_roles`` argument the two callers fill from the setting) and the
reader is checked at its unset default. The settings-name wiring end to end is
pinned by the consumer's own HTTP test against its real settings file.
"""

TESTIT_TIER = "core"

from testit import helpers as th

BRAND_A = "t4195_brand_a"
BRAND_B = "t4195_brand_b"
MEMBER_A = "t4195_member_a@test.com"
PLATFORM = "t4195_platform@test.com"
PROBE_ROLE = "t4195_probe_role"
VIEW = ["view_metrics", "metrics"]
WRITE = ["write_metrics", "metrics"]


def _request_for(user, path="/api/metrics/fetch"):
    from django.test import RequestFactory
    from mojo.helpers.request_parser import parse_request_data

    request = RequestFactory().get(path)
    request.DATA = parse_request_data(request)
    request.user = user
    return request


@th.django_unit_setup()
def setup_group_role_gate(opts):
    from mojo.apps.account.models import Group, User

    User.objects.filter(email__in=[MEMBER_A, PLATFORM]).delete()
    Group.objects.filter(name__in=[BRAND_A, BRAND_B]).delete()

    brand_a = Group.objects.create(name=BRAND_A, kind="org")
    brand_b = Group.objects.create(name=BRAND_B, kind="org")
    opts.brand_a = brand_a.pk
    opts.brand_b = brand_b.pk

    member = User.objects.create_user(username=MEMBER_A, email=MEMBER_A, password="t4195##pw")
    member.is_active = True
    member.save()
    brand_a.add_member(member).add_permission(PROBE_ROLE)
    opts.member_id = member.pk

    platform = User.objects.create_user(username=PLATFORM, email=PLATFORM, password="t4195##pw")
    platform.is_active = True
    platform.save()
    platform.add_permission(PROBE_ROLE)
    opts.platform_id = platform.pk


def _member(opts):
    from mojo.apps.account.models import User
    return User.objects.get(pk=opts.member_id)


def _platform(opts):
    from mojo.apps.account.models import User
    return User.objects.get(pk=opts.platform_id)


@th.django_unit_test("unset settings leave the group gate exactly as before")
def test_default_unchanged(opts):
    from mojo import errors as me
    from mojo.apps.metrics.rest import helpers

    assert helpers._consumer_roles("METRICS_GROUP_VIEW_ROLES") == [], (
        "an undeclared view-role setting must resolve to an empty list")
    assert helpers._consumer_roles("METRICS_GROUP_WRITE_ROLES") == [], (
        "an undeclared write-role setting must resolve to an empty list")
    request = _request_for(_member(opts))
    with th.assert_raises(me.PermissionDeniedException):
        helpers.check_view_permissions(request, f"group-{opts.brand_a}")


@th.django_unit_test("a nominated role reads its own brand and not another")
def test_role_allows_own_group_only(opts):
    """THE regression: a member holding only a consumer role is 403 on their own
    brand today; with the role nominated they read it — and still not brand B."""
    from mojo import errors as me
    from mojo.apps.metrics.rest import helpers

    request = _request_for(_member(opts))
    assert helpers._check_group_account_permission(
        request, f"group-{opts.brand_a}", VIEW, [PROBE_ROLE]) is True, (
        "a member holding a nominated role must read their own group account")
    with th.assert_raises(me.PermissionDeniedException):
        helpers._check_group_account_permission(
            request, f"group-{opts.brand_b}", VIEW, [PROBE_ROLE])


@th.django_unit_test("a user-level role reaches every brand without membership")
def test_user_level_role_is_cross_brand(opts):
    """Deliberate: the user-level grant path is how a platform-wide role reaches
    a brand account, the same reach a user-level view_metrics has always had."""
    from mojo.apps.metrics.rest import helpers

    request = _request_for(_platform(opts))
    for pk in (opts.brand_a, opts.brand_b):
        assert helpers._check_group_account_permission(
            request, f"group-{pk}", VIEW, [PROBE_ROLE]) is True, (
            f"a user-level nominated role must read group-{pk} with no membership")


@th.django_unit_test("always-true member tokens are refused, not honored")
def test_always_true_tokens_refused(opts):
    from mojo import errors as me
    from mojo.apps.metrics.rest import helpers

    assert helpers._merge_roles(["view_metrics"], ["member", PROBE_ROLE, "view_metrics"]) == \
        ["view_metrics", PROBE_ROLE], "merge must drop always-true tokens and duplicates"
    request = _request_for(_member(opts))
    for token in ("all", "authenticated", "member", "full_member"):
        with th.assert_raises(me.PermissionDeniedException):
            helpers._check_group_account_permission(
                request, f"group-{opts.brand_a}", VIEW, [token])


@th.django_unit_test("consumer roles never reach global, user- or public accounts")
def test_roles_confined_to_group_prefix(opts):
    from mojo import errors as me
    from mojo.apps.metrics.rest import helpers

    request = _request_for(_platform(opts))
    for account in ("global", f"user-{opts.member_id}", "public", "t4195_custom"):
        assert helpers._check_group_account_permission(request, account, VIEW, [PROBE_ROLE]) is False, (
            f"the group helper must not engage on {account!r} whatever roles are passed")
    with th.assert_raises(me.PermissionDeniedException):
        helpers.check_view_permissions(request, "global")


@th.django_unit_test("the write list is independent of the view list")
def test_write_roles_independent(opts):
    from mojo import errors as me
    from mojo.apps.metrics.rest import helpers

    request = _request_for(_member(opts))
    assert helpers._check_group_account_permission(
        request, f"group-{opts.brand_a}", VIEW, [PROBE_ROLE]) is True, "view must pass"
    with th.assert_raises(me.PermissionDeniedException):
        helpers._check_group_account_permission(
            request, f"group-{opts.brand_a}", WRITE,
            helpers._consumer_roles("METRICS_GROUP_WRITE_ROLES"))
