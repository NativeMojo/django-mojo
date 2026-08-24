"""GROUP_FIELD resolving to NULL must clear request.group, not keep the caller's.

`MojoModel._evaluate_permission` re-binds `request.group` to the ROW's owning
tenant before the group-membership check, so a caller-supplied `?group=` cannot
widen access. The GROUP_FIELD branch only re-binds when the resolution is
non-null, so a row whose tenant path yields None leaves the CALLER's group
bound — and a GroupMember-level grant in an unrelated tenant then authorizes
the read. The legacy direct-`group` branch assigns unconditionally and fails
closed.

These tests pin the intended contract (documented in
docs/django_developer/rest/permissions.md: "a null link along the way yields
'no group' (fail-closed — the flat user/superuser check still applies)"):

  - a null-tenant row is NOT readable on a GroupMember-level grant, even with
    ?group=<a group the caller legitimately belongs to>;
  - a GLOBAL grant still reads it (the fix must not over-restrict);
  - normal same-tenant reads are unaffected.

Covers both GROUP_FIELD shapes: a direct field (PublicMessage, "group") and a
related path (FileRendition, "original_file__group").
"""
import os
import tempfile
import shutil as _shutil

from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "core"  # #2792 tier curation

MEMBER_USER = "gfnull_member@example.com"
GLOBAL_USER = "gfnull_global@example.com"
PWORD = "gfnull##mojo99"

PM_PATH = "/api/account/public_message"
REND_PATH = "/api/fileman/rendition"


def _write_stub(tmpdir, storage_path, data=b"hi"):
    full = os.path.join(tmpdir, storage_path.lstrip("/"))
    os.makedirs(os.path.dirname(full) or tmpdir, exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(data)


@th.django_unit_setup()
def setup_group_field_null(opts):
    from mojo.apps.account.models import User, Group, GroupMember, PublicMessage
    from mojo.apps.fileman.models import FileManager, File, FileRendition
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip="127.0.0.1", key="login")

    # Long-lived DB: delete what this setup is about to create.
    FileRendition.objects.filter(role__startswith="gfnull_").delete()
    File.objects.filter(filename__startswith="gfnull_").delete()
    FileManager.objects.filter(name__startswith="gfnull_fm_").delete()
    PublicMessage.objects.filter(email__startswith="gfnull-msg").delete()
    GroupMember.objects.filter(group__name__startswith="gfnull_group").delete()
    Group.objects.filter(name__startswith="gfnull_group").delete()
    User.objects.filter(username__in=[MEMBER_USER, GLOBAL_USER]).delete()

    opts.tmpdir = tempfile.mkdtemp(prefix="mojo_gfnull_")

    group_a = Group.objects.create(name="gfnull_group_a", kind="organization", is_active=True)
    opts.group_a_id = group_a.id

    # GroupMember-level grants ONLY — nothing at the user level. This is the
    # identity the null-tenant rows must not be reachable by.
    member = User.objects.create_user(
        username=MEMBER_USER, email=MEMBER_USER, password=PWORD)
    gm = GroupMember.objects.create(user=member, group=group_a, is_active=True)
    gm.add_permission(["view_support", "view_fileman"])
    opts.member_id = member.id

    # USER-level (platform) grants — must still reach the null-tenant rows.
    glob = User.objects.create_user(
        username=GLOBAL_USER, email=GLOBAL_USER, password=PWORD)
    glob.add_permission(["view_support", "view_fileman"])
    glob.save()

    # --- PublicMessage: GROUP_FIELD = "group" (direct field) ---
    opts.pm_null_id = PublicMessage.objects.create(
        kind="contact_us", name="NullTenant",
        email="gfnull-msg-null@example.com",
        message="SENTINEL_NULL_TENANT_BODY",
    ).id
    opts.pm_owned_id = PublicMessage.objects.create(
        kind="contact_us", group=group_a, name="OwnedByA",
        email="gfnull-msg-owned@example.com",
        message="owned by group a",
    ).id

    # --- FileRendition: GROUP_FIELD = "original_file__group" (related path) ---
    def _mk_rendition(group, tag):
        fm = FileManager.objects.create(
            name=f"gfnull_fm_{tag}", backend_type="file", backend_url="file://",
            group=group, is_active=True, is_public=False,
        )
        fm.set_setting("base_path", opts.tmpdir)
        fm.save(update_fields=["mojo_secrets", "modified"])
        fobj = File(
            filename=f"gfnull_{tag}.txt", content_type="text/plain", category="text",
            file_size=2, file_manager=fm, user=None, group=group,
        )
        fobj.generate_storage_filename()
        fobj.save()
        _write_stub(opts.tmpdir, fobj.storage_file_path)
        return FileRendition.objects.create(
            original_file=fobj, role=f"gfnull_{tag}", filename=f"gfnull_{tag}.jpg",
            storage_path=f"{opts.tmpdir}/gfnull_{tag}.jpg", content_type="image/jpeg",
            category="image", upload_status=FileRendition.COMPLETED,
        )

    # user=None on the null-tenant file: File.VIEW_PERMS contains "owner" and
    # the owner branch returns BEFORE the group re-bind, so a caller-owned
    # fixture would pass for a reason unrelated to the branch under test.
    opts.rend_null_id = _mk_rendition(None, "null").id
    opts.rend_owned_id = _mk_rendition(group_a, "owned").id


