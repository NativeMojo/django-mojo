"""ITEM-035: bare domain terms are view_X + manage_X combined.

The bare domain-category permission ("users", "groups", ...) is NOT a lower
tier — it is view_X and manage_X combined into one simple term. Any gate that
accepts manage_X must therefore accept bare X. The fix is central
(mojo/helpers/perms.py + the three has_permission checkers), so these tests
cover the expansion itself plus the previously-blocked REST gates.
"""
import uuid as _uuid
from testit import helpers as th

from tests.test_global_perms._helpers import (
    make_user, make_group_member, login,
)

TOGGLE_PERM = "gp_bt_toggle_perm"


@th.django_unit_setup()
def setup_base_term_expansion(opts):
    from mojo.apps.account.models import User, GroupMember
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    from mojo.apps.account.models.setting import Setting

    # Clean slate on the long-lived DB — no protection map may interfere with
    # the default can_change_permission fallback these tests exercise.
    Setting.remove("MEMBER_PERMS_PROTECTION")
    Setting.remove("APIKEY_PERMS_PROTECTION")

    # Actor holding ONLY member-level {"groups": True} + their group.
    actor, actor_email, actor_pw, group = make_group_member(["groups"])
    opts.actor = actor
    opts.actor_email = actor_email
    opts.actor_pw = actor_pw
    opts.group = group

    # A fellow member of the same group (perm-toggle target).
    target, _, _ = make_user()
    tm, _ = GroupMember.objects.get_or_create(user=target, group=group)
    tm.permissions = {}
    tm.save()
    opts.target_member_id = tm.pk

    # An EXISTING user to invite (avoids the auto-create-User + email path).
    invitee, invitee_email, _ = make_user()
    opts.invitee_email = invitee_email

    # User holding ONLY the bare global "users" perm.
    users_only, users_email, users_pw = make_user(["users"])
    opts.users_only = users_only
    opts.users_email = users_email
    opts.users_pw = users_pw

    # Whitelisted geoip row for the SAVE_PERMS gate test.
    GeoLocatedIP.objects.filter(ip_address="203.0.113.235").delete()
    geo = GeoLocatedIP.objects.create(
        ip_address="203.0.113.235",
        provider="test",
        is_whitelisted=True,
        whitelisted_reason="ITEM-035 fixture",
    )
    opts.geo_id = geo.pk


# ---------------------------------------------------------------------------
# Checker-level expansion
# ---------------------------------------------------------------------------

@th.django_unit_test("bare term satisfies view_/manage_ checks on User")
def test_user_bare_term_expansion(opts):
    from mojo.apps.account.models import User

    holder = opts.users_only
    assert holder.has_permission("manage_users") is True, \
        f"bare 'users' must satisfy a manage_users check, perms={holder.permissions!r}"
    assert holder.has_permission("view_users") is True, \
        f"bare 'users' must satisfy a view_users check, perms={holder.permissions!r}"
    assert holder.has_permission("users") is True, \
        "bare term itself must still be granted"

    # One-directional: manage_users alone does NOT grant the combined term.
    manage_only = User(permissions={"manage_users": True})
    assert manage_only.has_permission("users") is False, \
        "manage_users must NOT satisfy a check for the combined 'users' term"

    # Non-category suffixes are not expanded.
    odd = User(permissions={"members": True, "settings": True})
    assert odd.has_permission("manage_members") is False, \
        "'members' is not a domain category — no expansion"
    assert odd.has_permission("manage_settings") is False, \
        "'settings' is not a domain category — no expansion"


@th.django_unit_test("bare term satisfies view_/manage_ checks on GroupMember")
def test_member_bare_term_expansion(opts):
    from mojo.apps.account.models import GroupMember

    member = GroupMember.objects.get(user=opts.actor, group=opts.group)
    assert member.has_permission("manage_groups") is True, \
        f"member-level bare 'groups' must satisfy manage_groups, perms={member.permissions!r}"
    assert member.has_permission("view_groups") is True, \
        f"member-level bare 'groups' must satisfy view_groups, perms={member.permissions!r}"
    assert member.has_permission("manage_group") is False, \
        "'manage_group' (singular, member-scoped) is not a domain category — no expansion"
    assert member.has_permission("manage_members") is False, \
        "'members' is not a domain category — no expansion"


