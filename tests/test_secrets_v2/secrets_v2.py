from testit import helpers as th

K1 = "1" * 40
K2 = "2" * 40
PREFIX = "secrets_v2_"
GROUP_NAME = "secrets_v2_group"


def _keys(obj, keys):
    # Per-instance seam: no settings, no module patch.
    obj._secrets_key_list = lambda: list(keys)
    return obj


def _user(name, keys, secrets=None):
    from mojo.apps.account.models import User
    User.objects.filter(username=PREFIX + name).delete()
    user = _keys(User(username=PREFIX + name, email=f"{PREFIX}{name}@example.com"), keys)
    user.save()
    if secrets:
        user.set_secrets(secrets)
        user.save()
    return user


def _load(pk, keys):
    from mojo.apps.account.models import User
    return _keys(User.objects.get(pk=pk), keys)


def _blob(pk):
    from mojo.apps.account.models import User
    return User.objects.filter(pk=pk).values_list("mojo_secrets", flat=True).first()


def _rewrap(pk, keys, **kwargs):
    from mojo.apps.account.management.commands.rewrap_secrets import rewrap
    return rewrap(models=["account.user"], pk=pk, keys=keys, **kwargs)["account.user"]


def _raises(fn, *errors):
    try:
        fn()
    except errors:
        return True
    return False


@th.django_unit_setup()
def setup_secrets_v2(opts):
    from mojo.apps.account.models import User, Group
    User.objects.filter(username__startswith=PREFIX).delete()
    Group.objects.filter(name=GROUP_NAME).delete()


@th.django_unit_test()
def test_no_key_is_legacy(opts):
    from mojo.helpers import crypto
    user = _user("nokey", [""], {"token": "abc"})
    blob = _blob(user.pk)
    assert blob and not blob.startswith("v2:"), f"without a key the write must be legacy, got {blob[:8]!r}"
    plain = crypto.decrypt(blob, user._get_secrets_password(), False)
    assert plain.get("token") == "abc", "legacy blob must open with the row password as today"
    assert _load(user.pk, [""]).get_secret("token") == "abc", "legacy read must work without a key"


@th.django_unit_test()
def test_key_set_writes_v2(opts):
    from mojo.helpers import crypto
    user = _user("withkey", [K1], {"token": "abc"})
    blob = _blob(user.pk)
    assert blob.startswith("v2:"), f"with MOJO_SECRETS_KEY the write must be v2, got {blob[:8]!r}"
    row_only = crypto.decrypt(blob[3:], user._get_secrets_password(), True)
    assert row_only is None, "the row's own fields must not decrypt a v2 blob"
    assert _load(user.pk, [K1]).get_secret("token") == "abc", "v2 read with the key must round trip"
    assert _raises(lambda: _load(user.pk, [""]).secrets, ValueError), "v2 read without the key must fail"


@th.django_unit_test()
def test_legacy_row_reads_with_key(opts):
    user = _user("legacyread", [""], {"token": "old"})
    assert not _blob(user.pk).startswith("v2:"), "setup should store a legacy blob"
    loaded = _load(user.pk, [K1])
    assert loaded.get_secret("token") == "old", "legacy rows must still read once a key is set"
    loaded.set_secret("token", "new")
    loaded.save()
    assert _blob(user.pk).startswith("v2:"), "a changed secret must be rewritten as v2"
    assert _load(user.pk, [K1]).get_secret("token") == "new", "rewritten secret must read back"


@th.django_unit_test()
def test_short_key_refused(opts):
    from django.core.exceptions import ImproperlyConfigured
    user = _user("shortkey", [""])
    _keys(user, ["too-short"])
    user.set_secret("token", "abc")
    assert _raises(user.save, ImproperlyConfigured), "a key under 32 characters must raise, not write"
    assert _blob(user.pk) is None, "nothing may be written with a short key"


@th.django_unit_test()
def test_blob_copied_to_other_row_or_model_refused(opts):
    from mojo.apps.account.models import User, Group
    source = _user("copysrc", [K1], {"token": "abc"})
    target = _user("copydst", [K1])
    blob = _blob(source.pk)
    User.objects.filter(pk=target.pk).update(mojo_secrets=blob)
    assert _raises(lambda: _load(target.pk, [K1]).secrets, ValueError), "a blob copied to another row must be refused"
    group = Group.objects.create(name=GROUP_NAME, kind="organization")
    Group.objects.filter(pk=group.pk).update(mojo_secrets=blob)
    loaded = _keys(Group.objects.get(pk=group.pk), [K1])
    assert _raises(lambda: loaded.secrets, ValueError), "a blob copied to another model must be refused"
    group.delete()


@th.django_unit_test()
def test_rotation_with_fallback_then_rewrap(opts):
    user = _user("rotate", [K1], {"token": "abc"})
    assert _load(user.pk, [K2, K1]).get_secret("token") == "abc", "old key in fallbacks must still read"
    counts = _rewrap(user.pk, [K2, K1], apply=True)
    assert counts["v2_fallback"] == 1 and counts["rewrapped"] == 1, f"rotation should rewrap the row: {counts}"
    assert _load(user.pk, [K2]).get_secret("token") == "abc", "after rewrap the new key alone must read"
    again = _rewrap(user.pk, [K2])
    assert again["v2_primary"] == 1 and again["v2_fallback"] == 0, f"dry run should show primary only: {again}"


