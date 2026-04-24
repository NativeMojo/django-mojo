"""Tests for routing fileman URLs through mojo.apps.shortlink.

Covers:
  - Tier 1 (internal/display): auto-created shortlink per File and per Rendition.
  - Tier 2 (share): on-demand mint via `{"share": ...}` action, attributed + optional tracking.
  - Toggles: global FILEMAN_USE_SHORTLINKS, per-FileManager use_shortlinks, precedence.
  - Public vs private backends (no recursion in resolver).
  - Revocation: is_active=False regenerates; row-deleted regenerates.
  - Discrete regenerate_renditions action shape (the fix).
  - Delete cascade scoped to source="fileman"/"fileman-share".
"""
import os
import tempfile
import shutil as _shutil

from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_USER = "fileman_sl_user"
TEST_USER_2 = "fileman_sl_user2"
TEST_PWORD = "fileman##mojo99"


def _write_file(tmpdir, storage_file_path, data):
    full_path = os.path.join(tmpdir, storage_file_path.lstrip('/'))
    os.makedirs(os.path.dirname(full_path) or tmpdir, exist_ok=True)
    with open(full_path, 'wb') as fh:
        fh.write(data)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

@th.django_unit_setup()
def setup_shortlink_urls(opts):
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import FileManager, File, FileRendition
    from mojo.apps.shortlink.models import ShortLink
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    u1 = User.objects.filter(username=TEST_USER).last()
    if u1 is None:
        u1 = User(username=TEST_USER, email=f"{TEST_USER}@example.com")
        u1.save()
    u1.is_email_verified = True
    u1.save_password(TEST_PWORD)
    u1.add_permission(["view_fileman", "manage_files"])
    u1.save()
    opts.user = u1

    u2 = User.objects.filter(username=TEST_USER_2).last()
    if u2 is None:
        u2 = User(username=TEST_USER_2, email=f"{TEST_USER_2}@example.com")
        u2.save()
    u2.is_email_verified = True
    u2.save_password(TEST_PWORD)
    u2.add_permission(["view_fileman", "manage_files"])
    u2.save()
    opts.user2 = u2

    tmpdir = tempfile.mkdtemp(prefix="mojo_sl_test_")
    opts.tmpdir = tmpdir

    fm = FileManager.objects.filter(name="test_sl_fm", user=u1).first()
    if fm is None:
        fm = FileManager(
            name="test_sl_fm",
            backend_type="file",
            backend_url="file://",
            user=u1,
            is_active=True,
            is_default=False,
            is_public=False,  # private so resolver returns a *new* signed URL
        )
        fm.save()
    fm.backend_url = "file://"
    fm.is_public = False
    fm.is_active = True
    fm.save()
    fm.set_setting("base_path", tmpdir)
    # Default: do not force per-manager toggle. Tests twiddle as needed.
    fm.set_setting("use_shortlinks", None)
    fm.set_setting("shortlink_track_clicks", None)
    fm.set_setting("shortlink_expire_days", None)
    fm.save(update_fields=["mojo_secrets", "modified"])
    opts.fm_id = fm.pk

    # A public-backend FileManager for the public-path test.
    pub = FileManager.objects.filter(name="test_sl_fm_public", user=u1).first()
    if pub is None:
        pub = FileManager(
            name="test_sl_fm_public",
            backend_type="file",
            backend_url="file://",
            user=u1,
            is_active=True,
            is_default=False,
            is_public=True,
        )
        pub.save()
    pub.is_public = True
    pub.save()
    pub.set_setting("base_path", tmpdir)
    pub.save(update_fields=["mojo_secrets", "modified"])
    opts.fm_pub_id = pub.pk

    File.objects.filter(user__in=[u1, u2]).delete()
    FileRendition.objects.all().delete()
    ShortLink.objects.filter(source__in=["fileman", "fileman-share"]).delete()


def _mk_file(opts, filename="tier1.txt", file_manager_id=None, content=b"hi"):
    from mojo.apps.fileman.models import FileManager, File
    fm = FileManager.objects.get(pk=file_manager_id or opts.fm_id)
    f = File(filename=filename, content_type="text/plain", category="text",
             file_size=len(content), file_manager=fm, user=opts.user)
    f.generate_storage_filename()
    f.save()
    _write_file(opts.tmpdir, f.storage_file_path, content)
    f.mark_as_completed(commit=True)
    f.refresh_from_db()
    return f


