"""GROUP_FIELD honoring on a model with NO direct `group` FK.

FileRendition scopes to its tenant through `original_file__group`
(RestMeta.GROUP_FIELD, a related path). Before the fix, only the `?group=`
narrower (`on_rest_list`) consulted GROUP_FIELD — the two permission seams did
not:

  - the bare list fallback in `on_rest_handle_list` gated on
    `hasattr(cls, "group")` → a group MEMBER (perm held at the GroupMember
    level, not the user level) was denied even their OWN renditions, while a
    user-level perm holder got `objects.all()` unfiltered (cross-tenant leak);
  - detail `_evaluate_permission` gated on `hasattr(cls, "group")` → it fell
    through to a flat `user.has_permission` check, so it never scoped to the
    row's tenant.

These tests exercise those two paths with a MEMBER-level grant (the case that
flipped): a member of group A sees their own rendition and never group B's, on
the bare list AND on detail; a user-level (platform) grant still sees both.

Self-contained: own users, groups, FileManager, files, renditions — does not
lean on the shortlink suite's fixtures.
"""
import os
import tempfile
import shutil as _shutil

from testit import helpers as th
from testit.helpers import assert_true, assert_eq

MEMBER_USER = "gf_member_a"
GLOBAL_USER = "gf_global"
PWORD = "gfield##mojo99"


def _write_stub(tmpdir, storage_path, data=b"hi"):
    full = os.path.join(tmpdir, storage_path.lstrip("/"))
    os.makedirs(os.path.dirname(full) or tmpdir, exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(data)


@th.django_unit_setup()
def setup_group_field_scoping(opts):
    from mojo.apps.account.models import User, Group, GroupMember
    from mojo.apps.fileman.models import FileManager, File, FileRendition
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip="127.0.0.1", key="login")

    # Clean up prior runs (long-lived DB).
    FileRendition.objects.filter(role__startswith="gf_").delete()
    File.objects.filter(filename__startswith="gf_").delete()
    FileManager.objects.filter(name__startswith="gf_fm_").delete()
    GroupMember.objects.filter(group__name__startswith="gf_group_").delete()
    Group.objects.filter(name__startswith="gf_group_").delete()
    User.objects.filter(username__in=[MEMBER_USER, GLOBAL_USER]).delete()

    opts.tmpdir = tempfile.mkdtemp(prefix="mojo_gf_test_")

    group_a = Group.objects.create(name="gf_group_a", kind="organization")
    group_b = Group.objects.create(name="gf_group_b", kind="organization")
    opts.group_a_id = group_a.id
    opts.group_b_id = group_b.id

    # MEMBER-level grant only: view_fileman on group A, nothing at the user
    # level. This is the case the old code denied outright.
    member = User.objects.create(username=MEMBER_USER, email=f"{MEMBER_USER}@example.com")
    member.is_email_verified = True
    member.save_password(PWORD)
    member.save()
    gm = group_a.add_member(member)
    gm.add_permission(["view_fileman"])
    opts.member_id = member.id

    # USER-level (platform) grant: sees everything, no membership needed.
    glob = User.objects.create(username=GLOBAL_USER, email=f"{GLOBAL_USER}@example.com")
    glob.is_email_verified = True
    glob.save_password(PWORD)
    glob.add_permission(["view_fileman"])
    glob.save()

    def _mk_rendition(group, tag):
        fm = FileManager.objects.create(
            name=f"gf_fm_{tag}", backend_type="file", backend_url="file://",
            group=group, is_active=True, is_public=False,
        )
        fm.set_setting("base_path", opts.tmpdir)
        fm.save(update_fields=["mojo_secrets", "modified"])
        fobj = File(
            filename=f"gf_{tag}.txt", content_type="text/plain", category="text",
            file_size=2, file_manager=fm, user=None, group=group,
        )
        fobj.generate_storage_filename()
        fobj.save()
        _write_stub(opts.tmpdir, fobj.storage_file_path)
        rend = FileRendition.objects.create(
            original_file=fobj, role=f"gf_{tag}", filename=f"gf_{tag}.jpg",
            storage_path=f"{opts.tmpdir}/gf_{tag}.jpg", content_type="image/jpeg",
            category="image", upload_status=FileRendition.COMPLETED,
        )
        return rend

    opts.rend_a_id = _mk_rendition(group_a, "a").id
    opts.rend_b_id = _mk_rendition(group_b, "b").id