@th.django_unit_test()
def test_rewrap_dry_run_writes_nothing(opts):
    user = _user("dryrun", [""], {"token": "abc"})
    before = _blob(user.pk)
    counts = _rewrap(user.pk, [K1])
    assert counts["legacy"] == 1 and counts["rewrapped"] == 0, f"dry run should only count: {counts}"
    assert _blob(user.pk) == before, "dry run must not change the row"


@th.django_unit_test()
def test_rewrap_apply_is_idempotent(opts):
    user = _user("apply", [""], {"token": "abc"})
    first = _rewrap(user.pk, [K1], apply=True)
    assert first["rewrapped"] == 1, f"apply should convert the legacy row: {first}"
    assert _blob(user.pk).startswith("v2:"), "applied row must be v2"
    second = _rewrap(user.pk, [K1], apply=True)
    assert second["rewrapped"] == 0 and second["v2_primary"] == 1, f"second run must change nothing: {second}"
    assert _load(user.pk, [K1]).get_secret("token") == "abc", "secret must survive rewrap"


@th.django_unit_test()
def test_rewrap_stops_on_corrupt_blob(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.management.commands.rewrap_secrets import RewrapError
    user = _user("corrupt", [""])
    User.objects.filter(pk=user.pk).update(mojo_secrets="bm90IGEgcmVhbCBibG9iIGF0IGFsbCBzdHJpbmc=")
    assert _raises(lambda: _rewrap(user.pk, [K1], apply=True), RewrapError), "an unreadable row must stop the command"
    assert _blob(user.pk) == "bm90IGEgcmVhbCBibG9iIGF0IGFsbCBzdHJpbmc=", "the unreadable row must not be written"


@th.django_unit_test()
def test_rewrap_counts_empty_marker(opts):
    from mojo.apps.account.models import User
    user = _user("marker", [""])
    User.objects.filter(pk=user.pk).update(mojo_secrets="{}")
    counts = _rewrap(user.pk, [K1], apply=True)
    assert counts["empty_marker"] == 1 and counts["rewrapped"] == 0, f"'{{}}' must be skipped as a marker: {counts}"
    assert _blob(user.pk) == "{}", "the marker row must be left alone"


@th.django_unit_test()
def test_rollback_to_legacy(opts):
    user = _user("rollback", [K1], {"token": "abc"})
    loaded = _load(user.pk, ["", K1])
    assert loaded.get_secret("token") == "abc", "key in fallbacks must still read v2"
    loaded.set_secret("other", "x")
    loaded.save()
    assert not _blob(user.pk).startswith("v2:"), "with the key moved to fallbacks, writes must be legacy"
    _user("rollback2", [K1], {"token": "def"})
    from mojo.apps.account.models import User
    second = User.objects.get(username=PREFIX + "rollback2")
    counts = _rewrap(second.pk, ["", K1], to_legacy=True, apply=True)
    assert counts["rewrapped"] == 1, f"--to-legacy should convert the v2 row: {counts}"
    assert not _blob(second.pk).startswith("v2:"), "row must be legacy after --to-legacy"
    assert _load(second.pk, [""]).get_secret("token") == "def", "legacy row must read with no key at all"


@th.django_unit_test()
def test_rewrap_refusals(opts):
    from mojo.apps.account.management.commands.rewrap_secrets import RewrapError
    user = _user("refuse", [""], {"token": "abc"})
    assert _raises(lambda: _rewrap(user.pk, [""], apply=True), RewrapError), "--apply must be refused with no key"
    assert _raises(lambda: _rewrap(user.pk, ["short"], apply=True), RewrapError), "--apply must refuse a short key"
    assert _raises(lambda: _rewrap(user.pk, [K1], to_legacy=True, apply=True), RewrapError), \
        "--to-legacy --apply must be refused while a primary key is set"
    assert not _blob(user.pk).startswith("v2:"), "refused runs must not write"


@th.django_unit_test()
def test_overriding_subclass_stays_legacy(opts):
    from mojo.models.secrets import MojoSecrets

    class CustomScheme:
        _secrets_write_key = MojoSecrets._secrets_write_key

        def _get_secrets_password(self):
            return "custom"

        def _secrets_key_list(self):
            return [K1]

    assert CustomScheme()._secrets_write_key() is None, "a model with its own password must keep writing legacy"


@th.django_unit_test()
def test_anonymize_stores_null(opts):
    user = _user("anon", [""], {"token": "abc"})
    from mojo.apps.account.models import User
    try:
        _load(user.pk, [""]).pii_anonymize()
        assert _blob(user.pk) is None, f"anonymize must store NULL, got {_blob(user.pk)!r}"
    finally:
        User.objects.filter(pk=user.pk).delete()
