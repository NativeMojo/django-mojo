"""APIKEY_PERMS_PROTECTION is three layers: the platform-wide Setting row,
UNDER the settings-file map, UNDER the framework floor (maestro item #7150).

Pure coverage of resolve_apikey_perms_protection, the function
ApiKey.can_change_permission reads the map through: no settings, database or
server writes. The parser itself is MEMBER_PERMS_PROTECTION's and is covered in
member_protection_resolver.py. The end-to-end contract (a weaker, malformed or
blank row against a configured file map) needs a server reload and lives in
tests/test_global_perms_extended_serial.
"""
from testit import helpers as th
from testit.helpers import assert_eq


def _floor():
    from mojo.apps.account.models.api_key import APIKEY_PERMS_PROTECTION_DEFAULTS
    return dict(APIKEY_PERMS_PROTECTION_DEFAULTS)


@th.django_unit_test("apikey protection: the file map wins over a row in both directions")
def test_file_wins_over_row_both_ways(opts):
    from mojo.apps.account.models.api_key import resolve_apikey_perms_protection as resolve

    file_map = {"prot": "sys.never"}
    assert_eq(resolve(file_map, '{"prot": "manage_group"}'), {"prot": "sys.never", **_floor()},
              "a weaker row naming a file key must not loosen it — the file wins")
    loose_file = {"prot": "manage_group"}
    assert_eq(resolve(loose_file, '{"prot": "sys.never"}'), {"prot": "manage_group", **_floor()},
              "a stricter row naming a file key must not tighten it either — the file wins")
    assert_eq(resolve(file_map, '{"prot": ["manage_group", "manage_members"]}'),
              {"prot": "sys.never", **_floor()},
              "a row re-mapping a file key to a list must not replace the file's requirement")


@th.django_unit_test("apikey protection: a row adds a protected permission beside the file map")
def test_row_adds_a_permission(opts):
    from mojo.apps.account.models.api_key import resolve_apikey_perms_protection as resolve

    assert_eq(resolve({"prot": "sys.never"}, '{"extra": "sys.other"}'),
              {"prot": "sys.never", "extra": "sys.other", **_floor()},
              "a row adding a key must be merged in beside the file map")
    assert_eq(resolve(None, '{"extra": "sys.other"}'), {"extra": "sys.other", **_floor()},
              "with no file map a row's additions sit under the framework floor")
    assert_eq(resolve(None, {"extra": ["sys.a", "sys.b"]}), {"extra": ["sys.a", "sys.b"], **_floor()},
              "an 'any of' list requirement must be carried through")


@th.django_unit_test("apikey protection: an empty, missing or blank row adds nothing")
def test_empty_row_adds_nothing(opts):
    from mojo.apps.account.models.api_key import resolve_apikey_perms_protection as resolve

    file_map = {"prot": "sys.never"}
    expected = {"prot": "sys.never", **_floor()}
    assert_eq(resolve(file_map, "{}"), expected,
              "an empty-object row must not remove the file's protection")
    assert_eq(resolve(file_map, {}), expected, "an empty dict row must not remove it either")
    assert_eq(resolve(file_map, None), expected, "no row must leave the file map in force")
    for blank in ("", "   ", "\t", "  \n"):
        assert_eq(resolve(file_map, blank), expected,
                  f"a blank row {blank!r} must add nothing and leave the file map in force")
    assert_eq(resolve(None, None), _floor(),
              "nothing configured anywhere is exactly the framework floor")


@th.django_unit_test("apikey protection: a malformed row or file value resolves to None")
def test_malformed_source_is_none(opts):
    from mojo.apps.account.models.api_key import resolve_apikey_perms_protection as resolve

    file_map = {"prot": "sys.never"}
    malformed = {
        "non-JSON string": "not json",
        "JSON null": "null",
        "JSON list": '["x"]',
        "JSON number": "5",
        "one bad entry beside a good one": '{"a": "sys.a", "b": ""}',
        "empty requirement list": '{"a": []}',
        "numeric requirement": '{"a": 5}',
        "boolean requirement": '{"a": true}',
    }
    for label, row in malformed.items():
        assert resolve(file_map, row) is None, \
            f"a malformed row ({label}: {row!r}) must resolve to None even with a valid file map"
    for label, value in {
        "list": ["prot"],
        "non-JSON string": "not json",
        "number": 5,
        "boolean": True,
        "one bad entry beside a good one": {"a": "sys.a", "b": ""},
        "numeric requirement": {"a": 5},
        "boolean requirement": {"a": True},
    }.items():
        assert resolve(value, '{"extra": "sys.other"}') is None, \
            f"a malformed file value ({label}: {value!r}) must resolve to None even with a valid row"
        assert resolve(value, None) is None, \
            f"a malformed file value ({label}: {value!r}) with no row must resolve to None"
    assert resolve(["prot"], "not json") is None, "both sources malformed must resolve to None"


@th.django_unit_test("apikey protection: the framework floor wins over the file and the row")
def test_builtins_win_over_both(opts):
    from mojo.apps.account.models.api_key import resolve_apikey_perms_protection as resolve

    floor = _floor()
    assert len(floor) == 4, f"this test pins the four built-in protections, got {sorted(floor)}"
    weak = {perm: "manage_group" for perm in floor}
    for label, resolved in {
        "file": resolve(weak, None),
        "row": resolve(None, weak),
        "file and row": resolve(weak, weak),
    }.items():
        for perm, requirement in floor.items():
            assert_eq(resolved[perm], requirement,
                      f"{perm} re-mapped by the {label} must keep its built-in requirement")
    assert_eq(resolve({"mine": "sys.mine"}, {"theirs": "sys.theirs"}),
              {"theirs": "sys.theirs", "mine": "sys.mine", **floor},
              "deployment entries from both sources must survive beside the floor")
