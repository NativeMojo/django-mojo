"""VaultAccessLog — the "who opened this secret" trail, plus two live bugs.

Before this, a secrets vault kept only `VaultFile.unlocked_by`: the LAST
unlocker, overwritten each time. A successful download left no trace at all,
and a password brute-force left none either (those raise a plain ValueException
and take the generic error branch, not the denial branch that emits an
incident).

Also covers two defects found while scoping:
  - validate_access_token's IP binding silently evaporated when BOTH the
    minted and the presented IP were None (`None != None` is False).
  - download_file_streaming contained a `yield`, making it a GENERATOR
    function, so its password check never ran at call time — the caller's
    `except ValueError` was dead code and a wrong password produced a
    truncated body under a full Content-Length instead of a 403.
"""

TESTIT_TIER = "bug"
from testit import helpers as th
from testit.helpers import assert_eq, assert_true

OWNER_EMAIL = "vlog_owner@test.com"
OWNER_PASSWORD = "vlog_owner_pw_99"
OUTSIDER_EMAIL = "vlog_outsider@test.com"
OUTSIDER_PASSWORD = "vlog_outsider_pw_99"
GROUP_A = "vlog-group-a"
GROUP_B = "vlog-group-b"
FILE_PASSWORD = "vlogpw#12345"
DATA_SECRET = "sk-VLOG-SECRET"


def _login(opts, email, password):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="login")
    ok = opts.client.login(email, password)
    assert ok, f"login failed for {email}: {opts.client.last_response.body}"


def _make_file_obj(content, name):
    from io import BytesIO
    f = BytesIO(content)
    f.name = name
    f.size = len(content)
    f.content_type = "text/plain"
    return f


def _logs(opts, **kw):
    from mojo.apps.filevault.models import VaultAccessLog
    return VaultAccessLog.objects.filter(**kw)


@th.django_unit_setup()
def setup_vault_access_log(opts):
    from mojo.apps.account.models import User, Group, GroupMember
    from mojo.apps.fileman.models import FileManager
    from mojo.apps.filevault.services import vault as vault_service
    import sys
    # Reuse the FileManager helper from the DM-047 suite rather than
    # duplicating it; both need the same filesystem-backed default.
    scoping = sys.modules.get("tests.test_filevault.3_test_rest_scoping")

    Group.objects.filter(name__in=[GROUP_A, GROUP_B]).delete()
    User.objects.filter(email__in=[OWNER_EMAIL, OUTSIDER_EMAIL]).delete()

    group_a = Group.objects.create(name=GROUP_A, kind="organization")
    group_b = Group.objects.create(name=GROUP_B, kind="organization")
    opts.group_a_id = group_a.pk

    owner = User.objects.create_user(
        username=OWNER_EMAIL, email=OWNER_EMAIL, password=OWNER_PASSWORD)
    owner.is_active = True
    owner.is_email_verified = True
    owner.requires_mfa = False
    owner.save()
    opts.owner_id = owner.pk

    outsider = User.objects.create_user(
        username=OUTSIDER_EMAIL, email=OUTSIDER_EMAIL, password=OUTSIDER_PASSWORD)
    outsider.is_active = True
    outsider.is_email_verified = True
    outsider.requires_mfa = False
    outsider.save()
    opts.outsider_id = outsider.pk
    ms_b = GroupMember(user=outsider, group=group_b)
    ms_b.save()
    ms_b.add_permission("view_vault")

    if scoping is not None:
        scoping._ensure_file_manager(FileManager)
    else:
        base_path = "/tmp/mojo-fileman-tests"
        fm, _ = FileManager.objects.get_or_create(
            user=None, group=None, name="Test FileManager (file)",
            defaults={"backend_type": FileManager.FILE_SYSTEM,
                      "backend_url": "file:///", "is_default": True,
                      "is_active": True})
        fm.backend_type = FileManager.FILE_SYSTEM
        fm.backend_url = "file:///"
        fm.is_default = True
        fm.is_active = True
        fm.set_settings({"base_path": base_path})
        fm.save()

    normal = vault_service.upload_file(
        file_obj=_make_file_obj(b"tenant A secret file", "vlog-file.txt"),
        name="vlog-file.txt", group=group_a, user=owner)
    opts.file_id = normal.pk

    protected = vault_service.upload_file(
        file_obj=_make_file_obj(b"tenant A protected", "vlog-protected.txt"),
        name="vlog-protected.txt", group=group_a, user=owner,
        password=FILE_PASSWORD)
    opts.protected_file_id = protected.pk

    data = vault_service.store_data(
        group=group_a, user=owner, name="vlog-secret",
        data={"api_key": DATA_SECRET, "n": 42})
    opts.data_id = data.pk


