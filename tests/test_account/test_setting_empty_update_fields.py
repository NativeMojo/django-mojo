"""Maestro item #7739 — a Setting save told to write nothing does nothing.

Bug: `Setting.save` called `super().save()` and then pushed the object's
in-memory value to the Redis settings cache whatever `update_fields` was.
Django writes nothing for an empty `update_fields`, so an existing row stayed
75 in the database while the cache said 60. For a new row it was worse:
`MojoSecrets.save` follows the first (no-op) save with a second, unrestricted
`super().save()`, which inserts the whole row.

Fix: `Setting.save` returns before anything else when `update_fields` is given
and empty, in any collection form, a generator, or the fourth positional
argument.

Every row is test-owned (literal `TESTIT_EUF_` keys, a test-owned group) and is
removed from Redis and the database in setup and cleanup. Nothing here patches
production code: the database and the cache are the real isolated ones.
"""
import uuid as _uuid
from testit import helpers as th

KEY = "TESTIT_EUF_LIMIT"
OTHER_KEY = "TESTIT_EUF_OTHER"
NEW_KEY = "TESTIT_EUF_NEW"
NEW_SECRET_KEY = "TESTIT_EUF_NEW_SECRET"
CONTROL_KEY = "TESTIT_EUF_CONTROL"


def _drop():
    """Remove every test-owned row from Redis and the database."""
    from mojo.apps.account.models import Setting
    rows = Setting.objects.filter(key__startswith="TESTIT_EUF_")
    for row in rows:
        row.remove_from_cache()
    rows.delete()
    for key in (KEY, OTHER_KEY, NEW_KEY, NEW_SECRET_KEY, CONTROL_KEY):
        Setting(key=key).remove_from_cache()


def _stored(pk):
    from mojo.apps.account.models import Setting
    return Setting.objects.get(pk=pk)


def _cached(key, group_id=None):
    from mojo.apps.account.models import Setting
    return Setting.get_cached(key, group_id)


def _fresh_row(opts, warm):
    """The existing row re-read from the database, so the draft starts clean,
    with the cache warmed to the stored value or emptied."""
    from mojo.apps.account.models import Setting
    row = Setting.objects.get(pk=opts.row_pk)
    if warm:
        row.push_to_cache()
        assert _cached(KEY) == (row.value, True), \
            f"setup: a warm cache must hold the stored {row.value!r}, got {_cached(KEY)}"
    else:
        row.remove_from_cache()
        assert _cached(KEY)[1] is False, \
            f"setup: a cold cache must not hold the key, got {_cached(KEY)}"
    return row


def _assert_untouched(opts, warm, why):
    stored = _stored(opts.row_pk)
    assert stored.key == KEY and stored.value == "75" and stored.group_id is None \
        and stored.is_secret is False, (
            f"{why}: the stored row must be unchanged, got key={stored.key} "
            f"value={stored.value!r} group={stored.group_id} secret={stored.is_secret}")
    expected = ("75", True) if warm else (None, False)
    assert _cached(KEY) == expected, \
        f"{why}: the cache must still read {expected}, got {_cached(KEY)}"


@th.django_unit_setup()
def setup_empty_update_fields(opts):
    from mojo.apps.account.models import Group, Setting

    _drop()
    Group.objects.filter(name__startswith="euf_grp_").delete()
    group = Group.objects.create(
        name=f"euf_grp_{_uuid.uuid4().hex[:8]}", kind="organization", is_active=True)
    opts.group_id = group.pk

    row = Setting(key=KEY, group=None, is_secret=False)
    row.set_value("75")
    row.save()
    opts.row_pk = row.pk
    assert _cached(KEY) == ("75", True), f"setup: the row must be cached, got {_cached(KEY)}"