@th.django_unit_test("GROUP_FIELD null: member-level grant + ?group= must NOT read a null-tenant PublicMessage")
def test_null_tenant_public_message_denied(opts):
    """The caller holds view_support only as a GroupMember of group A, and
    supplies ?group=<group A>. The row belongs to NO tenant, so the re-bind must
    clear request.group and drop the check to the caller's GLOBAL perms — which
    they do not have. Denied."""
    opts.client.logout()
    opts.client.login(MEMBER_USER, PWORD)

    resp = opts.client.get(f"{PM_PATH}/{opts.pm_null_id}?group={opts.group_a_id}")
    body = str(opts.client.last_response.body)
    assert_true(
        resp.status_code in (401, 403, 404),
        f"SECURITY: group-member-only grant read a NULL-tenant PublicMessage with "
        f"?group=<own group>; expected 401/403/404, got {resp.status_code}: {body[:300]}",
    )
    assert_true(
        "SENTINEL_NULL_TENANT_BODY" not in body,
        f"SECURITY: null-tenant message body leaked in the refusal: {body[:300]}",
    )


@th.django_unit_test("GROUP_FIELD null (related path): member-level grant + ?group= must NOT read a null-tenant rendition")
def test_null_tenant_rendition_denied(opts):
    """Same contract through a related GROUP_FIELD path ("original_file__group")
    whose first hop is non-null but whose final hop is None."""
    opts.client.logout()
    opts.client.login(MEMBER_USER, PWORD)

    resp = opts.client.get(f"{REND_PATH}/{opts.rend_null_id}?group={opts.group_a_id}")
    body = str(opts.client.last_response.body)
    assert_true(
        resp.status_code in (401, 403, 404),
        f"SECURITY: group-member-only grant read a NULL-tenant FileRendition with "
        f"?group=<own group>; expected 401/403/404, got {resp.status_code}: {body[:300]}",
    )
    assert_true(
        "gfnull_null" not in body,
        f"SECURITY: null-tenant rendition content leaked in the refusal: {body[:300]}",
    )


@th.django_unit_test("GROUP_FIELD null: a GLOBAL grant still reads the null-tenant rows (no over-restriction)")
def test_null_tenant_readable_by_global_grant(opts):
    """The anti-over-restriction control. Clearing request.group must drop the
    check to the flat user/superuser check — NOT deny outright. A platform
    holder of view_support / view_fileman keeps full visibility. This must pass
    both before and after the fix."""
    opts.client.logout()
    opts.client.login(GLOBAL_USER, PWORD)

    resp = opts.client.get(f"{PM_PATH}/{opts.pm_null_id}")
    assert_eq(
        resp.status_code, 200,
        f"global view_support must still read the null-tenant PublicMessage, "
        f"got {resp.status_code}: {opts.client.last_response.body}",
    )

    resp = opts.client.get(f"{REND_PATH}/{opts.rend_null_id}")
    assert_eq(
        resp.status_code, 200,
        f"global view_fileman must still read the null-tenant FileRendition, "
        f"got {resp.status_code}: {opts.client.last_response.body}",
    )