# ---------------------------------------------------------------------------
# The trail
# ---------------------------------------------------------------------------

@th.django_unit_test("a successful unlock is recorded with actor, tenant and ip")
def test_unlock_granted_recorded(opts):
    before = _logs(opts, vault_file_id=opts.file_id, action="unlock").count()

    _login(opts, OWNER_EMAIL, OWNER_PASSWORD)
    resp = opts.client.post(f"/api/filevault/file/{opts.file_id}/unlock", {})
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"owner unlock should succeed: {resp.body}")

    rows = _logs(opts, vault_file_id=opts.file_id, action="unlock", result="granted")
    assert_eq(rows.count(), before + 1,
              "exactly one granted unlock row must be written")
    row = rows.order_by("-created").first()
    assert_eq(row.user_id, opts.owner_id, "the row must name the accessor")
    assert_eq(row.group_id, opts.group_a_id,
              "the row must be scoped to the FILE's tenant, not request.group")
    assert_eq(row.target_name, "vlog-file.txt",
              "target_name is denormalized so the trail survives file deletion")
    assert_true(row.ip, "the accessor's ip must be recorded")


@th.django_unit_test("a cross-tenant denial is recorded in the VICTIM's trail")
def test_cross_tenant_denial_recorded(opts):
    from mojo.apps.filevault.models import VaultFile

    # Captured before the probe: an earlier test in this module unlocks the
    # same file legitimately, so the invariant is "the denial does not TOUCH
    # unlocked_by", not "unlocked_by is null".
    before_unlocked_by = VaultFile.objects.get(pk=opts.file_id).unlocked_by_id

    _login(opts, OUTSIDER_EMAIL, OUTSIDER_PASSWORD)
    resp = opts.client.post(f"/api/filevault/file/{opts.file_id}/unlock", {})
    opts.client.logout()
    assert_eq(resp.response.get("code"), 403, "cross-tenant unlock must be denied")

    rows = _logs(opts, vault_file_id=opts.file_id, action="unlock", result="denied",
                 reason="permission_denied")
    assert_true(rows.exists(),
                "a denied unlock must leave a row — 'who tried to open my secret' "
                "is the question this trail exists to answer")
    row = rows.order_by("-created").first()
    assert_eq(row.user_id, opts.outsider_id, "the row must name the prober")
    assert_eq(row.group_id, opts.group_a_id,
              "the denial belongs to the TARGET's tenant, not the prober's")

    # DM-047 invariant must still hold: a denied unlock writes nothing.
    vf = VaultFile.objects.get(pk=opts.file_id)
    assert_eq(vf.unlocked_by_id, before_unlocked_by,
              "a denied cross-tenant unlock must not touch unlocked_by")
    assert_true(vf.unlocked_by_id != opts.outsider_id,
                "the prober must never end up recorded as the unlocker")


@th.django_unit_test("a wrong file password is recorded — the brute-force signal")
def test_bad_password_recorded(opts):
    _login(opts, OWNER_EMAIL, OWNER_PASSWORD)
    resp = opts.client.post(
        f"/api/filevault/file/{opts.protected_file_id}/unlock",
        {"password": "wrong-password"})
    opts.client.logout()
    assert_eq(resp.response.get("code"), 403, "a wrong password must be refused")

    rows = _logs(opts, vault_file_id=opts.protected_file_id, action="unlock",
                 result="denied", reason="invalid_password")
    assert_true(rows.exists(),
                "a wrong-password unlock must leave a row: it raises a plain "
                "ValueException and so emits no permission-denied incident")


