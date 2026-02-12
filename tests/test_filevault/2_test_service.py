"""
Django tests for the filevault service layer and models.

Tests VaultFile/VaultData creation, encryption/decryption via the service,
and access token generation/validation.
"""

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TEST_USER = "testit"
TEST_PWORD = "testit##mojo"


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

@th.django_unit_setup()
def setup_vault_service(opts):
    from mojo.apps.account.models import User, Group

    # ensure test user
    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(username=TEST_USER, display_name=TEST_USER, email=f"{TEST_USER}@example.com")
        user.save()
    user.save_password(TEST_PWORD)
    user.add_permission("view_vault")
    user.add_permission("manage_vault")
    user.save()
    opts.user = user

    # ensure test group
    group, _ = Group.objects.get_or_create(name="test_vault_group", defaults={"kind": "organization"})
    opts.group = group

    # clean up any leftover test data
    from mojo.apps.filevault.models import VaultFile, VaultData
    VaultFile.objects.filter(group=group).delete()
    VaultData.objects.filter(group=group).delete()


# ---------------------------------------------------------------------------
# VaultFile service tests
# ---------------------------------------------------------------------------

@th.django_unit_test("upload_file creates encrypted VaultFile")
def test_upload_file(opts):
    from io import BytesIO
    from mojo.apps.filevault.services import vault as vault_service
    from mojo.apps.filevault.models import VaultFile

    content = b"This is test file content for filevault."
    f = BytesIO(content)
    f.name = "test_doc.txt"
    f.size = len(content)
    f.content_type = "text/plain"

    vault_file = vault_service.upload_file(
        file_obj=f,
        name="test_doc.txt",
        group=opts.group,
        user=opts.user,
        description="Test document",
    )

    assert_true(vault_file.pk is not None, "VaultFile should be saved to DB")
    assert_eq(vault_file.name, "test_doc.txt", "name should match")
    assert_eq(vault_file.content_type, "text/plain", "content_type should match")
    assert_eq(vault_file.size, len(content), "size should match original")
    assert_eq(vault_file.is_encrypted, 2, "should be marked as AES-256-GCM encrypted")
    assert_true(vault_file.uuid, "uuid should be set")
    assert_true(vault_file.ekey, "wrapped ekey should be stored")
    assert_true(vault_file.hashed_password is None, "no password = no hash")
    assert_eq(vault_file.requires_password, False, "requires_password should be False")
    assert_true(vault_file.chunk_count >= 1, "should have at least 1 chunk")

    opts.vault_file_id = vault_file.pk
    opts.vault_file_uuid = vault_file.uuid
    opts.original_content = content


@th.django_unit_test("download_file decrypts correctly")
def test_download_file(opts):
    from mojo.apps.filevault.services import vault as vault_service
    from mojo.apps.filevault.models import VaultFile

    vault_file = VaultFile.objects.get(pk=opts.vault_file_id)
    decrypted = vault_service.download_file(vault_file)
    assert_eq(decrypted, opts.original_content, "decrypted content should match original")


@th.django_unit_test("upload with password and download with correct password")
def test_upload_download_with_password(opts):
    from io import BytesIO
    from mojo.apps.filevault.services import vault as vault_service

    content = b"Password-protected secret content."
    f = BytesIO(content)
    f.name = "secret.txt"
    f.size = len(content)
    f.content_type = "text/plain"

    vault_file = vault_service.upload_file(
        file_obj=f,
        name="secret.txt",
        group=opts.group,
        user=opts.user,
        password="mypassword",
    )

    assert_eq(vault_file.requires_password, True, "should require password")
    assert_true(vault_file.hashed_password is not None, "hashed_password should be set")

    decrypted = vault_service.download_file(vault_file, password="mypassword")
    assert_eq(decrypted, content, "decrypted with correct password should match")

    opts.pw_vault_file_id = vault_file.pk


@th.django_unit_test("download with wrong password fails")
def test_download_wrong_password(opts):
    from mojo.apps.filevault.services import vault as vault_service
    from mojo.apps.filevault.models import VaultFile

    vault_file = VaultFile.objects.get(pk=opts.pw_vault_file_id)
    try:
        vault_service.download_file(vault_file, password="wrongpassword")
        assert False, "should have raised ValueError for wrong password"
    except ValueError as e:
        assert_true("Invalid password" in str(e), "error should mention invalid password")


@th.django_unit_test("download without password when required fails")
def test_download_missing_password(opts):
    from mojo.apps.filevault.services import vault as vault_service
    from mojo.apps.filevault.models import VaultFile

    vault_file = VaultFile.objects.get(pk=opts.pw_vault_file_id)
    try:
        vault_service.download_file(vault_file)
        assert False, "should have raised ValueError for missing password"
    except ValueError as e:
        assert_true("Password required" in str(e), "error should mention password required")


# ---------------------------------------------------------------------------
# Access tokens
# ---------------------------------------------------------------------------

