"""Maestro #1135 — "full_member" pseudo-perm + guest member marker.

"member" stays the view tier (any member row); "full_member" is the write
tier — a member row whose permissions JSON has no truthy "guest" key. The
perm is derived from the marker alone: a stored permissions["full_member"]
key is ignored on member rows. Marking guest does not strip other grants
(demotion = strip + mark) — the mixed-list test pins that contract.

Seam tests cover Group.user_has_permission, the path REST perm lists take:
the global User dict is consulted first, so superusers and explicit global
full_member grants override the per-group marker by design.
"""
import uuid as _uuid
from testit import helpers as th

PASSWORD = "Fm##guest99"


def _mk_user(email, superuser=False):
    from mojo.apps.account.models import User
    user = User.objects.create_user(username=email, email=email, password=PASSWORD)
    user.is_active = True
    user.is_email_verified = True
    if superuser:
        user.is_superuser = True
    user.save()
    return user


def _member(opts, user_id):
    from mojo.apps.account.models import GroupMember
    return GroupMember.objects.get(group_id=opts.grp_id, user_id=user_id)


@th.django_unit_setup()
def setup_member_full_member(opts):
    """One group with: a plain member (no perms), a manager later marked
    guest (contract test), and two non-member users (outsider, superuser)."""
    from mojo.apps.account.models import User, Group

    # delete-before-create — tests run against a long-lived DB
    User.objects.filter(email__startswith="fullmem_").delete()
    Group.objects.filter(name__startswith="fullmem_grp_").delete()

    tag = _uuid.uuid4().hex[:8]
    grp = Group.objects.create(
        name=f"fullmem_grp_{tag}", kind="organization", is_active=True)
    opts.grp_id = grp.pk

    plain = _mk_user(f"fullmem_plain_{tag}@account.test")
    grp.add_member(plain)
    opts.plain_id = plain.pk

    mgr = _mk_user(f"fullmem_mgr_{tag}@account.test")
    grp.add_member(mgr)
    opts.mgr_id = mgr.pk

    opts.outsider_id = _mk_user(f"fullmem_out_{tag}@account.test").pk
    opts.super_id = _mk_user(f"fullmem_super_{tag}@account.test", superuser=True).pk


@th.django_unit_test("full_member: plain member row passes every tier")
def test_plain_member_tiers(opts):
    m = _member(opts, opts.plain_id)
    assert m.has_permission("full_member"), \
        "an unmarked member row must satisfy full_member (marker absent = full member)"
    for tier in ("member", "all", "authenticated"):
        assert m.has_permission(tier), \
            f"an unmarked member row must satisfy the {tier!r} view tier"


@th.django_unit_test("full_member: guest marker blocks the write tier, view tier intact")
def test_guest_marker_blocks_full_member(opts):
    m = _member(opts, opts.plain_id)
    m.add_permission("guest")
    assert not m.has_permission("full_member"), \
        "a row with a truthy permissions['guest'] must fail full_member"
    assert not m.has_permission(["full_member"]), \
        "list-OR form must agree with the scalar: a guest fails ['full_member']"
    for tier in ("member", "all", "authenticated"):
        assert m.has_permission(tier), \
            f"marking guest must not touch the {tier!r} view tier"
    m.remove_permission("guest")


@th.django_unit_test("full_member: removing the guest marker restores the write tier")
def test_remove_guest_restores(opts):
    m = _member(opts, opts.plain_id)
    m.add_permission("guest")
    m.remove_permission("guest")
    assert m.has_permission("full_member"), \
        "after remove_permission('guest') the row must satisfy full_member again"
    assert m.has_permission(["full_member"]), \
        "list-OR form must agree with the scalar after the marker is removed"


@th.django_unit_test("full_member: guest marker does NOT strip other grants (demotion contract)")
def test_guest_with_manage_grant_passes_manage_tiers(opts):
    m = _member(opts, opts.mgr_id)
    m.add_permission("manage_group")
    m.add_permission("guest")
    assert not m.has_permission("full_member"), \
        "the marker must fail full_member even while other grants remain"
    assert m.has_permission("manage_group"), \
        "marking guest must not strip a retained manage_group grant"
    assert m.has_permission(["manage_group", "full_member"]), \
        "an OR tier containing manage_group must still pass for a marked row that " \
        "retains the grant — demotion requires stripping grants AND setting the marker"


@th.django_unit_test("full_member: stored permissions['full_member'] key is ignored on member rows")
def test_stored_full_member_key_is_ignored(opts):
    m = _member(opts, opts.plain_id)
    m.add_permission("guest")
    m.permissions["full_member"] = True
    m.save()
    assert not m.has_permission("full_member"), \
        "full_member is derived from the guest marker only — a stored " \
        "permissions['full_member'] grant must not unlock the write tier for a guest"
    m.remove_permission("guest")
    m.remove_permission("full_member")


@th.django_unit_test("full_member: present-but-falsy guest marker means full member")
def test_falsy_guest_marker_means_full_member(opts):
    m = _member(opts, opts.plain_id)
    m.permissions["guest"] = False
    m.save()
    assert m.has_permission("full_member"), \
        "permissions['guest'] = False (present but falsy) must count as full member"
    m.remove_permission("guest")


@th.django_unit_test("full_member: Group.user_has_permission seam (global dict first, then member row)")
def test_user_has_permission_seam(opts):
    from mojo.apps.account.models import User, Group
    grp = Group.objects.get(pk=opts.grp_id)
    plain = User.objects.get(pk=opts.plain_id)
    outsider = User.objects.get(pk=opts.outsider_id)
    superuser = User.objects.get(pk=opts.super_id)

    assert grp.user_has_permission(plain, ["full_member"]), \
        "seam: an unmarked member must pass ['full_member'] through user_has_permission"

    m = _member(opts, opts.plain_id)
    m.add_permission("guest")
    assert not grp.user_has_permission(plain, ["full_member"]), \
        "seam: a guest member must fail ['full_member'] through user_has_permission"

    assert not grp.user_has_permission(outsider, ["full_member"]), \
        "seam: a non-member non-superuser must fail ['full_member'] (fail closed)"
    assert grp.user_has_permission(superuser, ["full_member"]), \
        "seam: a superuser must pass ['full_member'] via the User-level bypass"

    # Explicit global grant on the user overrides the per-group marker —
    # intentional operator-override semantics (global dict is checked first).
    plain.add_permission("full_member")
    plain = User.objects.get(pk=opts.plain_id)
    assert grp.user_has_permission(plain, ["full_member"]), \
        "seam: an explicit global full_member grant must override the guest marker"

    plain.remove_permission("full_member")
    m.remove_permission("guest")
