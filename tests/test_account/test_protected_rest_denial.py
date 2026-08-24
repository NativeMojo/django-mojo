"""Default-tier protected-setting denial contracts (maestro item #2558).

The exploitable surface for protected configuration is the generic
/api/settings REST endpoint; #1839 moved its denial tests opt-in because they
exercised real protected keys. These run the SAME contracts against
TESTIT_PROTECTED_SENTINEL — a reserved key the test project's testit_support
app registers as protected in both processes — so a write can never touch
real configuration and every parallel module is safe. The real-key variants
stay in tests/test_account_admin_extended_serial/test_system_setup.py.
"""
from testit import helpers as th


SENTINEL = "TESTIT_PROTECTED_SENTINEL"
ADMIN_USER = "prd_denial_admin"
ADMIN_EMAIL = "prd_denial_admin@example.com"
ADMIN_PASSWORD = "prdden##mojo99"


@th.django_unit_setup()
def setup_protected_rest_denial(opts):
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import system_settings

    assert system_settings.is_protected_setting(SENTINEL), (
        "the test project's testit_support app must register "
        f"{SENTINEL} as protected — is its AppConfig.ready() wired?"
    )

    # Long-lived DB: clear any sentinel rows a prior interrupted run stranded.
    # The queryset delete bypasses the model-level protected guard on purpose —
    # the row is test-owned and the key is reserved.
    Setting.objects.filter(key=SENTINEL).delete()

    User.objects.filter(username=ADMIN_USER).delete()
    admin = User(username=ADMIN_USER, email=ADMIN_EMAIL,
                 display_name=ADMIN_USER, is_superuser=True)
    admin.save()
    admin.is_email_verified = True
    admin.is_active = True
    admin.save_password(ADMIN_PASSWORD)
    admin.save()
    opts.prd_admin_id = admin.pk


@th.django_unit_test("generic /api/settings POST refuses a protected key")
def test_rest_create_refused(opts):
    from mojo.apps.account.models import Setting

    assert opts.client.login(ADMIN_USER, ADMIN_PASSWORD), "admin login failed"
    try:
        resp = opts.client.post(
            "/api/settings", {"key": SENTINEL, "value": "evil"})
        assert resp.status_code == 403, (
            f"generic Setting REST must refuse a protected key even for a "
            f"superuser, got {resp.status_code}: {opts.client.last_response.body}"
        )
        assert not Setting.objects.filter(key=SENTINEL).exists(), (
            "the denied POST must not persist a row"
        )
    finally:
        opts.client.logout()


@th.django_unit_test("generic /api/settings update and delete refuse an existing protected row")
def test_rest_update_delete_refused(opts):
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import system_settings

    actor = User.objects.get(pk=opts.prd_admin_id)
    value = system_settings.set_value(actor, SENTINEL, "guarded")
    assert value == "guarded", f"dedicated setter must accept the sentinel, got {value!r}"
    row = Setting.objects.get(key=SENTINEL, group=None)

    assert opts.client.login(ADMIN_USER, ADMIN_PASSWORD), "admin login failed"
    try:
        updated = opts.client.post(
            f"/api/settings/{row.pk}", {"value": "changed"})
        assert updated.status_code == 403, (
            f"generic REST must refuse updating a protected row, got "
            f"{updated.status_code}: {opts.client.last_response.body}"
        )
        deleted = opts.client.delete(f"/api/settings/{row.pk}")
        assert deleted.status_code == 403, (
            f"generic REST must refuse deleting a protected row, got "
            f"{deleted.status_code}: {opts.client.last_response.body}"
        )
        row.refresh_from_db()
        assert row.value == "guarded", (
            f"the denied update must not change the stored value, got {row.value!r}"
        )
    finally:
        opts.client.logout()
        Setting.objects.filter(key=SENTINEL).delete()


@th.django_unit_test("model writes refuse the protected key: set, rename-into, delete")
def test_model_layer_refused(opts):
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import system_settings
    from mojo import errors as merrors

    with th.assert_raises(merrors.PermissionDeniedException):
        Setting.set(SENTINEL, "nope")
    assert not Setting.objects.filter(key=SENTINEL).exists(), (
        "refused Setting.set must not persist"
    )

    row = Setting.objects.create(key="TESTIT_PRD_PLAIN", value="ok")
    try:
        row.key = SENTINEL
        with th.assert_raises(merrors.PermissionDeniedException):
            row.save()
    finally:
        # Keyed delete, not pk — the isolation scanner proves safety by the
        # literal reserved key.
        Setting.objects.filter(key="TESTIT_PRD_PLAIN").delete()

    actor = User.objects.get(pk=opts.prd_admin_id)
    system_settings.set_value(actor, SENTINEL, "guarded")
    try:
        # The instance-level guarded.delete() refusal stays in the opt-in
        # original — an instance delete has no key the policy scanner can
        # prove. Setting.remove carries the literal key, so it is the
        # provable half of the same contract.
        with th.assert_raises(merrors.PermissionDeniedException):
            Setting.remove(SENTINEL)
        assert Setting.objects.filter(key=SENTINEL, group=None).exists(), (
            "denied protected removal must not delete the row"
        )
    finally:
        Setting.objects.filter(key=SENTINEL).delete()
