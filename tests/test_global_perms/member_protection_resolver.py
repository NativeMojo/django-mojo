"""MEMBER_PERMS_PROTECTION is the settings-file map with the platform-wide
Setting row merged UNDER it (maestro item #7148).

Pure coverage of the two functions GroupMember.can_change_permission reads the
map through: no settings, database or server writes. The end-to-end contract
(a weaker, malformed or blank row against a configured file map) needs a server
reload and lives in tests/test_global_perms_extended_serial.
"""
from testit import helpers as th
from testit.helpers import assert_eq


@th.django_unit_test("member protection: a dict or a JSON object string parses to the same map")
def test_parse_accepts_dict_and_json_object(opts):
    from mojo.apps.account.models.member import parse_member_perms_protection as parse

    assert_eq(parse({"a": "sys.b"}), {"a": "sys.b"}, "a valid dict must parse to itself")
    assert_eq(parse('{"a": "sys.b"}'), {"a": "sys.b"},
              "a Setting row is a JSON string and must parse to the same map")
    assert_eq(parse({}), {}, "an empty dict is a valid, empty map")
    assert_eq(parse("{}"), {}, "an empty JSON object is a valid, empty map")
    assert_eq(parse({"a": ["x", "y"]}), {"a": ["x", "y"]},
              "a list of requirements must be accepted")
    assert_eq(parse('{"a": ["x", "y"]}'), {"a": ["x", "y"]},
              "a JSON list of requirements must be accepted")
    assert_eq(parse({"a": ("x", "y")}), {"a": ("x", "y")},
              "a tuple of requirements (settings file) must be accepted")
    assert_eq(parse({"a": {"x", "y"}}), {"a": {"x", "y"}},
              "a set of requirements (settings file) must be accepted")


@th.django_unit_test("member protection: None, empty and whitespace-only read as nothing configured")
def test_parse_blank_is_empty_not_malformed(opts):
    from mojo.apps.account.models.member import parse_member_perms_protection as parse

    assert_eq(parse(None), {}, "None (no row / setting absent) must read as empty")
    assert_eq(parse(""), {}, "an empty string must read as empty, not malformed")
    for blank in ("   ", "\t", "\n", "  \n"):
        assert_eq(parse(blank), {},
                  f"whitespace-only {blank!r} must read as empty, not malformed")


@th.django_unit_test("member protection: anything else is malformed and parses to None")
def test_parse_rejects_malformed(opts):
    from mojo.apps.account.models.member import parse_member_perms_protection as parse

    malformed = {
        "non-JSON string": "not json",
        "truncated JSON": '{"a": "b"',
        "JSON list": '["a"]',
        "JSON string": '"a"',
        "JSON number": "5",
        "JSON null": "null",
        "list": ["a"],
        "number": 5,
        "boolean": True,
        "empty key": {"": "sys.b"},
        "whitespace-only key": {" ": "sys.b"},
        "non-string key": {5: "sys.b"},
        "empty value": {"a": ""},
        "whitespace-only value": {"a": "  "},
        "null value": {"a": None},
        "boolean value": {"a": True},
        "dict value": {"a": {"b": "c"}},
        "empty list": {"a": []},
        "empty tuple": {"a": ()},
        "empty set": {"a": set()},
        "non-string member": {"a": ["x", 5]},
        "empty-string member": {"a": ["x", ""]},
        "JSON empty value": '{"a": ""}',
        "JSON empty list": '{"a": []}',
        "JSON non-string member": '{"a": ["x", 5]}',
    }
    for label, value in malformed.items():
        assert parse(value) is None, \
            f"{label} ({value!r}) must be malformed (None), got {parse(value)!r}"


@th.django_unit_test("member protection: the file map wins for its own keys, a row only adds")
def test_resolve_file_is_the_floor(opts):
    from mojo.apps.account.models.member import resolve_member_perms_protection as resolve

    file_map = {"prot": "sys.never"}
    assert_eq(resolve(file_map, '{"prot": "member"}'), {"prot": "sys.never"},
              "a row naming a file key must not loosen it — the file wins")
    assert_eq(resolve(file_map, "{}"), {"prot": "sys.never"},
              "an empty row must not remove the file's protection")
    assert_eq(resolve(file_map, None), {"prot": "sys.never"},
              "no row must leave the file map in force")
    for blank in ("", "  \n"):
        assert_eq(resolve(file_map, blank), {"prot": "sys.never"},
                  f"a blank row {blank!r} must add nothing and leave the file map in force")
    assert_eq(resolve(file_map, '{"extra": "sys.other"}'),
              {"prot": "sys.never", "extra": "sys.other"},
              "a row adding a key must be merged in beside the file map")
    assert_eq(resolve(None, '{"extra": "sys.other"}'), {"extra": "sys.other"},
              "with no file map a row's additions are the whole map")
    assert_eq(resolve(None, None), {}, "nothing configured anywhere is an empty map")


@th.django_unit_test("member protection: a malformed file value or row resolves to None")
def test_resolve_malformed_source_is_none(opts):
    from mojo.apps.account.models.member import resolve_member_perms_protection as resolve

    assert resolve({"prot": "sys.never"}, "not json") is None, \
        "a malformed row must resolve to None even with a valid file map"
    assert resolve(["prot"], '{"extra": "sys.other"}') is None, \
        "a malformed file value must resolve to None even with a valid row"
    assert resolve("not json", None) is None, \
        "a malformed file value with no row must resolve to None"
    assert resolve(["prot"], "not json") is None, \
        "both sources malformed must resolve to None"
