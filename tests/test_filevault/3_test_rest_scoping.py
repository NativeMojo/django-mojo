"""
Regression tests for DM-047 — the three filevault action endpoints
(unlock / retrieve / password) fetched a resource by client-supplied pk with
NO group/owner scoping, gated only by @requires_auth. Any authenticated user
could:
  - POST file/<pk>/unlock   -> mint a cross-tenant download token AND write
                               unlocked_by on another tenant's file
  - POST data/<pk>/retrieve -> read another tenant's decrypted VaultData
  - POST file/<pk>/password -> use another tenant's file as a password oracle

After the fix each endpoint scopes the fetched instance to the caller via
VaultFile/VaultData.rest_check_permission_or_raise(request, "VIEW_PERMS", inst)
— owner-id match, or membership (with the perm) in the row's OWNING group.

The cross-tenant probe here is realistic: the outsider is a legitimate vault
user in their OWN tenant (view_vault scoped to group B via their membership)
but has NO global vault permission and is not a member of group A — so a global
perm (which legitimately bypasses group scoping) is not what's carrying them.
They must be denied on group A's rows; the owner path is unchanged.

Also covers the hardening shipped with the same fix:
  - unlock now requires the file password up front for password-protected files
  - the download-token TTL is clamped to VAULT_TOKEN_MAX_TTL
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true


OWNER_EMAIL = "dm047_owner@test.com"
OWNER_PASSWORD = "dm047_owner_pw_99"
OUTSIDER_EMAIL = "dm047_outsider@test.com"
OUTSIDER_PASSWORD = "dm047_outsider_pw_99"
GROUP_A = "dm047-group-a"
GROUP_B = "dm047-group-b"
FILE_PASSWORD = "filepw#12345"
DATA_SECRET = "sk-DM047-SECRET"


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


def _decode_token_payload(token):
    """Decode the (unverified) payload half of a vault access token."""
    import json
    from base64 import urlsafe_b64decode
    payload_b64 = token.split(".")[0]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    return json.loads(urlsafe_b64decode(payload_b64).decode("utf-8"))


def _ensure_file_manager(FileManager):
    """A filesystem-backed system default FileManager (required by upload_file)."""
    base_path = "/tmp/mojo-fileman-tests"
    sys_manager = FileManager.objects.filter(
        user=None, group=None, is_default=True, is_active=True).first()
    if sys_manager is None or sys_manager.backend_type != FileManager.FILE_SYSTEM:
        sys_manager, _ = FileManager.objects.get_or_create(
            user=None, group=None, name="Test FileManager (file)",
            defaults={
                "backend_type": FileManager.FILE_SYSTEM,
                "backend_url": "file:///",
                "is_default": True,
                "is_active": True,
            })
        sys_manager.backend_type = FileManager.FILE_SYSTEM
        sys_manager.backend_url = "file:///"
        sys_manager.is_default = True
        sys_manager.is_active = True
        sys_manager.set_settings({"base_path": base_path})
        sys_manager.save()
    FileManager.objects.filter(
        user=None, group=None, is_default=True
    ).exclude(pk=sys_manager.pk).update(is_default=False)


@th.django_unit_setup()
def setup_dm047(opts):
    from mojo.apps.account.models import User, Group, GroupMember
    from mojo.apps.fileman.models import FileManager
    from mojo.apps.filevault.services import vault as vault_service

    # clean up first (tests run on a long-lived DB): dropping the groups cascades
    # any VaultFile/VaultData rows (group FK is CASCADE).
    Group.objects.filter(name__in=[GROUP_A, GROUP_B]).delete()
    User.objects.filter(email__in=[OWNER_EMAIL, OUTSIDER_EMAIL]).delete()

    group_a = Group.objects.create(name=GROUP_A, kind="organization")
    group_b = Group.objects.create(name=GROUP_B, kind="organization")
    opts.group_a_id = group_a.pk
    opts.group_b_id = group_b.pk

    # owner (tenant A) — owns the vault rows, authorized via the owner branch.
    owner = User.objects.create_user(
        username=OWNER_EMAIL, email=OWNER_EMAIL, password=OWNER_PASSWORD)
    owner.is_active = True
    owner.is_email_verified = True
    owner.requires_mfa = False
    owner.save()
    opts.owner_id = owner.pk

    # outsider (tenant B) — a real vault user in B, NO global perm, NOT in A.
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

    _ensure_file_manager(FileManager)

    normal = vault_service.upload_file(
        file_obj=_make_file_obj(b"tenant A secret file", "a-file.txt"),
        name="a-file.txt", group=group_a, user=owner)
    opts.file_id = normal.pk

    protected = vault_service.upload_file(
        file_obj=_make_file_obj(b"tenant A protected file", "a-protected.txt"),
        name="a-protected.txt", group=group_a, user=owner, password=FILE_PASSWORD)
    opts.protected_file_id = protected.pk

    data = vault_service.store_data(
        group=group_a, user=owner, name="a-secret",
        data={"api_key": DATA_SECRET, "n": 42})
    opts.data_id = data.pk


# ---------------------------------------------------------------------------
# Cross-tenant denial (THE regression)
# ---------------------------------------------------------------------------

@th.django_unit_test("outsider cannot unlock tenant A's file — no token minted, no unlocked_by write")
def test_outsider_denied_unlock(opts):
    from mojo.apps.filevault.models import VaultFile

    _login(opts, OUTSIDER_EMAIL, OUTSIDER_PASSWORD)
    resp = opts.client.post(f"/api/filevault/file/{opts.file_id}/unlock", {})
    opts.client.logout()

    assert_eq(resp.response.get("code"), 403,
              f"a cross-tenant unlock must be denied 403, got body: {resp.response!r}")
    assert_eq(resp.response.get("status"), False,
              f"a denied unlock must carry status=false, got: {resp.response!r}")
    assert_true(resp.response.get("data") is None,
                f"a denied unlock must not return a token payload, got data: {resp.response.get('data')!r}")
    assert_true("token" not in (resp.body or ""),
                f"a denied unlock response must contain no token, got: {resp.body!r}")

    vf = VaultFile.objects.get(pk=opts.file_id)
    assert_true(vf.unlocked_by_id is None,
                f"a denied cross-tenant unlock must NOT write unlocked_by, but it was set to {vf.unlocked_by_id!r}")


@th.django_unit_test("outsider cannot retrieve tenant A's VaultData plaintext")
def test_outsider_denied_retrieve(opts):
    _login(opts, OUTSIDER_EMAIL, OUTSIDER_PASSWORD)
    resp = opts.client.post(f"/api/filevault/data/{opts.data_id}/retrieve", {})
    opts.client.logout()

    assert_eq(resp.response.get("code"), 403,
              f"a cross-tenant retrieve must be denied 403, got body: {resp.response!r}")
    assert_true(resp.response.get("data") is None,
                f"a denied retrieve must not return decrypted plaintext, got: {resp.response.get('data')!r}")
    assert_true(DATA_SECRET not in (resp.body or ""),
                f"a denied retrieve must not leak the decrypted secret in the body: {resp.body!r}")


@th.django_unit_test("outsider cannot use tenant A's file as a password oracle")
def test_outsider_denied_password(opts):
    _login(opts, OUTSIDER_EMAIL, OUTSIDER_PASSWORD)
    resp = opts.client.post(
        f"/api/filevault/file/{opts.protected_file_id}/password", {"password": "guess"})
    opts.client.logout()

    assert_eq(resp.response.get("code"), 403,
              f"a cross-tenant password check must be denied 403, got body: {resp.response!r}")
    assert_true(resp.response.get("data") is None,
                f"a denied password check must not leak a validity flag, got: {resp.response.get('data')!r}")


# ---------------------------------------------------------------------------
# Owner happy path (must be unchanged)
# ---------------------------------------------------------------------------

@th.django_unit_test("owner can unlock their own file — token minted, unlocked_by recorded")
def test_owner_unlock_succeeds(opts):
    from mojo.apps.filevault.models import VaultFile

    _login(opts, OWNER_EMAIL, OWNER_PASSWORD)
    resp = opts.client.post(f"/api/filevault/file/{opts.file_id}/unlock", {})
    opts.client.logout()

    assert_eq(resp.response.get("code"), 200,
              f"the owner's unlock must succeed, got body: {resp.response!r}")
    token = resp.response.data.get("token")
    assert_true(token, f"a successful unlock must return a token, got: {resp.response.data!r}")

    vf = VaultFile.objects.get(pk=opts.file_id)
    assert_eq(vf.unlocked_by_id, opts.owner_id,
              f"a successful unlock must record unlocked_by=owner, got {vf.unlocked_by_id!r}")


@th.django_unit_test("owner can retrieve their own VaultData plaintext")
def test_owner_retrieve_succeeds(opts):
    _login(opts, OWNER_EMAIL, OWNER_PASSWORD)
    resp = opts.client.post(f"/api/filevault/data/{opts.data_id}/retrieve", {})
    opts.client.logout()

    assert_eq(resp.response.get("code"), 200,
              f"the owner's retrieve must succeed, got body: {resp.response!r}")
    decrypted = resp.response.data.get("data")  # handler returns dict(data=<decrypted>)
    assert_eq(decrypted.get("api_key"), DATA_SECRET,
              f"the owner must get their decrypted payload back, got: {decrypted!r}")


@th.django_unit_test("owner password check returns validity for their own file")
def test_owner_password_succeeds(opts):
    _login(opts, OWNER_EMAIL, OWNER_PASSWORD)
    resp_ok = opts.client.post(
        f"/api/filevault/file/{opts.protected_file_id}/password", {"password": FILE_PASSWORD})
    resp_bad = opts.client.post(
        f"/api/filevault/file/{opts.protected_file_id}/password", {"password": "wrong"})
    opts.client.logout()

    assert_eq(resp_ok.response.data.get("valid"), True,
              f"the correct password must validate, got: {resp_ok.response!r}")
    assert_eq(resp_bad.response.data.get("valid"), False,
              f"a wrong password must not validate, got: {resp_bad.response!r}")


# ---------------------------------------------------------------------------
# Hardening shipped with the fix
# ---------------------------------------------------------------------------

@th.django_unit_test("unlock clamps the download-token TTL to VAULT_TOKEN_MAX_TTL")
def test_unlock_ttl_clamped(opts):
    from mojo.helpers.crypto import vault as crypto_vault

    _login(opts, OWNER_EMAIL, OWNER_PASSWORD)
    resp = opts.client.post(
        f"/api/filevault/file/{opts.file_id}/unlock", {"ttl": 99999999})
    opts.client.logout()

    token = resp.response.data.get("token")
    assert_true(token, f"unlock should return a token, got: {resp.response!r}")
    payload = _decode_token_payload(token)
    lifetime = payload["exp"] - payload["iat"]
    assert_true(lifetime <= crypto_vault.VAULT_TOKEN_MAX_TTL,
                f"token lifetime {lifetime}s must be clamped to <= {crypto_vault.VAULT_TOKEN_MAX_TTL}s")
    assert_eq(resp.response.data.get("ttl"), crypto_vault.VAULT_TOKEN_MAX_TTL,
              f"the response ttl must report the clamped value, got: {resp.response.data.get('ttl')!r}")


@th.django_unit_test("unlock requires the file password up front for password-protected files")
def test_unlock_requires_password(opts):
    _login(opts, OWNER_EMAIL, OWNER_PASSWORD)
    resp_none = opts.client.post(
        f"/api/filevault/file/{opts.protected_file_id}/unlock", {})
    resp_wrong = opts.client.post(
        f"/api/filevault/file/{opts.protected_file_id}/unlock", {"password": "wrong"})
    resp_ok = opts.client.post(
        f"/api/filevault/file/{opts.protected_file_id}/unlock", {"password": FILE_PASSWORD})
    opts.client.logout()

    assert_eq(resp_none.response.get("code"), 403,
              f"unlocking a protected file with no password must be denied, got: {resp_none.response!r}")
    assert_eq(resp_wrong.response.get("code"), 403,
              f"unlocking a protected file with a wrong password must be denied, got: {resp_wrong.response!r}")
    assert_eq(resp_ok.response.get("code"), 200,
              f"unlocking a protected file with the correct password must succeed, got: {resp_ok.response!r}")
    assert_true(resp_ok.response.data.get("token"),
                f"a correct-password unlock must return a token, got: {resp_ok.response.data!r}")