# ---------------------------------------------------------------------------
# Tier 1 — internal/display shortlink
# ---------------------------------------------------------------------------

@th.django_unit_test("Tier1: generate_download_url returns a /s/<code> shortlink")
def test_tier1_returns_shortlink(opts):
    from mojo.apps.shortlink.models import ShortLink

    f = _mk_file(opts, "tier1_basic.txt")
    url = f.generate_download_url()
    assert_true("/s/" in (url or ""),
                f"expected short URL containing /s/, got {url!r}")
    code = url.rstrip("/").rsplit("/", 1)[-1]
    assert_true(len(code) >= 5, f"code should be non-empty, got {code!r}")

    # A ShortLink row exists linked to this file with source="fileman".
    sl = ShortLink.objects.filter(code=code).first()
    assert_true(sl is not None, f"ShortLink row for code {code} should exist")
    assert_eq(sl.file_id, f.id, f"ShortLink.file should point at file {f.id}")
    assert_eq(sl.source, "fileman", f"tier-1 source should be 'fileman', got {sl.source}")
    assert_eq(sl.resolve_file, True, "resolve_file should be True for dynamic presigns")
    assert_eq(sl.bot_passthrough, False, "bot_passthrough defaults to False for file links")

    f.refresh_from_db()
    assert_eq(f.shortlink_code, code, f"file should cache shortlink_code={code}")
    opts.tier1_code = code
    opts.tier1_file_id = f.id


@th.django_unit_test("Tier1: second call returns same URL, no duplicate row")
def test_tier1_idempotent(opts):
    from mojo.apps.fileman.models import File
    from mojo.apps.shortlink.models import ShortLink

    f = File.objects.get(pk=opts.tier1_file_id)
    url1 = f.generate_download_url()
    url2 = f.generate_download_url()
    assert_eq(url1, url2, "tier-1 URL should be stable across reads")
    rows = ShortLink.objects.filter(file_id=f.id, source="fileman")
    assert_eq(rows.count(), 1, f"exactly one tier-1 shortlink expected, got {rows.count()}")


@th.django_unit_test("Tier1: rendition gets its own shortlink distinct from file")
def test_tier1_rendition_distinct(opts):
    from mojo.apps.fileman.models import File, FileRendition
    from mojo.apps.shortlink.models import ShortLink

    f = File.objects.get(pk=opts.tier1_file_id)
    # Fabricate a rendition row (no need to actually render in this test).
    r = FileRendition.objects.create(
        original_file=f,
        role="thumbnail_test",
        filename="thumb.jpg",
        storage_path=f"{opts.tmpdir}/thumb.jpg",
        content_type="image/jpeg",
        category="image",
        upload_status=FileRendition.COMPLETED,
    )
    # Drop a matching bytes file so the backend can serve it if needed.
    _write_file(opts.tmpdir, r.storage_path, b"stub")

    r_url = r.generate_download_url()
    f_url = f.generate_download_url()
    assert_true(r_url != f_url, f"rendition URL {r_url!r} should differ from file URL {f_url!r}")
    r_code = r_url.rstrip("/").rsplit("/", 1)[-1]

    sl = ShortLink.objects.filter(code=r_code).first()
    assert_true(sl is not None, f"rendition ShortLink should exist for code {r_code}")
    assert_eq(sl.rendition_id, r.id, "ShortLink should be linked to the rendition (not file)")
    assert_true(sl.file_id is None, "rendition tier-1 shortlink should NOT set file FK")
    opts.rendition_id = r.id
    opts.rendition_code = r_code


# ---------------------------------------------------------------------------
# Toggles — global + per-FileManager
# ---------------------------------------------------------------------------

@th.django_unit_test("Toggle: per-FileManager use_shortlinks=False returns direct URL")
def test_toggle_fm_off(opts):
    from mojo.apps.fileman.models import FileManager
    from mojo.apps.shortlink.models import ShortLink

    # Flip per-manager toggle OFF.
    fm = FileManager.objects.get(pk=opts.fm_id)
    fm.set_setting("use_shortlinks", False)
    fm.save(update_fields=["mojo_secrets", "modified"])

    f = _mk_file(opts, "tier1_off.txt")
    url = f.generate_download_url()
    assert_true("/s/" not in (url or ""),
                f"disabled → direct URL, got {url!r}")
    assert_true(
        not ShortLink.objects.filter(file=f, source="fileman").exists(),
        "no tier-1 shortlink should be created when per-manager toggle is False",
    )

    # Restore toggle for subsequent tests.
    fm.set_setting("use_shortlinks", None)
    fm.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("Toggle: per-FileManager True overrides global False")