@th.django_unit_test("GROUP_FIELD: member's BARE list is scoped to their group, no cross-tenant leak")
def test_bare_list_member_scoped(opts):
    """No `?group=`. The member holds view_fileman only at the GroupMember
    level, so the flat check fails and the list fallback must scope by
    original_file__group__in=<member's groups>. Old code: 403 (fallback gated
    on hasattr(cls,"group")). New code: 200 with group A's rendition only."""
    opts.client.logout()
    opts.client.login(MEMBER_USER, PWORD)

    resp = opts.client.get("/api/fileman/rendition?size=500")
    assert_eq(resp.status_code, 200,
              f"member bare list should be 200, got {resp.status_code}: {opts.client.last_response.body}")

    items = getattr(resp.response, "data", None) or []
    ids = {getattr(it, "id", None) for it in items}
    assert_true(opts.rend_a_id in ids,
                f"member must see their own group's rendition; ids={ids}")
    assert_true(opts.rend_b_id not in ids,
                f"SECURITY: foreign group's rendition leaked into member bare list; ids={ids}")


@th.django_unit_test("GROUP_FIELD: member detail on OWN rendition is allowed, FOREIGN is denied")
def test_detail_cross_tenant(opts):
    """Detail goes through _evaluate_permission with an instance. It must
    resolve the instance's tenant via GROUP_FIELD and check membership there —
    not a flat user.has_permission. Own → 200; foreign → denied."""
    opts.client.logout()
    opts.client.login(MEMBER_USER, PWORD)

    resp = opts.client.get(f"/api/fileman/rendition/{opts.rend_a_id}")
    assert_eq(resp.status_code, 200,
              f"member should read OWN rendition detail, got {resp.status_code}: {opts.client.last_response.body}")

    resp = opts.client.get(f"/api/fileman/rendition/{opts.rend_b_id}")
    body = str(opts.client.last_response.body)
    assert_true(resp.status_code in (401, 403, 404),
                f"member must NOT read FOREIGN rendition detail, got {resp.status_code}: {body[:200]}")
    assert_true("gf_b" not in body,
                f"SECURITY: foreign rendition content leaked in detail body: {body[:200]}")


@th.django_unit_test("GROUP_FIELD: a user-level (platform) grant still sees every group")
def test_bare_list_global_sees_all(opts):
    """The system-level semantics are preserved: a holder of view_fileman at
    the USER level is a platform admin for that perm and sees all renditions."""
    opts.client.logout()
    opts.client.login(GLOBAL_USER, PWORD)

    resp = opts.client.get("/api/fileman/rendition?size=500")
    assert_eq(resp.status_code, 200,
              f"global grant list should be 200, got {resp.status_code}: {opts.client.last_response.body}")

    items = getattr(resp.response, "data", None) or []
    ids = {getattr(it, "id", None) for it in items}
    assert_true(opts.rend_a_id in ids and opts.rend_b_id in ids,
                f"user-level grant should see all renditions; ids={ids}")


@th.django_unit_setup()
def cleanup_group_field_scoping(opts):
    from mojo.apps.account.models import User, Group, GroupMember
    from mojo.apps.fileman.models import FileManager, File, FileRendition

    FileRendition.objects.filter(role__startswith="gf_").delete()
    File.objects.filter(filename__startswith="gf_").delete()
    FileManager.objects.filter(name__startswith="gf_fm_").delete()
    GroupMember.objects.filter(group__name__startswith="gf_group_").delete()
    Group.objects.filter(name__startswith="gf_group_").delete()
    User.objects.filter(username__in=[MEMBER_USER, GLOBAL_USER]).delete()
    if getattr(opts, "tmpdir", None) and os.path.exists(opts.tmpdir):
        _shutil.rmtree(opts.tmpdir, ignore_errors=True)