@th.django_unit_test("#7739: every empty update_fields form writes and publishes nothing")
def test_empty_forms_existing_row(opts):
    forms = (
        ("empty list", lambda: []),
        ("empty tuple", lambda: ()),
        ("empty set", lambda: set()),
        ("empty frozenset", lambda: frozenset()),
        ("empty generator", lambda: (name for name in ())),
    )
    for warm in (True, False):
        for name, make in forms:
            row = _fresh_row(opts, warm)
            row.set_value("60")
            row.save(update_fields=make())
            _assert_untouched(opts, warm, f"{name}, cache {'warm' if warm else 'cold'}")


@th.django_unit_test("#7739: an empty update_fields as the fourth positional argument is a no-op")
def test_empty_positional_existing_row(opts):
    import warnings
    for warm in (True, False):
        row = _fresh_row(opts, warm)
        row.set_value("60")
        with warnings.catch_warnings():
            # Django 5.2 still accepts positional save() arguments, with a
            # deprecation warning; the call itself is what is under test.
            warnings.simplefilter("ignore")
            row.save(False, False, None, [])
        _assert_untouched(opts, warm, f"positional [], cache {'warm' if warm else 'cold'}")


@th.django_unit_test("#7739: positional update_fields with keyword update_fields=None is bound as Django binds it")
def test_empty_positional_with_keyword_none(opts):
    import warnings
    from mojo.apps.account.models import Setting

    forms = (
        ("list", lambda: []),
        ("tuple", lambda: ()),
        ("generator", lambda: (name for name in ())),
    )
    with warnings.catch_warnings():
        # Django 5.2 takes the positional value when the keyword is its
        # default None (Model._parse_save_params); the mix is under test.
        warnings.simplefilter("ignore")
        for warm in (True, False):
            for name, make in forms:
                row = _fresh_row(opts, warm)
                row.set_value("60")
                row.save(False, False, None, make(), update_fields=None)
                _assert_untouched(
                    opts, warm,
                    f"positional {name} with update_fields=None, cache {'warm' if warm else 'cold'}")

        fresh = Setting(key=NEW_KEY, group=None, is_secret=False)
        fresh.set_value("60")
        fresh.save(False, False, None, [], update_fields=None)
        assert not Setting.objects.filter(key=NEW_KEY).exists(), \
            "a new row saved with positional [] and update_fields=None must not be inserted"
        assert _cached(NEW_KEY)[1] is False, \
            f"a new row saved with positional [] and update_fields=None must not be cached, got {_cached(NEW_KEY)}"

        row = _fresh_row(opts, warm=True)
        row.set_value("60")
        try:
            row.save(False, False, None, [], update_fields=[])
        except TypeError:
            pass
        else:
            raise AssertionError(
                "positional and keyword update_fields together must still raise Django's TypeError")
        _assert_untouched(opts, True, "positional and keyword update_fields together")

        row = _fresh_row(opts, warm=False)
        row.set_value("64")
        row.save(False, False, None, (name for name in ["value"]), update_fields=None)
        assert _stored(opts.row_pk).value == "64" and _cached(KEY) == ("64", True), \
            f"a non-empty positional generator with update_fields=None must write and publish, " \
            f"got {_stored(opts.row_pk).value!r} / {_cached(KEY)}"
        row = _fresh_row(opts, warm=True)
        row.set_value("75")
        row.save()


@th.django_unit_test("#7739: a draft differing in value, is_secret, key or group changes nothing")
def test_empty_save_with_any_draft(opts):
    from mojo.apps.account.models import Group

    group = Group.objects.get(pk=opts.group_id)
    for warm in (True, False):
        tag = f"cache {'warm' if warm else 'cold'}"

        row = _fresh_row(opts, warm)
        row.set_value("60")
        row.save(update_fields=[])
        _assert_untouched(opts, warm, f"draft value, {tag}")

        row = _fresh_row(opts, warm)
        row.is_secret = True
        row.set_value("60")
        row.save(update_fields=[])
        _assert_untouched(opts, warm, f"draft is_secret, {tag}")

        row = _fresh_row(opts, warm)
        row.key = OTHER_KEY
        row.set_value("60")
        row.save(update_fields=[])
        _assert_untouched(opts, warm, f"draft key, {tag}")
        assert _cached(OTHER_KEY)[1] is False, \
            f"draft key, {tag}: nothing may be published under the draft key, got {_cached(OTHER_KEY)}"

        row = _fresh_row(opts, warm)
        row.group = group
        row.set_value("60")
        row.save(update_fields=[])
        _assert_untouched(opts, warm, f"draft group, {tag}")
        assert _cached(KEY, opts.group_id)[1] is False, \
            f"draft group, {tag}: nothing may be published for the group, got {_cached(KEY, opts.group_id)}"