def test_toggle_fm_true_wins(opts):
    from mojo.apps.fileman.models import FileManager
    from mojo.apps.shortlink.models import ShortLink
    from unittest import mock

    fm = FileManager.objects.get(pk=opts.fm_id)
    fm.set_setting("use_shortlinks", True)  # per-manager ON
    fm.save(update_fields=["mojo_secrets", "modified"])

    # Simulate global OFF via settings.get returning False. Patching the helper
    # module's settings import is simplest (and works because generate_download_url
    # is invoked in-process — no cross-process server isolation).
    from mojo.apps.fileman.models import file as file_module
    with mock.patch.object(file_module, "_shortlink_installed", return_value=True):
        with mock.patch("mojo.helpers.settings.settings.get") as mget:
            def _fake_get(k, default=None, **kwargs):
                if k == "FILEMAN_USE_SHORTLINKS":
                    return False
                return default
            mget.side_effect = _fake_get
            f = _mk_file(opts, "tier1_fm_wins.txt")
            url = f.generate_download_url()
    assert_true("/s/" in (url or ""),
                f"per-manager True should override global False; got {url!r}")
    assert_true(
        ShortLink.objects.filter(file=f, source="fileman").exists(),
        "shortlink row should be created when per-manager wins",
    )

    fm.set_setting("use_shortlinks", None)
    fm.save(update_fields=["mojo_secrets", "modified"])


# ---------------------------------------------------------------------------
# Public vs private backend — resolver must not recurse
# ---------------------------------------------------------------------------

@th.django_unit_test("Public FileManager: shortlink created; resolver returns CDN URL (no recursion)")
def test_public_manager_no_recursion(opts):
    from mojo.apps.shortlink.models import ShortLink

    f = _mk_file(opts, "tier1_public.txt", file_manager_id=opts.fm_pub_id)
    url = f.generate_download_url()
    assert_true("/s/" in (url or ""),
                f"public-manager files still get shortlinks per user preference; got {url!r}")
    sl = ShortLink.objects.filter(file=f, source="fileman").first()
    assert_true(sl is not None, "tier-1 shortlink row expected for public manager")

    # Calling resolver should NOT recurse back into generate_download_url() —
    # it must go through get_direct_download_url(). Verify it returns a string
    # that does NOT contain /s/ (i.e., the raw backend URL).
    resolved = sl.resolve()
    assert_true(resolved is not None, "resolver should return a URL, got None")
    assert_true("/s/" not in resolved,
                f"resolver should return the direct URL, got {resolved!r}")


# ---------------------------------------------------------------------------
# Revocation — is_active=False and hard-delete both trigger regenerate
# ---------------------------------------------------------------------------

@th.django_unit_test("Revocation: is_active=False triggers regenerate on next call")
def test_revocation_is_active(opts):
    from mojo.apps.fileman.models import File
    from mojo.apps.shortlink.models import ShortLink

    f = _mk_file(opts, "tier1_revoke.txt")
    url1 = f.generate_download_url()
    code1 = url1.rstrip("/").rsplit("/", 1)[-1]

    ShortLink.objects.filter(code=code1).update(is_active=False)

    url2 = f.generate_download_url()
    code2 = url2.rstrip("/").rsplit("/", 1)[-1]
    assert_true(code1 != code2, f"revoked shortlink should regenerate, got same code {code1}")
    assert_true(
        ShortLink.objects.filter(code=code2, is_active=True).exists(),
        "new shortlink should be active",
    )


@th.django_unit_test("Revocation: hard-deleted shortlink triggers regenerate")
def test_revocation_hard_delete(opts):
    from mojo.apps.fileman.models import File
    from mojo.apps.shortlink.models import ShortLink

    f = _mk_file(opts, "tier1_delete.txt")
    url1 = f.generate_download_url()
    code1 = url1.rstrip("/").rsplit("/", 1)[-1]

    ShortLink.objects.filter(code=code1).delete()

    url2 = f.generate_download_url()
    code2 = url2.rstrip("/").rsplit("/", 1)[-1]
    assert_true(code1 != code2,
                f"hard-deleted shortlink should regenerate, got same code {code1}")


