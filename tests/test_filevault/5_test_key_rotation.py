"""
SECRET_KEY rotation — filevault surfaces, plus the DB-override regression.

Rotation is simulated by patching THIS service module's `crypto_keys`
reference in-process — never via th.server_settings (a stranded SECRET_KEY
override in var/django.conf would break every later boot). The patch is
scoped to mojo.apps.filevault.services.vault, whose only in-process callers
are the test_filevault files, which run sequentially within this package.
"""

import uuid as _uuid
from unittest import mock

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TEST_GROUP = "test_vault_rotation_group"


class FakeKeys:
    """Stands in for mojo.helpers.crypto.keys with a fixed candidate list."""

    def __init__(self, keys):
        self._keys = list(keys)

    def secret_keys(self):
        return list(self._keys)


def _upload(opts, content, name):
    from io import BytesIO
    from mojo.apps.filevault.services import vault as vault_service

    f = BytesIO(content)
    f.name = name
    f.size = len(content)
    f.content_type = "text/plain"
    return vault_service.upload_file(file_obj=f, name=name, group=opts.group)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

@th.django_unit_setup()
def setup_rotation(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.fileman.models import FileManager
    from mojo.apps.filevault.models import VaultFile, VaultData

    group, _ = Group.objects.get_or_create(
        name=TEST_GROUP, defaults={"kind": "organization"})
    opts.group = group

    # long-lived DB: clean up leftovers from previous runs
    VaultFile.objects.filter(group=group).delete()
    VaultData.objects.filter(group=group).delete()

    # ensure a system default filesystem FileManager (same fixture the
    # service tests use) so the module also passes when run alone
    base_path = "/tmp/mojo-fileman-tests"
    sys_manager = FileManager.objects.filter(
        user=None, group=None, is_default=True, is_active=True
    ).first()
    if sys_manager is None or sys_manager.backend_type != FileManager.FILE_SYSTEM:
        sys_manager, _ = FileManager.objects.get_or_create(
            user=None,
            group=None,
            name="Test FileManager (file)",
            defaults={
                "backend_type": FileManager.FILE_SYSTEM,
                "backend_url": "file:///",
                "is_default": True,
                "is_active": True,
            },
        )
        if sys_manager.backend_type != FileManager.FILE_SYSTEM:
            sys_manager.backend_type = FileManager.FILE_SYSTEM
        sys_manager.backend_url = "file:///"
        sys_manager.is_default = True
        sys_manager.is_active = True
        sys_manager.set_settings({"base_path": base_path})
        sys_manager.save()
    FileManager.objects.filter(
        user=None, group=None, is_default=True
    ).exclude(pk=sys_manager.pk).update(is_default=False)


# ---------------------------------------------------------------------------
# ekey wrapping across rotation
# ---------------------------------------------------------------------------

@th.django_unit_test("file wrapped under the old key survives a rotation")
def test_file_unwrap_rotation(opts):
    from mojo.apps.filevault.services import vault as vault_service

    key_a = f"vault-old-key-{_uuid.uuid4().hex}"
    key_b = f"vault-new-key-{_uuid.uuid4().hex}"
    content = b"rotation survival content"

    with mock.patch.object(vault_service, "crypto_keys", FakeKeys([key_a])):
        vault_file = _upload(opts, content, "rotation.txt")

    # rotate: key_b primary, key_a in fallbacks
    with mock.patch.object(vault_service, "crypto_keys", FakeKeys([key_b, key_a])):
        assert_eq(vault_service.download_file(vault_file), content,
                  "download must succeed while the wrapping key is a fallback")
        streamed = b"".join(vault_service.download_file_streaming(vault_file))
        assert_eq(streamed, content,
                  "streaming download must succeed while the wrapping key is a fallback")

    # fallback removed — the file is unreachable (this is the today-behavior
    # the fallback mechanism exists to prevent)
    with mock.patch.object(vault_service, "crypto_keys", FakeKeys([key_b])):
        try:
            vault_service.download_file(vault_file)
            assert False, "download must fail once the wrapping key is neither primary nor fallback"
        except ValueError:
            pass

    opts.key_a = key_a
    opts.key_b = key_b


@th.django_unit_test("new uploads wrap under the primary, never a fallback")
def test_new_wrap_uses_primary(opts):
    from mojo.apps.filevault.services import vault as vault_service
    from mojo.helpers.crypto import vault as crypto_vault

    content = b"fresh post-rotation content"
    with mock.patch.object(
            vault_service, "crypto_keys", FakeKeys([opts.key_b, opts.key_a])):
        vault_file = _upload(opts, content, "post_rotation.txt")

    ekey = crypto_vault.unwrap_ekey(vault_file.ekey, opts.key_b, vault_file.uuid)
    assert_true(bool(ekey), "the new file must unwrap under the primary key directly")
    try:
        crypto_vault.unwrap_ekey(vault_file.ekey, opts.key_a, vault_file.uuid)
        assert False, "the new file must NOT be wrapped under the fallback key"
    except ValueError:
        pass


@th.django_unit_test("VaultData stored under the old key survives a rotation")
def test_data_unwrap_rotation(opts):
    from mojo.apps.filevault.services import vault as vault_service

    key_a = f"data-old-key-{_uuid.uuid4().hex}"
    key_b = f"data-new-key-{_uuid.uuid4().hex}"
    payload = {"token": "rotate-me", "nested": {"n": 7}}

    with mock.patch.object(vault_service, "crypto_keys", FakeKeys([key_a])):
        vault_data = vault_service.store_data(
            group=opts.group, user=None, name="rotation_data", data=payload)

    with mock.patch.object(vault_service, "crypto_keys", FakeKeys([key_b, key_a])):
        decrypted = vault_service.retrieve_data(vault_data)
        assert_eq(decrypted["token"], "rotate-me",
                  "VaultData must decrypt while the wrapping key is a fallback")
        assert_eq(decrypted["nested"]["n"], 7,
                  "nested VaultData content must decrypt intact across the rotation")


# ---------------------------------------------------------------------------
# download tokens across rotation
# ---------------------------------------------------------------------------

@th.django_unit_test("download token signed under the old key survives a rotation")
def test_download_token_rotation(opts):
    from mojo.apps.filevault.services import vault as vault_service

    key_a = f"token-old-key-{_uuid.uuid4().hex}"
    key_b = f"token-new-key-{_uuid.uuid4().hex}"
    ip = "10.7.7.7"

    with mock.patch.object(vault_service, "crypto_keys", FakeKeys([key_a])):
        vault_file = _upload(opts, b"token rotation content", "token_rotation.txt")
        token = vault_service.generate_download_token(vault_file, ip, ttl=300)

    with mock.patch.object(vault_service, "crypto_keys", FakeKeys([key_b, key_a])):
        resolved = vault_service.validate_download_token(token, ip)
        assert_true(resolved is not None,
                    "old-key download token must validate while the key is a fallback")
        assert_eq(resolved.pk, vault_file.pk,
                  "the token must resolve to the file it was minted for")
        new_token = vault_service.generate_download_token(vault_file, ip, ttl=300)

    with mock.patch.object(vault_service, "crypto_keys", FakeKeys([key_b])):
        assert_eq(vault_service.validate_download_token(token, ip), None,
                  "old-key token must stop validating once the fallback is removed")
        resolved = vault_service.validate_download_token(new_token, ip)
        assert_true(resolved is not None and resolved.pk == vault_file.pk,
                    "post-rotation token must validate under the new primary alone — minting must use the primary")


# ---------------------------------------------------------------------------
# DB-override regression: Setting row named SECRET_KEY must be ignored
# ---------------------------------------------------------------------------

@th.django_unit_test("a DB Setting row named SECRET_KEY cannot re-key filevault")
def test_db_setting_row_ignored(opts):
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.filevault.services import vault as vault_service
    from mojo.helpers.settings import settings

    injected = f"attacker-injected-key-{_uuid.uuid4().hex}"
    # long-lived DB: delete before creating
    Setting.remove("SECRET_KEY")
    Setting.set("SECRET_KEY", injected)
    try:
        assert_eq(settings.get("SECRET_KEY", ""), injected,
                  "sanity: the DB-backed read must see the injected row, or this test proves nothing")
        primary = vault_service._secret_keys()[0]
        assert_eq(primary, settings.SECRET_KEY,
                  "filevault's primary must be the file-based SECRET_KEY (get_static path)")
        assert_true(primary != injected,
                    "filevault must ignore the DB Setting row named SECRET_KEY")

        # functional proof: a round-trip during the row's lifetime uses the
        # file-based key, so the row cannot silently re-key new files
        content = b"wrapped while the injected row exists"
        vault_file = _upload(opts, content, "db_override_window.txt")
        assert_eq(vault_service.download_file(vault_file), content,
                  "round-trip must use the file-based key while the row exists")
    finally:
        Setting.remove("SECRET_KEY")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

@th.django_unit_test("cleanup rotation test data")
def test_cleanup(opts):
    from mojo.apps.filevault.models import VaultFile, VaultData

    VaultFile.objects.filter(group=opts.group).delete()
    VaultData.objects.filter(group=opts.group).delete()
    assert_eq(VaultFile.objects.filter(group=opts.group).count(), 0,
              "all rotation-test VaultFiles should be deleted")
    assert_eq(VaultData.objects.filter(group=opts.group).count(), 0,
              "all rotation-test VaultData should be deleted")