@th.django_unit_test("a VaultData retrieve is recorded and stores no plaintext")
def test_retrieve_recorded_without_plaintext(opts):
    _login(opts, OWNER_EMAIL, OWNER_PASSWORD)
    resp = opts.client.post(f"/api/filevault/data/{opts.data_id}/retrieve", {})
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"owner retrieve should succeed: {resp.body}")

    rows = _logs(opts, vault_data_id=opts.data_id, action="retrieve", result="granted")
    assert_true(rows.exists(), "a successful retrieve must be recorded")

    for row in _logs(opts, vault_data_id=opts.data_id):
        blob = f"{row.target_name}{row.reason}{row.user_agent}"
        assert_true(DATA_SECRET not in blob,
                    "the audit row must never contain the secret it records access to")


@th.django_unit_test("the trail is tenant-scoped and read-only over REST")
def test_access_log_rest_surface(opts):
    _login(opts, OUTSIDER_EMAIL, OUTSIDER_PASSWORD)
    resp = opts.client.get("/api/filevault/accesslog")
    if resp.status_code == 200:
        for item in (resp.response.data or []):
            assert_true(item.get("id") is None or True, "")
        ids = [i.get("id") for i in (resp.response.data or [])]
        a_ids = set(_logs(opts, group_id=opts.group_a_id).values_list("id", flat=True))
        assert_true(not (set(ids) & a_ids),
                    "a tenant-B member must not see tenant A's access rows")

    # Writes are refused whatever the caller's permissions.
    resp = opts.client.post("/api/filevault/accesslog", {"action": "unlock"})
    assert_true(resp.status_code in (401, 403, 404, 405),
                f"the access log must be append-only via record(), got {resp.status_code}")
    opts.client.logout()


# ---------------------------------------------------------------------------
# The two defects found while scoping
# ---------------------------------------------------------------------------

@th.tier("core")
@th.django_unit_test("IP binding fails closed when either side has no IP")
def test_token_ip_binding_fails_closed(opts):
    """`None != None` is False, so an IP-less mint used to validate for any
    other IP-less caller — the binding silently evaporated."""
    from mojo.helpers.crypto import vault as crypto_vault

    secret = "vlog-test-secret-key"
    token = crypto_vault.generate_access_token(42, None, secret, ttl=60)
    assert_true(crypto_vault.validate_access_token(token, None, secret) is None,
                "a token minted with no IP must never validate — before the fix "
                "None != None was False and the binding was skipped entirely")
    assert_true(crypto_vault.validate_access_token(token, "1.2.3.4", secret) is None,
                "an IP-less token must not validate for a real IP either")

    bound = crypto_vault.generate_access_token(42, "1.2.3.4", secret, ttl=60)
    assert_true(crypto_vault.validate_access_token(bound, None, secret) is None,
                "a bound token must not validate for an IP-less caller")
    assert_eq(crypto_vault.validate_access_token(bound, "1.2.3.4", secret), 42,
              "the normal bound case must still work")


@th.django_unit_test("download_file_streaming raises eagerly, not mid-stream")
def test_streaming_password_error_is_eager(opts):
    """It used to contain the `yield` directly, so calling it ran none of the
    body: the password check never fired at call time, the caller's
    `except ValueError` was dead, and the client got a truncated file under a
    full Content-Length instead of a 403."""
    from mojo.apps.filevault.models import VaultFile
    from mojo.apps.filevault.services import vault as vault_service

    vf = VaultFile.objects.get(pk=opts.protected_file_id)
    try:
        vault_service.download_file_streaming(vf, password="wrong-password")
    except ValueError:
        return
    assert False, (
        "download_file_streaming must raise AT CALL TIME for a bad password — "
        "if it only raises during iteration the response headers are already "
        "sent and the client silently receives a truncated file")


@th.django_unit_test("cleanup")
def test_vault_access_log_cleanup(opts):
    from mojo.apps.account.models import User, Group

    opts.client.logout()
    Group.objects.filter(name__in=[GROUP_A, GROUP_B]).delete()
    User.objects.filter(email__in=[OWNER_EMAIL, OUTSIDER_EMAIL]).delete()
