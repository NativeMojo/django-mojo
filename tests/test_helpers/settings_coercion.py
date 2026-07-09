"""settings.get(kind=...) coercion contract (ITEM-023).

A present-but-uncoercible value must degrade to the DECLARED default (loudly —
a settings warning is logged, though these tests pin only the value contract):
- an unrecognized bool STRING no longer truthy-coerces to True;
- a bracket-wrapped unparsable list no longer falls through to comma-split
  (which manufactured nonsense entries);
- dict/int garbage keeps returning the default shape.

All rows use unregistered ITEM023_COERCE_* keys (write validation never
interferes) and are created and removed INSIDE each test via Setting.set /
Setting.remove, which keep the Redis settings hash coherent.
"""
from testit import helpers as th

BOOL_KEY = "ITEM023_COERCE_BOOL"
DICT_KEY = "ITEM023_COERCE_DICT"
LIST_KEY = "ITEM023_COERCE_LIST"
INT_KEY = "ITEM023_COERCE_INT"


def _remove(*keys):
    from mojo.apps.account.models.setting import Setting
    for key in keys:
        Setting.remove(key)


@th.django_unit_setup()
def setup_settings_coercion(opts):
    # Long-lived DB: clear leftovers from a previous run before any test.
    _remove(BOOL_KEY, DICT_KEY, LIST_KEY, INT_KEY)


@th.django_unit_test("coercion: unrecognized bool strings return the DECLARED default, not True")
def test_bool_garbage_returns_declared_default(opts):
    from mojo.helpers.settings import settings
    from mojo.apps.account.models.setting import Setting
    try:
        Setting.set(BOOL_KEY, "garbage")
        val = settings.get(BOOL_KEY, False, kind="bool")
        assert val is False, \
            f"garbage bool string with default False must return False, got {val!r}"
        val = settings.get(BOOL_KEY, True, kind="bool")
        assert val is True, \
            f"garbage bool string with default True must return True, got {val!r}"
        val = settings.get(BOOL_KEY, kind="bool")
        assert val is False, \
            f"garbage bool string with no default must return False, got {val!r}"

        # Recognized strings still parse.
        Setting.set(BOOL_KEY, "yes")
        assert settings.get(BOOL_KEY, False, kind="bool") is True, \
            "'yes' must still coerce True"
        Setting.set(BOOL_KEY, "off")
        assert settings.get(BOOL_KEY, True, kind="bool") is False, \
            "'off' must still coerce False"
        # Real booleans round-trip.
        Setting.set(BOOL_KEY, True)
        assert settings.get(BOOL_KEY, False, kind="bool") is True, \
            "a real boolean must round-trip"
    finally:
        _remove(BOOL_KEY)


@th.django_unit_test("coercion: unparsable dict values return the default shape")
def test_dict_garbage_returns_default(opts):
    from mojo.helpers.settings import settings
    from mojo.apps.account.models.setting import Setting
    try:
        Setting.set(DICT_KEY, "{not-json")
        val = settings.get(DICT_KEY, {"d": 1}, kind="dict")
        assert val == {"d": 1}, \
            f"unparsable dict must return the declared default, got {val!r}"
        val = settings.get(DICT_KEY, kind="dict")
        assert val == {}, f"unparsable dict with no default must return {{}}, got {val!r}"

        Setting.set(DICT_KEY, {"a": 1})
        val = settings.get(DICT_KEY, {}, kind="dict")
        assert dict(val) == {"a": 1}, f"a valid dict must round-trip, got {val!r}"
    finally:
        _remove(DICT_KEY)


@th.django_unit_test("coercion: bracket-wrapped unparsable lists return the default, never comma-split")
def test_list_bracket_garbage_returns_default(opts):
    from mojo.helpers.settings import settings
    from mojo.apps.account.models.setting import Setting
    try:
        # Trailing comma = invalid JSON. The old behavior comma-split this into
        # nonsense entries ('["payments"', ']') — for FAIL_CLOSED_SCOPES that
        # silently changes which scopes fail closed.
        Setting.set(LIST_KEY, '["payments",]')
        val = settings.get(LIST_KEY, ["x"], kind="list")
        assert val == ["x"], (
            f"bracket-wrapped unparsable list must return the declared default, "
            f"got {val!r}"
        )

        # Plain comma strings are a legit format and keep working.
        Setting.set(LIST_KEY, "a, b")
        val = settings.get(LIST_KEY, [], kind="list")
        assert val == ["a", "b"], f"comma strings must still split, got {val!r}"

        Setting.set(LIST_KEY, ["p1", "p2"])
        val = settings.get(LIST_KEY, [], kind="list")
        assert list(val) == ["p1", "p2"], f"a valid list must round-trip, got {val!r}"
    finally:
        _remove(LIST_KEY)


@th.django_unit_test("coercion: garbage int values return the declared default")
def test_int_garbage_returns_default(opts):
    from mojo.helpers.settings import settings
    from mojo.apps.account.models.setting import Setting
    try:
        Setting.set(INT_KEY, "12x")
        val = settings.get(INT_KEY, 42, kind="int")
        assert val == 42, f"garbage int must return the declared default, got {val!r}"
        Setting.set(INT_KEY, 7)
        assert settings.get(INT_KEY, 0, kind="int") == 7, "a valid int must round-trip"
    finally:
        _remove(INT_KEY)


@th.django_unit_test("coercion: pre-existing garbage in a geofence key degrades to the declared default")
def test_geofence_read_path_planted_garbage(opts):
    from mojo.helpers.settings import settings
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.account.services.geofence import cache as gf_cache
    key = "GEOFENCE_ALLOW_PRIVATE_IPS"
    try:
        # Valid write first (write validation allows it), then plant garbage
        # via queryset.update(), which bypasses save() — modeling a pre-existing
        # bad row — and refresh the Redis settings hash to match.
        Setting.set(key, True)
        Setting.objects.filter(key=key, group=None).update(value="garbage")
        row = Setting.objects.filter(key=key, group=None).first()
        row.push_to_cache()

        val = settings.get(key, True, kind="bool")
        assert val is True, \
            f"planted garbage with default True must read True (the default), got {val!r}"
        val = settings.get(key, False, kind="bool")
        assert val is False, (
            f"planted garbage with default False must read False — the old "
            f"behavior truthy-coerced it to True (fail-open), got {val!r}"
        )
    finally:
        Setting.remove(key)
        gf_cache.invalidate_all()
