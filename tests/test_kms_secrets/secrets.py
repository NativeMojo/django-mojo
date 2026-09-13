"""KMS failures must never turn existing encrypted secrets into an empty store.

Every helper belongs to one model instance. No shared KMS implementation,
settings, caches, or model classes are patched by these parallel-safe tests.
"""
from testit import helpers as th

TESTIT_TIER = "core"
PROVIDER_DETAIL = "TEST_ONLY_PROVIDER_DETAIL_PRIVATE_KEY"


class _FakeKMS:
    def __init__(self):
        self.blobs = {}
        self.decrypt_calls = []
        self.encrypt_calls = []
        self.unavailable = False

    def encrypt_field(self, context, data):
        from copy import deepcopy
        self.encrypt_calls.append((context, deepcopy(dict(data))))
        if self.unavailable:
            raise ValueError(PROVIDER_DETAIL)
        blob = f"test-kms-envelope-{len(self.blobs) + 1}"
        self.blobs[blob] = (context, deepcopy(dict(data)))
        return blob

    def decrypt_dict_field(self, context, blob):
        from copy import deepcopy
        self.decrypt_calls.append((context, blob))
        if self.unavailable:
            raise ValueError(PROVIDER_DETAIL)
        stored_context, data = self.blobs[blob]
        if context != stored_context:
            raise ValueError(PROVIDER_DETAIL)
        return deepcopy(data)


def _row(pk=3255001, **kwargs):
    from mojo.apps.dnsman.models import AcmeAccount
    row = AcmeAccount(pk=pk, **kwargs)
    kms = _FakeKMS()
    row._get_kms = lambda: kms
    row.debug_calls = []
    row.debug = lambda *args: row.debug_calls.append(args)
    return row, kms


def _seed(row, kms, data=None):
    blob = kms.encrypt_field(
        row._kms_context(), data if data is not None else {"original": "keep-me"})
    row.mojo_secrets = blob
    kms.encrypt_calls.clear()
    return blob


def _unavailable(call):
    import traceback
    try:
        call()
    except RuntimeError as error:
        from mojo.models.secrets import SecretsUnavailableError
        assert isinstance(error, SecretsUnavailableError), \
            "Secret custody failures must have the dedicated RuntimeError subtype"
        rendered = "".join(traceback.format_exception(error))
        assert PROVIDER_DETAIL not in rendered, \
            "Public exception and traceback must not expose provider details"
        return error
    raise AssertionError("An unreadable encrypted store must raise, never return empty")


def _saved_row(label):
    import contextlib
    from mojo.apps.dnsman.models import AcmeAccount

    @contextlib.contextmanager
    def owned():
        url = f"https://kms-secrets-{label}.invalid/directory"
        AcmeAccount.objects.filter(directory_url=url).delete()
        row, kms = _row(pk=None, directory_url=url)
        try:
            yield row, kms
        finally:
            AcmeAccount.objects.filter(directory_url=url).delete()
    return owned()


@th.django_unit_test("KMS read outage preserves ciphertext and retries after recovery")
def test_read_outage_and_recovery(opts):
    row, kms = _row()
    original = _seed(row, kms)
    kms.unavailable = True
    _unavailable(lambda: row.get_secret("original", "default"))
    assert row.mojo_secrets == original, "Failed read must preserve the exact blob"
    assert row._exposed_secrets is None, "Failed read must not cache an empty mapping"
    _unavailable(lambda: row.secrets)
    assert len(kms.decrypt_calls) == 2, "Each failed read must remain retryable"
    kms.unavailable = False
    assert row.get_secret("original") == "keep-me", "Recovered custody must expose original data"
    assert row.get_secret("missing", "fallback") == "fallback", "Readable stores retain get defaults"
    assert len(kms.decrypt_calls) == 3, "Successful reads should reuse their plaintext cache"
    assert not kms.encrypt_calls, "Reading secrets must not encrypt or replace them"


@th.django_unit_test("KMS setters cannot replace secrets after a failed read")
def test_failed_setters_preserve_blob(opts):
    for setter in (lambda row: row.set_secret("new", "value"),
                   lambda row: row.set_secrets({"new": "value"})):
        row, kms = _row()
        original = _seed(row, kms)
        kms.unavailable = True
        _unavailable(lambda: setter(row))
        row.save_secrets()
        assert row.mojo_secrets == original, "A failed setter must not dirty or erase the blob"
        assert not kms.encrypt_calls, "A failed setter must never encrypt replacement data"