# ---------------------------------------------------------------------------
# Tier 2 — share action
# ---------------------------------------------------------------------------

@th.django_unit_test("Tier2: {\"share\": true} mints a share shortlink via REST")
def test_share_action_minimal(opts):
    from mojo.apps.fileman.models import File
    from mojo.apps.shortlink.models import ShortLink

    f = _mk_file(opts, "share_min.txt")
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post(f"/api/fileman/file/{f.id}", {"share": True})
    assert_eq(resp.status_code, 200,
              f"share action should return 200, got {resp.status_code} {getattr(resp, 'response', None)}")
    data = resp.response
    assert_true(data.url and "/s/" in data.url,
                f"share response should include a /s/ URL, got {data!r}")
    assert_true(data.shortlink_code, "share response should include shortlink_code")
    assert_eq(data.track_clicks, False, "track_clicks should default to False")
    assert_true(data.expires_at is None, f"never-expire by default, got {data.expires_at!r}")

    sl = ShortLink.objects.filter(code=data.shortlink_code).first()
    assert_true(sl is not None, "share shortlink row should exist")
    assert_eq(sl.source, "fileman-share", f"share source should be 'fileman-share', got {sl.source}")
    assert_eq(sl.file_id, f.id, "share shortlink should be linked to file")
    assert_eq(sl.user_id, opts.user.id, "share shortlink should be attributed to the sharer")


@th.django_unit_test("Tier2: share dict carries expire_days, track_clicks, note")
def test_share_action_options(opts):
    from mojo.apps.shortlink.models import ShortLink

    f = _mk_file(opts, "share_opts.txt")
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post(
        f"/api/fileman/file/{f.id}",
        {"share": {"expire_days": 7, "track_clicks": True, "note": "hello"}},
    )
    assert_eq(resp.status_code, 200, f"got {resp.status_code}")
    data = resp.response
    assert_eq(data.track_clicks, True, "track_clicks should be True")
    assert_true(data.expires_at is not None, "expires_at should be set when expire_days > 0")

    sl = ShortLink.objects.get(code=data.shortlink_code)
    assert_eq(sl.track_clicks, True, "row track_clicks should be True")
    assert_eq(sl.metadata.get("note"), "hello", f"note should be stored in metadata, got {sl.metadata}")


@th.django_unit_test("Tier2: two different users produce two distinct attributed shares")
def test_share_per_user_attribution(opts):
    from mojo.apps.shortlink.models import ShortLink

    f = _mk_file(opts, "share_multi.txt")

    opts.client.login(TEST_USER, TEST_PWORD)
    r1 = opts.client.post(f"/api/fileman/file/{f.id}", {"share": True})
    assert_eq(r1.status_code, 200, "first share 200")
    code1 = r1.response.shortlink_code

    opts.client.logout()
    opts.client.login(TEST_USER_2, TEST_PWORD)
    r2 = opts.client.post(f"/api/fileman/file/{f.id}", {"share": True})
    assert_eq(r2.status_code, 200, "second share 200")
    code2 = r2.response.shortlink_code

    assert_true(code1 != code2, "two shares should produce distinct codes")

    sl1 = ShortLink.objects.get(code=code1)
    sl2 = ShortLink.objects.get(code=code2)
    assert_eq(sl1.user_id, opts.user.id, "first share attributed to user 1")
    assert_eq(sl2.user_id, opts.user2.id, "second share attributed to user 2")


@th.django_unit_test("Tier2: expire_days clamped to MAX_SHARE_EXPIRE_DAYS")
def test_share_expire_days_clamped(opts):
    from mojo.apps.fileman.models import File

    f = _mk_file(opts, "share_clamp.txt")
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post(
        f"/api/fileman/file/{f.id}",
        {"share": {"expire_days": 99999}},
    )
    assert_eq(resp.status_code, 200, f"got {resp.status_code}")
    # Not raising is the main check; expires_at exists and is within ~MAX days.
    from mojo.apps.shortlink.models import ShortLink
    sl = ShortLink.objects.get(code=resp.response.shortlink_code)
    assert_true(sl.expires_at is not None, "expires_at should be set")
    # Clamp is 3650 days; use a loose upper-bound check.
    from mojo.helpers import dates
    years = (sl.expires_at - dates.utcnow()).days
    assert_true(years <= 3651, f"expire_days should be clamped, got ~{years}d remaining")


