"""Tests for the per-Group webhook signing secret accessor + REST endpoint."""
from testit import helpers as th


ADMIN_USER = "wsec_admin@test.com"
ADMIN_PWORD = "wsec_admin_pw_99"
NONADMIN_USER = "wsec_nonadmin@test.com"
NONADMIN_PWORD = "wsec_nonadmin_pw_99"
GROUP_NAME_A = "wsec_group_a"
GROUP_NAME_B = "wsec_group_b"


@th.django_unit_setup()
def setup_webhook_secret(opts):
    from mojo.apps.account.models import User, Group, GroupMember, ApiKey

    # Clean up any leftover state so the suite is repeatable on a long-lived DB.
    ApiKey.objects.filter(name__startswith="wsec_").delete()
    Group.objects.filter(name__in=[GROUP_NAME_A, GROUP_NAME_B]).delete()
    User.objects.filter(email__in=[ADMIN_USER, NONADMIN_USER]).delete()

    admin = User.objects.create_user(username=ADMIN_USER, email=ADMIN_USER, password=ADMIN_PWORD)
    admin.is_active = True
    admin.is_email_verified = True
    admin.requires_mfa = False
    admin.save()
    admin.add_permission(["manage_group", "manage_groups"])
    admin.save()
    opts.admin_id = admin.pk

    nonadmin = User.objects.create_user(
        username=NONADMIN_USER, email=NONADMIN_USER, password=NONADMIN_PWORD,
    )
    nonadmin.is_active = True
    nonadmin.is_email_verified = True
    nonadmin.requires_mfa = False
    nonadmin.save()
    # No manage_group / manage_groups — should be denied.
    opts.nonadmin_id = nonadmin.pk

    group_a = Group.objects.create(name=GROUP_NAME_A, kind="organization")
    group_b = Group.objects.create(name=GROUP_NAME_B, kind="organization")
    GroupMember.objects.get_or_create(
        user=admin, group=group_a, defaults={"permissions": {"manage_group": True}},
    )
    opts.group_a_id = group_a.pk
    opts.group_b_id = group_b.pk


# ---------------------------------------------------------------------------
# Model-level accessor behavior
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_get_returns_none_when_no_secret_and_default_no_auto_create(opts):
    """Default get_webhook_secret() must NOT auto-create — verify paths rely on this."""
    from mojo.apps.account.models import Group
    g = Group.objects.get(pk=opts.group_a_id)
    g.set_secret("webhook_secret", None)
    g.save()
    g.refresh_from_db()
    assert g.get_webhook_secret() is None, (
        "Default get_webhook_secret() must return None when no secret is set "
        "(auto_create defaults to False — required for safe verify paths)"
    )


@th.django_unit_test()
def test_auto_create_mints_wsec_prefixed_value(opts):
    """get_webhook_secret(auto_create=True) mints and persists on first read."""
    from mojo.apps.account.models import Group
    g = Group.objects.get(pk=opts.group_a_id)
    g.set_secret("webhook_secret", None)
    g.save()
    g.refresh_from_db()

    secret = g.get_webhook_secret(auto_create=True)
    assert secret is not None, "auto_create=True must mint a secret on first call"
    assert secret.startswith("wsec_"), f"secret must use wsec_ prefix, got: {secret!r}"
    assert len(secret) == len("wsec_") + 48, (
        f"secret must be 'wsec_' + 48 chars (total 53), got len={len(secret)}: {secret!r}"
    )

    g2 = Group.objects.get(pk=opts.group_a_id)
    assert g2.get_webhook_secret() == secret, (
        "After auto_create, a fresh load must return the same persisted secret"
    )


@th.django_unit_test()
def test_rotate_changes_value_preserves_created_at(opts):
    """rotate_webhook_secret() produces a new value but keeps created_at."""
    from mojo.apps.account.models import Group
    g = Group.objects.get(pk=opts.group_a_id)
    g.set_secret("webhook_secret", None)
    g.save()
    g.refresh_from_db()

    info1 = g.get_webhook_secret_info(auto_create=True)
    assert info1.value.startswith("wsec_"), "first mint should produce wsec_ value"
    assert info1.created_at == info1.last_rotated_at, (
        "on first mint, created_at and last_rotated_at must be equal"
    )

    info2 = g.rotate_webhook_secret()
    assert info2.value != info1.value, "rotate must produce a different secret value"
    assert info2.created_at == info1.created_at, (
        "rotate must preserve created_at across rotations"
    )
    assert info2.last_rotated_at >= info1.last_rotated_at, (
        "rotate must advance last_rotated_at (or leave equal if same second)"
    )