@th.django_unit_test("bare term satisfies view_/manage_ checks on ApiKey")
def test_apikey_bare_term_expansion(opts):
    from mojo.apps.account.models import ApiKey

    key = ApiKey(permissions={"groups": True})
    assert key.has_permission("manage_groups") is True, \
        "ApiKey bare 'groups' must satisfy manage_groups"
    assert key.has_permission("sys.manage_users") is False, \
        "sys.* must remain always-denied for API keys"

    manage_only = ApiKey(permissions={"manage_groups": True})
    assert manage_only.has_permission("groups") is False, \
        "manage_groups must NOT satisfy a check for the combined 'groups' term"


# ---------------------------------------------------------------------------
# Previously-blocked REST gates (each was a surprise 403 before the fix)
# ---------------------------------------------------------------------------

@th.django_unit_test("member with bare 'groups' can toggle a fellow member's perm")
def test_member_perm_toggle_with_bare_groups(opts):
    """Regression for the shipped MemberView.js bug: the can_change_permission
    fallback (member.py) only listed manage_* — bare 'groups' got a 403."""
    from mojo.apps.account.models import GroupMember

    login(opts, opts.actor_email, opts.actor_pw)
    resp = opts.client.post(
        f"/api/group/member/{opts.target_member_id}",
        {"group": opts.group.pk, "permissions": {TOGGLE_PERM: True}},
    )
    opts.client.logout()
    assert resp.status_code == 200, (
        f"bare-'groups' member must be able to toggle a fellow member's perm, "
        f"got {resp.status_code}: {opts.client.last_response.body}"
    )
    tm = GroupMember.objects.get(pk=opts.target_member_id)
    assert tm.permissions.get(TOGGLE_PERM) is True, \
        f"toggled perm must land on the member, got {tm.permissions!r}"


@th.django_unit_test("member with bare 'groups' can invite an existing user")
def test_invite_with_bare_groups(opts):
    """The invite gate (rest/group.py) listed only manage_* perms — bare
    'groups' (which IS manage_groups by definition) was denied."""
    from mojo.apps.account.models import User, GroupMember

    login(opts, opts.actor_email, opts.actor_pw)
    resp = opts.client.post(
        "/api/group/member/invite",
        {"group": opts.group.pk, "email": opts.invitee_email},
    )
    opts.client.logout()
    assert resp.status_code == 200, (
        f"bare-'groups' member must be able to invite, "
        f"got {resp.status_code}: {opts.client.last_response.body}"
    )
    invitee = User.objects.get(email=opts.invitee_email)
    assert GroupMember.objects.filter(user=invitee, group=opts.group).exists(), \
        "invitee must have been added as a member"


@th.django_unit_test("user with bare 'users' passes GeoLocatedIP SAVE_PERMS")
def test_geoip_action_with_bare_users(opts):
    """GeoLocatedIP.SAVE_PERMS lists manage_users but not the combined 'users'
    term its own VIEW_PERMS accepts — a bare-'users' admin got a 403 on every
    POST_SAVE_ACTION. unwhitelist is the least side-effectful action (flag
    clear + geofence cache invalidation; no firewall broadcast)."""
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    login(opts, opts.users_email, opts.users_pw)
    resp = opts.client.post(f"/api/system/geoip/{opts.geo_id}", {"unwhitelist": True})
    opts.client.logout()
    assert resp.status_code == 200, (
        f"bare-'users' admin must pass the geoip SAVE gate, "
        f"got {resp.status_code}: {opts.client.last_response.body}"
    )
    geo = GeoLocatedIP.objects.get(pk=opts.geo_id)
    assert geo.is_whitelisted is False, \
        f"unwhitelist action must have run, got is_whitelisted={geo.is_whitelisted!r}"