# ---------------------------------------------------------------------------
# Tier 2 — rendition share
# ---------------------------------------------------------------------------

@th.django_unit_test("Tier2: rendition share action mints a rendition-linked shortlink")
def test_share_rendition(opts):
    from mojo.apps.shortlink.models import ShortLink
    from mojo.apps.fileman.models import FileRendition
    # Reuse the rendition created earlier.
    r = FileRendition.objects.get(pk=opts.rendition_id)

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post(f"/api/fileman/rendition/{r.id}", {"share": True})
    assert_eq(resp.status_code, 200, f"got {resp.status_code} {getattr(resp, 'response', None)}")
    data = resp.response
    sl = ShortLink.objects.get(code=data.shortlink_code)
    assert_eq(sl.rendition_id, r.id, "share shortlink should be linked to rendition")
    assert_true(sl.file_id is None, "rendition share should not set file FK")
    assert_eq(sl.source, "fileman-share", f"source should be 'fileman-share', got {sl.source}")


# ---------------------------------------------------------------------------
# Regenerate action shape fix
# ---------------------------------------------------------------------------

@th.django_unit_test("Action shape: {\"regenerate_renditions\": true} enqueues job")
def test_regenerate_discrete_true(opts):
    from mojo.apps.jobs.models import Job

    f = _mk_file(opts, "regen_true.txt")
    before = Job.objects.filter(
        func="mojo.apps.fileman.asyncjobs.regenerate_renditions",
        payload__file_id=f.id,
    ).count()

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post(f"/api/fileman/file/{f.id}", {"regenerate_renditions": True})
    assert_eq(resp.status_code, 200, f"got {resp.status_code} {getattr(resp, 'response', None)}")

    after = Job.objects.filter(
        func="mojo.apps.fileman.asyncjobs.regenerate_renditions",
        payload__file_id=f.id,
    ).count()
    assert_eq(after - before, 1, "one regenerate job should be enqueued")


@th.django_unit_test("Action shape: {\"regenerate_renditions\": [\"thumbnail\"]} passes roles")
def test_regenerate_discrete_roles(opts):
    from mojo.apps.jobs.models import Job

    f = _mk_file(opts, "regen_roles.txt")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post(
        f"/api/fileman/file/{f.id}",
        {"regenerate_renditions": ["thumbnail"]},
    )
    assert_eq(resp.status_code, 200, f"got {resp.status_code}")

    job = (
        Job.objects
        .filter(func="mojo.apps.fileman.asyncjobs.regenerate_renditions")
        .filter(payload__file_id=f.id)
        .order_by("-created")
        .first()
    )
    assert_true(job is not None, "regenerate job should be enqueued")
    assert_eq(job.payload.get("roles"), ["thumbnail"],
              f"roles filter should be in payload, got {job.payload}")


@th.django_unit_test("Action shape: {\"action\": \"regenerate_renditions\"} is no longer recognized")
def test_regenerate_legacy_shape_dropped(opts):
    from mojo.apps.jobs.models import Job

    f = _mk_file(opts, "regen_legacy.txt")
    before = Job.objects.filter(
        func="mojo.apps.fileman.asyncjobs.regenerate_renditions",
        payload__file_id=f.id,
    ).count()

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post(
        f"/api/fileman/file/{f.id}",
        {"action": "regenerate_renditions"},
    )
    # The request succeeds (the legacy key just no-ops for this verb).
    assert_eq(resp.status_code, 200, f"got {resp.status_code}")
    after = Job.objects.filter(
        func="mojo.apps.fileman.asyncjobs.regenerate_renditions",
        payload__file_id=f.id,
    ).count()
    assert_eq(after - before, 0,
              "legacy {action: regenerate_renditions} must no longer dispatch")