@th.django_unit_test()
def test_two_groups_get_distinct_secrets(opts):
    """Different Groups must produce different secrets — no shared key material."""
    from mojo.apps.account.models import Group
    a = Group.objects.get(pk=opts.group_a_id)
    b = Group.objects.get(pk=opts.group_b_id)
    a.set_secret("webhook_secret", None); a.save(); a.refresh_from_db()
    b.set_secret("webhook_secret", None); b.save(); b.refresh_from_db()

    sa = a.get_webhook_secret(auto_create=True)
    sb = b.get_webhook_secret(auto_create=True)
    assert sa is not None and sb is not None, "both groups must mint"
    assert sa != sb, f"two groups must produce distinct secrets, got identical: {sa!r}"


# ---------------------------------------------------------------------------
# REST endpoint
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_rest_empty_body_auto_creates_and_is_idempotent(opts):
    """POST /api/group/webhook_secret with empty body mints once; repeat returns same."""
    from mojo.apps.account.models import Group
    g = Group.objects.get(pk=opts.group_a_id)
    g.set_secret("webhook_secret", None); g.save()

    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "admin login must succeed"

    r1 = opts.client.post("/api/group/webhook_secret", {"group": opts.group_a_id})
    assert r1.status_code == 200, f"first call must 200, got {r1.status_code}: {r1.response}"
    d1 = r1.response.data
    assert d1.secret.startswith("wsec_"), f"secret should be wsec_-prefixed, got: {d1.secret!r}"
    assert len(d1.secret) == 53, f"secret total length must be 53, got {len(d1.secret)}"
    assert d1.created_at == d1.last_rotated_at, (
        "first mint: created_at must equal last_rotated_at"
    )

    r2 = opts.client.post("/api/group/webhook_secret", {"group": opts.group_a_id})
    assert r2.status_code == 200, f"second call must 200, got {r2.status_code}: {r2.response}"
    d2 = r2.response.data
    assert d2.secret == d1.secret, "subsequent reads must return the same secret value"
    assert d2.created_at == d1.created_at, "created_at must be stable across reads"
    assert d2.last_rotated_at == d1.last_rotated_at, (
        "last_rotated_at must be stable across reads"
    )

    opts.client.logout()


@th.django_unit_test()
def test_rest_rotate_returns_new_secret(opts):
    """{rotate: true} produces a new distinct secret; subsequent read returns it."""
    from mojo.apps.account.models import Group
    g = Group.objects.get(pk=opts.group_a_id)
    g.set_secret("webhook_secret", None); g.save()

    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    r1 = opts.client.post("/api/group/webhook_secret", {"group": opts.group_a_id})
    assert r1.status_code == 200, f"prime call must succeed, got {r1.status_code}"
    original = r1.response.data.secret
    original_created = r1.response.data.created_at

    r2 = opts.client.post(
        "/api/group/webhook_secret",
        {"group": opts.group_a_id, "rotate": True},
    )
    assert r2.status_code == 200, f"rotate must 200, got {r2.status_code}: {r2.response}"
    assert r2.response.data.secret != original, (
        f"rotate must produce a different secret, got identical: {original!r}"
    )
    assert r2.response.data.created_at == original_created, (
        "rotate must preserve original created_at"
    )

    r3 = opts.client.post("/api/group/webhook_secret", {"group": opts.group_a_id})
    assert r3.response.data.secret == r2.response.data.secret, (
        "after rotate, a plain read must return the rotated value"
    )
    opts.client.logout()


@th.django_unit_test()
def test_rest_without_manage_group_is_denied(opts):
    """User without manage_group/manage_groups gets 403 (or 401)."""
    opts.client.login(NONADMIN_USER, NONADMIN_PWORD)
    assert opts.client.is_authenticated, "nonadmin login must succeed"

    resp = opts.client.post("/api/group/webhook_secret", {"group": opts.group_a_id})
    assert resp.status_code in (401, 403), (
        f"non-admin must be denied (401/403), got {resp.status_code}: {resp.response}"
    )
    opts.client.logout()


@th.django_unit_test()
def test_rest_two_groups_independent_secrets(opts):
    """Same admin issuing on two groups gets two different secrets back."""
    from mojo.apps.account.models import Group, GroupMember, User
    g_b = Group.objects.get(pk=opts.group_b_id)
    g_b.set_secret("webhook_secret", None); g_b.save()
    admin = User.objects.get(pk=opts.admin_id)
    GroupMember.objects.get_or_create(
        user=admin, group=g_b, defaults={"permissions": {"manage_group": True}},
    )

    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    ra = opts.client.post("/api/group/webhook_secret", {"group": opts.group_a_id})
    rb = opts.client.post("/api/group/webhook_secret", {"group": opts.group_b_id})
    assert ra.status_code == 200 and rb.status_code == 200, (
        f"both calls must succeed, got A={ra.status_code} B={rb.status_code}"
    )
    assert ra.response.data.secret != rb.response.data.secret, (
        "two groups must yield distinct secrets via the REST endpoint"
    )
    opts.client.logout()