@th.django_unit_test("A dirty unread encrypted store refuses save")
def test_dirty_unread_save_guard(opts):
    from objict import objict
    for exposed in (None, objict(), objict(replacement="unsafe")):
        row, kms = _row()
        original = _seed(row, kms)
        row._exposed_secrets = exposed
        row._secrets_changed = True
        _unavailable(row.save_secrets)
        assert row.mojo_secrets == original, "Unread dirty state must preserve ciphertext"
        assert not kms.encrypt_calls, "Guard must refuse before encrypting unread replacement data"


@th.django_unit_test("Replacing ciphertext invalidates its clean plaintext cache")
def test_clean_ciphertext_replacement(opts):
    row, kms = _row()
    _seed(row, kms, {"version": "first"})
    assert row.get_secret("version") == "first", "Fixture must load its first envelope"
    replacement = _seed(row, kms, {"version": "second"})
    assert row.get_secret("version") == "second", "Replaced ciphertext must not expose stale plaintext"
    row.set_secret("added", True)
    row.save_secrets()
    assert row.mojo_secrets != replacement, "A loaded replacement can be updated"
    assert kms.decrypt_dict_field(row._kms_context(), row.mojo_secrets) == {
        "version": "second", "added": True,
    }, "Updates must preserve replacement contents, not resurrect the previous blob"


@th.django_unit_test("Replacing ciphertext cannot authorize saving a dirty stale cache")
def test_dirty_ciphertext_replacement(opts):
    row, kms = _row()
    _seed(row, kms)
    row.set_secret("new", "unsaved")
    replacement = _seed(row, kms, {"different": "preserve-this"})
    _unavailable(row.save_secrets)
    assert row.mojo_secrets == replacement, "Dirty old plaintext must not overwrite a new envelope"
    assert not kms.encrypt_calls, "Ciphertext replacement must invalidate old save authorization"


@th.django_unit_test("Unsaved instances with ciphertext are never treated as empty stores")
def test_unsaved_nonnull_blob(opts):
    row, kms = _row(pk=None, mojo_secrets="existing-ciphertext")
    _unavailable(lambda: row.secrets)
    _unavailable(lambda: row.set_secret("new", "unsafe"))
    row._secrets_changed = True
    _unavailable(row.save_secrets)
    assert row.mojo_secrets == "existing-ciphertext", "Missing identity cannot authorize replacement"
    assert not kms.encrypt_calls, "An unidentified existing blob must never be overwritten"


@th.django_unit_test("Explicit clear remains available when KMS cannot decrypt")
def test_explicit_clear_and_new_store(opts):
    row, kms = _row()
    _seed(row, kms)
    kms.unavailable = True
    _unavailable(lambda: row.secrets)
    row.clear_secrets()
    row.save_secrets()
    assert row.mojo_secrets is None, "Explicit clear must remove even unreadable ciphertext"
    assert not row.secrets, "Explicitly cleared store must read as empty"
    assert not kms.encrypt_calls, "Clearing must not require KMS encryption"
    kms.unavailable = False
    row.set_secret("fresh", "value")
    row.save_secrets()
    assert kms.decrypt_dict_field(row._kms_context(), row.mojo_secrets) == {"fresh": "value"}, \
        "An explicitly cleared store must allow fresh secrets"


@th.django_unit_test("New secret stores and merges persist without logging plaintext")
def test_new_save_and_merge(opts):
    with _saved_row("create") as (row, kms):
        row.set_secrets({"original": "keep-me", "nested": {"first": 1}})
        assert not row.debug_calls, "Setting secrets must never debug-log the plaintext input"
        row.save()
        assert row.pk is not None, "New secret-bearing models must receive a database identity"
        assert kms.encrypt_calls[0][0] == row._kms_context(), \
            "Encryption must bind the allocated primary key"
        row.set_secrets('{"nested": {"second": 2}, "added": "value"}')
        row.save()
        row.refresh_from_db()
        assert row.secrets == {
            "original": "keep-me", "nested": {"first": 1, "second": 2}, "added": "value",
        }, "Normal merges and updates must preserve unrelated secrets"
        assert not row.debug_calls, "String-shaped secret input must not be logged either"


@th.django_unit_test("Refresh discards stale plaintext and old dirty authorization")
def test_refresh_reloads_ciphertext(opts):
    from mojo.apps.dnsman.models import AcmeAccount
    with _saved_row("refresh") as (row, kms):
        row.set_secret("version", "first")
        row.save()
        row.set_secret("local", "discard-on-refresh")
        replacement = kms.encrypt_field(row._kms_context(), {"version": "second"})
        AcmeAccount.objects.filter(pk=row.pk).update(mojo_secrets=replacement)
        row.refresh_from_db()
        assert row.mojo_secrets == replacement, "Refresh must fetch persisted replacement ciphertext"
        assert row.get_secret("version") == "second", "Refresh must invalidate old plaintext"
        assert row.get_secret("local") is None, "Refresh must discard unsaved plaintext edits"
        row.set_secret("preserved", True)
        row.save()
        row.refresh_from_db()
        assert row.secrets == {"version": "second", "preserved": True}, \
            "Loaded refreshed ciphertext must remain editable"