@th.django_unit_test("Regression: legacy {\"action\": \"mark_as_completed\"} still works")
def test_mark_completed_legacy_preserved(opts):
    from mojo.apps.fileman.models import FileManager, File

    fm = FileManager.objects.get(pk=opts.fm_id)
    # Create an uploading file, then mark completed via the legacy action shape.
    f = File(filename="legacy_mark.txt", content_type="text/plain", category="text",
             file_size=4, file_manager=fm, user=opts.user)
    f.generate_storage_filename()
    f.save()
    _write_file(opts.tmpdir, f.storage_file_path, b"test")
    f.mark_as_uploading(commit=True)

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post(f"/api/fileman/file/{f.id}", {"action": "mark_as_completed"})
    assert_eq(resp.status_code, 200, f"got {resp.status_code}")

    f.refresh_from_db()
    assert_eq(f.upload_status, File.COMPLETED, "file should be COMPLETED via legacy action")


# ---------------------------------------------------------------------------
# Delete cascade — scoped to source
# ---------------------------------------------------------------------------

@th.django_unit_test("Delete: deleting File drops tier-1 + tier-2 shortlinks, preserves others")
def test_delete_scoped_cleanup(opts):
    from mojo.apps.shortlink.models import ShortLink
    from mojo.apps.fileman.models import File

    f = _mk_file(opts, "delete_scope.txt")
    # Tier 1 — implicit via generate_download_url during mark_as_completed → the
    # file's URL isn't actually read, so force creation now.
    f.generate_download_url()
    # Tier 2 — mint a share link.
    opts.client.login(TEST_USER, TEST_PWORD)
    opts.client.post(f"/api/fileman/file/{f.id}", {"share": True})

    # A separate human-created shortlink (source="manual") linked to the same file.
    manual = ShortLink.create(url="https://example.com/manual", source="manual",
                              expire_days=0, file=f, user=opts.user)

    # Count before delete.
    assert_true(
        ShortLink.objects.filter(file=f, source="fileman").exists(),
        "precondition: tier-1 shortlink exists",
    )
    assert_true(
        ShortLink.objects.filter(file=f, source="fileman-share").exists(),
        "precondition: tier-2 share shortlink exists",
    )

    # Delete the file — triggers on_rest_pre_delete cleanup.
    f.on_rest_pre_delete()

    # Auto-generated rows should already be gone (cleanup ran inside pre_delete),
    # regardless of the subsequent delete() call. Check BEFORE delete() so we
    # are not confused by SET_NULL on the FK field.
    remaining_auto = ShortLink.objects.filter(
        file=f, source__in=["fileman", "fileman-share"]
    )
    assert_eq(remaining_auto.count(), 0,
              f"auto-generated shortlinks must be deleted by on_rest_pre_delete, "
              f"got {[(s.code, s.source) for s in remaining_auto]}")

    manual_id = manual.pk
    f.delete()

    # Manual shortlink survives (file FK goes NULL via SET_NULL).
    assert_true(
        ShortLink.objects.filter(pk=manual_id).exists(),
        "human-created (source='manual') shortlink should survive file delete",
    )


# ---------------------------------------------------------------------------
# Security regressions (from review of f5bf944)
# ---------------------------------------------------------------------------

@th.django_unit_test("Security: rendition shortlinks are cleaned up on File delete")
def test_rendition_shortlinks_cleaned_on_file_delete(opts):
    """Before the fix, File.on_rest_pre_delete only deleted shortlinks with
    `file=<self>`. Rendition shortlinks have `file=NULL` + `rendition=<id>`,
    so they were orphaned as inert rows after SET_NULL cascade."""
    from mojo.apps.fileman.models import FileRendition
    from mojo.apps.shortlink.models import ShortLink

    f = _mk_file(opts, "rendition_orphan.txt")
    r = FileRendition.objects.create(
        original_file=f,
        role="thumb_orphan",
        filename="orphan.jpg",
        storage_path=f"{opts.tmpdir}/orphan.jpg",
        content_type="image/jpeg",
        category="image",
        upload_status=FileRendition.COMPLETED,
    )
    _write_file(opts.tmpdir, r.storage_path, b"stub")

    r.generate_download_url()  # tier-1 rendition shortlink
    opts.client.login(TEST_USER, TEST_PWORD)
    opts.client.post(f"/api/fileman/rendition/{r.id}", {"share": True})  # tier-2

    assert_true(
        ShortLink.objects.filter(rendition=r, source="fileman").exists(),
        "precondition: tier-1 rendition shortlink exists",
    )
    assert_true(
        ShortLink.objects.filter(rendition=r, source="fileman-share").exists(),
        "precondition: tier-2 rendition share exists",
    )

    f.on_rest_pre_delete()

    remaining = ShortLink.objects.filter(
        rendition=r, source__in=["fileman", "fileman-share"]
    )
    assert_eq(remaining.count(), 0,
              f"rendition shortlinks must be deleted when File is deleted, "
              f"got {[(s.code, s.source) for s in remaining]}")