@th.django_unit_test("GROUP_FIELD null: a member still reads their OWN tenant's rows")
def test_own_tenant_still_readable_by_member(opts):
    """The other anti-over-restriction control: the non-null path is untouched,
    so a GroupMember-level grant keeps working for rows that DO belong to their
    tenant. Must pass both before and after the fix."""
    opts.client.logout()
    opts.client.login(MEMBER_USER, PWORD)

    resp = opts.client.get(f"{PM_PATH}/{opts.pm_owned_id}?group={opts.group_a_id}")
    assert_eq(
        resp.status_code, 200,
        f"member must still read their OWN group's PublicMessage, "
        f"got {resp.status_code}: {opts.client.last_response.body}",
    )

    resp = opts.client.get(f"{REND_PATH}/{opts.rend_owned_id}?group={opts.group_a_id}")
    assert_eq(
        resp.status_code, 200,
        f"member must still read their OWN group's FileRendition, "
        f"got {resp.status_code}: {opts.client.last_response.body}",
    )


@th.django_unit_test("FK attach to a null-tenant row must not strip the caller's group from the new row")
def test_fk_attach_to_null_tenant_keeps_caller_group(opts):
    """Attaching a tenant-less FK must not silently create a tenant-less row.

    `_evaluate_permission` re-binds `request.group` to the TARGET row's owning
    tenant as a side effect, and the create-time auto-stamp reads
    `request.group` AFTER the field loop has run every FK-attach VIEW check. So
    a create that attaches an FK whose target has no tenant would leave the new
    row with `group = NULL` instead of the caller's group — silently, with a
    200. `on_rest_save` snapshots and restores the caller's group around the
    loop (the same thing `on_rest_handle_batch` does between rows).

    Uses the GLOBAL-grant user: the point is the group STAMP, not the
    permission decision, so the FK attach itself must succeed.
    """
    from mojo.apps.account.models import User
    from mojo.apps.shortlink.models import ShortLink

    ShortLink.objects.filter(source="gfnull_fk_attach").delete()
    glob = User.objects.get(username=GLOBAL_USER)
    glob.add_permission(["manage_shortlinks"])
    glob.save()

    opts.client.login(GLOBAL_USER, PWORD)
    resp = opts.client.post(
        "/api/shortlink/link",
        {
            "url": "https://example.com/gfnull-fk-attach",
            "source": "gfnull_fk_attach",
            "rendition": opts.rend_null_id,
            "group": opts.group_a_id,
        },
    )
    assert resp.status_code == 200, \
        f"creating a shortlink with a null-tenant rendition FK should succeed " \
        f"for a global grant, got {resp.status_code}: {resp.response}"

    link = ShortLink.objects.filter(source="gfnull_fk_attach").first()
    assert link is not None, "the shortlink should have been created"
    assert link.group_id == opts.group_a_id, (
        f"the new row must keep the CALLER's group ({opts.group_a_id}); the "
        f"null-tenant rendition FK must not clear it mid-save. "
        f"Got group_id={link.group_id!r}"
    )
    ShortLink.objects.filter(source="gfnull_fk_attach").delete()


@th.django_unit_test("GROUP_FIELD null: without ?group= the member is denied before and after")
def test_null_tenant_denied_without_group_param(opts):
    """Pins that the fix only bites when a group was actually supplied — with no
    ?group= there is nothing bound to clear, and the member was already denied."""
    opts.client.logout()
    opts.client.login(MEMBER_USER, PWORD)

    resp = opts.client.get(f"{PM_PATH}/{opts.pm_null_id}")
    assert_true(
        resp.status_code in (401, 403, 404),
        f"member with no ?group= must not read the null-tenant PublicMessage, "
        f"got {resp.status_code}: {opts.client.last_response.body}",
    )


@th.django_unit_setup()
def cleanup_group_field_null(opts):
    from mojo.apps.account.models import User, Group, GroupMember, PublicMessage
    from mojo.apps.fileman.models import FileManager, File, FileRendition

    FileRendition.objects.filter(role__startswith="gfnull_").delete()
    File.objects.filter(filename__startswith="gfnull_").delete()
    FileManager.objects.filter(name__startswith="gfnull_fm_").delete()
    PublicMessage.objects.filter(email__startswith="gfnull-msg").delete()
    GroupMember.objects.filter(group__name__startswith="gfnull_group").delete()
    Group.objects.filter(name__startswith="gfnull_group").delete()
    User.objects.filter(username__in=[MEMBER_USER, GLOBAL_USER]).delete()
    if getattr(opts, "tmpdir", None) and os.path.exists(opts.tmpdir):
        _shutil.rmtree(opts.tmpdir, ignore_errors=True)