@th.django_unit_test("A dirty unread model save leaves the persisted blob untouched")
def test_guard_preserves_database(opts):
    from mojo.apps.dnsman.models import AcmeAccount
    from objict import objict
    with _saved_row("guard") as (row, kms):
        row.set_secret("original", "keep-me")
        row.save()
        original = row.mojo_secrets
        fresh = AcmeAccount.objects.get(pk=row.pk)
        fresh._get_kms = lambda: kms
        fresh._exposed_secrets = objict(replacement="unsafe")
        fresh._secrets_changed = True
        kms.encrypt_calls.clear()
        _unavailable(fresh.save)
        assert AcmeAccount.objects.get(pk=row.pk).mojo_secrets == original, \
            "Refused model.save must preserve the database ciphertext"
        assert not kms.encrypt_calls, "Refused model.save must not call the encrypt provider"


@th.django_unit_test("Empty new stores can be persisted and populated later")
def test_empty_creation(opts):
    with _saved_row("empty") as (row, kms):
        row.save()
        assert row.mojo_secrets is None and not row.secrets, "A new row without secrets remains empty"
        assert not kms.encrypt_calls and not kms.decrypt_calls, "An empty store must not contact KMS"
        row.set_secret("later", "value")
        row.save()
        row.refresh_from_db()
        assert row.get_secret("later") == "value", "A persisted empty store may receive its first secret"


@th.django_unit_test("Edge staging treats custody failures as unavailable material only")
def test_installer_custody_failure(opts):
    from types import SimpleNamespace
    from mojo.apps.edge.services import installer
    from mojo.models.secrets import SecretsUnavailableError

    class NoGenerationPath:
        def __str__(self):
            raise AssertionError("Unreadable custody must return before constructing a file path")

    certificate = SimpleNamespace(pk=3255001, cert_pem="TEST_CERT", chain_pem="")

    def unavailable(certificate):
        raise SecretsUnavailableError("Secret material unavailable")

    assert installer._write_material(
        NoGenerationPath(), certificate, private_key=unavailable) is False, \
        "An unavailable key must follow the installer's exclusion/retry policy without writing files"

    def unrelated_error(certificate):
        raise RuntimeError("unrelated-loader-error")

    try:
        installer._write_material(NoGenerationPath(), certificate, private_key=unrelated_error)
    except RuntimeError as error:
        assert str(error) == "unrelated-loader-error", \
            "An unrelated loader failure must retain its original exception"
    else:
        raise AssertionError("Installer must not suppress unrelated RuntimeError failures")


@th.django_unit_test("Rejected unread ciphertext cannot insert an unsaved model")
def test_unsaved_guard_precedes_database_insert(opts):
    from mojo.apps.dnsman.models import AcmeAccount
    from objict import objict
    with _saved_row("unread-insert") as (row, kms):
        row.mojo_secrets = "ciphertext-without-readable-identity"
        row._exposed_secrets = objict(replacement="unsafe")
        row._secrets_changed = True
        _unavailable(row.save)
        assert row.pk is None, "Refused secret writes must not allocate a model identity"
        assert not AcmeAccount.objects.filter(directory_url=row.directory_url).exists(), \
            "The unread ciphertext guard must run before the first database insert"
        assert row.mojo_secrets == "ciphertext-without-readable-identity", \
            "Refused creation must preserve the caller's original ciphertext"
        assert not kms.encrypt_calls, "Refused creation must not invoke encryption"


@th.django_unit_test("Decrypted arrays are unavailable stores, never accepted as mappings")
def test_decrypted_array_rejected(opts):
    for malformed in ([], [PROVIDER_DETAIL]):
        row, kms = _row()
        original = _seed(row, kms)
        kms.blobs[original] = (row._kms_context(), malformed)
        _unavailable(lambda: row.get_secret("original"))
        assert row.mojo_secrets == original, "Malformed plaintext must preserve its envelope"
        assert row._exposed_secrets is None, "An array must never enter the plaintext cache"
        _unavailable(lambda: row.set_secret("new", "unsafe"))
        row.save_secrets()
        assert not kms.encrypt_calls, "Rejected plaintext must not authorize replacement encryption"
        kms.blobs[original] = (row._kms_context(), {"original": "recovered"})
        assert row.get_secret("original") == "recovered", \
            "A failed plaintext-shape check must leave subsequent reads retryable"