@th.django_unit_test("generate and validate download token")
def test_download_token(opts):
    from mojo.apps.filevault.services import vault as vault_service
    from mojo.apps.filevault.models import VaultFile

    vault_file = VaultFile.objects.get(pk=opts.vault_file_id)
    token = vault_service.generate_download_token(vault_file, "10.0.0.1", ttl=60)
    assert_true(token, "token should be generated")

    resolved = vault_service.validate_download_token(token, "10.0.0.1")
    assert_true(resolved is not None, "token should resolve to a VaultFile")
    assert_eq(resolved.pk, vault_file.pk, "resolved file should match original")


@th.django_unit_test("token rejected for wrong IP")
def test_download_token_wrong_ip(opts):
    from mojo.apps.filevault.services import vault as vault_service
    from mojo.apps.filevault.models import VaultFile

    vault_file = VaultFile.objects.get(pk=opts.vault_file_id)
    token = vault_service.generate_download_token(vault_file, "10.0.0.1")
    resolved = vault_service.validate_download_token(token, "10.0.0.2")
    assert_eq(resolved, None, "wrong IP should invalidate token")


# ---------------------------------------------------------------------------
# VaultData service tests
# ---------------------------------------------------------------------------

@th.django_unit_test("store_data and retrieve_data round-trips")
def test_store_retrieve_data(opts):
    from mojo.apps.filevault.services import vault as vault_service

    secret_data = {"api_key": "sk-12345", "config": {"timeout": 30}}
    vault_data = vault_service.store_data(
        group=opts.group,
        user=opts.user,
        name="api_credentials",
        data=secret_data,
        description="Test API credentials",
    )

    assert_true(vault_data.pk is not None, "VaultData should be saved")
    assert_eq(vault_data.name, "api_credentials", "name should match")
    assert_true(vault_data.ekey, "wrapped ekey should be stored")
    assert_true(vault_data.edata, "encrypted data should be stored")
    assert_eq(vault_data.requires_password, False, "no password required")

    decrypted = vault_service.retrieve_data(vault_data)
    assert_eq(decrypted["api_key"], "sk-12345", "api_key should decrypt correctly")
    assert_eq(decrypted["config"]["timeout"], 30, "nested config should decrypt correctly")

    opts.vault_data_id = vault_data.pk


@th.django_unit_test("store_data with password and retrieve")
def test_store_retrieve_data_with_password(opts):
    from mojo.apps.filevault.services import vault as vault_service

    secret_data = {"secret": "top-secret-value"}
    vault_data = vault_service.store_data(
        group=opts.group,
        user=opts.user,
        name="password_protected_data",
        data=secret_data,
        password="data-password",
    )

    assert_eq(vault_data.requires_password, True, "should require password")

    decrypted = vault_service.retrieve_data(vault_data, password="data-password")
    assert_eq(decrypted["secret"], "top-secret-value", "data should decrypt with password")

    opts.pw_vault_data_id = vault_data.pk


@th.django_unit_test("retrieve_data with wrong password fails")
def test_retrieve_data_wrong_password(opts):
    from mojo.apps.filevault.services import vault as vault_service
    from mojo.apps.filevault.models import VaultData

    vault_data = VaultData.objects.get(pk=opts.pw_vault_data_id)
    try:
        vault_service.retrieve_data(vault_data, password="wrong")
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert_true("Invalid password" in str(e), "error should mention invalid password")


# ---------------------------------------------------------------------------
# Model properties
# ---------------------------------------------------------------------------

@th.django_unit_test("VaultFile fields not exposed in graphs")
def test_vault_file_hidden_fields(opts):
    from mojo.apps.filevault.models import VaultFile

    no_save = VaultFile.RestMeta.NO_SAVE_FIELDS
    assert_true("ekey" in no_save, "ekey should be in NO_SAVE_FIELDS")
    assert_true("hashed_password" in no_save, "hashed_password should be in NO_SAVE_FIELDS")
    assert_true("uuid" in no_save, "uuid should be in NO_SAVE_FIELDS")

    # verify ekey not in any graph fields
    for graph_name, graph in VaultFile.RestMeta.GRAPHS.items():
        fields = graph.get("fields", [])
        extra = graph.get("extra", [])
        assert_true("ekey" not in fields, f"ekey should not be in {graph_name} graph fields")
        assert_true("hashed_password" not in fields, f"hashed_password should not be in {graph_name} graph fields")
        assert_true("ekey" not in extra, f"ekey should not be in {graph_name} graph extra")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

@th.django_unit_test("cleanup test data")
def test_cleanup(opts):
    from mojo.apps.filevault.models import VaultFile, VaultData

    VaultFile.objects.filter(group=opts.group).delete()
    VaultData.objects.filter(group=opts.group).delete()

    remaining_files = VaultFile.objects.filter(group=opts.group).count()
    remaining_data = VaultData.objects.filter(group=opts.group).count()
    assert_eq(remaining_files, 0, "all test VaultFiles should be deleted")
    assert_eq(remaining_data, 0, "all test VaultData should be deleted")