@th.django_unit_test("Security: rendition list endpoint is group-scoped via GROUP_FIELD")
def test_rendition_list_group_scoped(opts):
    """FileRendition has no direct `group` FK. Without `GROUP_FIELD` pointing
    through the parent File, a request scoped to group A would still return
    group B's renditions. Verify that when `request.group` is set (via the
    `group=<id>` query param picked up by the auth decorator), the list
    filters to that group via `original_file__group=request.group`."""
    from mojo.apps.account.models import Group
    from mojo.apps.fileman.models import File, FileManager, FileRendition

    # Group A — the caller's scope.
    group_a = Group.objects.filter(name="rendition_scope_a").first()
    if group_a is None:
        group_a = Group(name="rendition_scope_a")
        group_a.save()

    # Group B — a different group; its renditions must not leak.
    group_b = Group.objects.filter(name="rendition_scope_b").first()
    if group_b is None:
        group_b = Group(name="rendition_scope_b")
        group_b.save()

    def _mk_rendition_in_group(group, role, filename):
        fm = FileManager.objects.filter(
            name=f"test_sl_scope_fm_{group.name}"
        ).first()
        if fm is None:
            fm = FileManager(
                name=f"test_sl_scope_fm_{group.name}",
                backend_type="file",
                backend_url="file://",
                group=group,
                is_active=True,
                is_default=False,
                is_public=False,
            )
            fm.save()
        fm.set_setting("base_path", opts.tmpdir)
        fm.save(update_fields=["mojo_secrets", "modified"])

        fobj = File(
            filename=filename, content_type="text/plain", category="text",
            file_size=2, file_manager=fm, user=None, group=group,
        )
        fobj.generate_storage_filename()
        fobj.save()
        _write_file(opts.tmpdir, fobj.storage_file_path, b"hi")
        r = FileRendition.objects.create(
            original_file=fobj,
            role=role,
            filename=f"{role}.jpg",
            storage_path=f"{opts.tmpdir}/{role}.jpg",
            content_type="image/jpeg",
            category="image",
            upload_status=FileRendition.COMPLETED,
        )
        return fobj, fm, r

    file_a, fm_a, rend_a = _mk_rendition_in_group(group_a, "scope_a_thumb", "scope_a.txt")
    file_b, fm_b, rend_b = _mk_rendition_in_group(group_b, "scope_b_thumb", "scope_b.txt")

    opts.client.logout()
    opts.client.login(TEST_USER, TEST_PWORD)
    # Passing `group=<group_a.id>` sets request.group=group_a via the auth
    # decorator; GROUP_FIELD then filters the queryset.
    resp = opts.client.get(f"/api/fileman/rendition?group={group_a.id}&size=500")
    assert_eq(resp.status_code, 200, f"list 200, got {resp.status_code}")

    items = getattr(resp.response, "data", None) or []
    returned_ids = {getattr(it, "id", None) for it in items}
    assert_true(rend_a.id in returned_ids,
                f"group A's rendition should appear; returned_ids={returned_ids}")
    assert_true(rend_b.id not in returned_ids,
                f"group B's rendition MUST NOT leak to a group-A-scoped list; "
                f"returned_ids={returned_ids}")

    # Cleanup
    rend_a.delete(); rend_b.delete()
    file_a.delete(); file_b.delete()
    fm_a.delete(); fm_b.delete()
    group_a.delete(); group_b.delete()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

@th.django_unit_setup()
def cleanup_shortlink_tests(opts):
    from mojo.apps.fileman.models import FileManager, File, FileRendition
    from mojo.apps.shortlink.models import ShortLink

    File.objects.filter(user__in=[opts.user, opts.user2]).delete()
    FileRendition.objects.all().delete()
    FileManager.objects.filter(pk__in=[opts.fm_id, opts.fm_pub_id]).delete()
    ShortLink.objects.filter(source__in=["fileman", "fileman-share", "manual"]).delete()

    if os.path.exists(opts.tmpdir):
        _shutil.rmtree(opts.tmpdir, ignore_errors=True)