@th.django_unit_test("#7739: a new row saved with empty update_fields is not created or cached")
def test_empty_save_new_row(opts):
    from mojo.apps.account.models import Setting

    plain = Setting(key=NEW_KEY, group=None, is_secret=False)
    plain.set_value("60")
    plain.save(update_fields=[])
    assert not Setting.objects.filter(key=NEW_KEY).exists(), \
        "a new row saved with update_fields=[] must not be inserted (MojoSecrets' second save)"
    assert _cached(NEW_KEY)[1] is False, \
        f"a new row saved with update_fields=[] must not be cached, got {_cached(NEW_KEY)}"

    secret = Setting(key=NEW_SECRET_KEY, group=None, is_secret=True)
    secret.set_value("60")
    secret.save(update_fields=())
    assert not Setting.objects.filter(key=NEW_SECRET_KEY).exists(), \
        "a new secret row saved with update_fields=() must not be inserted"
    assert _cached(NEW_SECRET_KEY)[1] is False, \
        f"a new secret row saved with update_fields=() must not be cached, got {_cached(NEW_SECRET_KEY)}"


@th.django_unit_test("#7739: ordinary saves still write and publish as before")
def test_positive_controls(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import Group, Setting

    row = _fresh_row(opts, warm=True)
    row.set_value("61")
    row.save()
    assert _stored(opts.row_pk).value == "61" and _cached(KEY) == ("61", True), \
        f"a plain save must write and publish, got {_stored(opts.row_pk).value!r} / {_cached(KEY)}"

    row = _fresh_row(opts, warm=False)
    row.set_value("62")
    row.save(update_fields=["value"])
    assert _stored(opts.row_pk).value == "62" and _cached(KEY) == ("62", True), \
        f"save(update_fields=['value']) must write and publish, got {_stored(opts.row_pk).value!r} / {_cached(KEY)}"

    row = _fresh_row(opts, warm=True)
    row.set_value("63")
    row.save(update_fields=(name for name in ["value"]))
    assert _stored(opts.row_pk).value == "63" and _cached(KEY) == ("63", True), \
        f"a non-empty generator must still be honoured, got {_stored(opts.row_pk).value!r} / {_cached(KEY)}"

    Setting.set(CONTROL_KEY, "70")
    assert Setting.objects.filter(key=CONTROL_KEY, group=None).values_list("value", flat=True).first() == "70" \
        and _cached(CONTROL_KEY) == ("70", True), \
        f"Setting.set must write and publish, got {_cached(CONTROL_KEY)}"

    row = _fresh_row(opts, warm=True)
    row.group = Group.objects.get(pk=opts.group_id)
    try:
        row.save()
    except merrors.ValueException:
        pass
    else:
        raise AssertionError("moving an existing row to another group must still be refused")
    assert _stored(opts.row_pk).group_id is None, "a refused scope change must leave the row's group alone"

    # Back to the stored 75 for any later run of the file.
    row = _fresh_row(opts, warm=True)
    row.set_value("75")
    row.save()


@th.django_unit_test("#7739: cleanup")
def test_cleanup(opts):
    from mojo.apps.account.models import Group
    _drop()
    Group.objects.filter(pk=opts.group_id).delete()
