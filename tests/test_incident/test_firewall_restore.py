"""
Tests for firewall ipset restore script generation.

Verifies that _build_restore_script produces correct ipset restore
format with atomic swap, and validates/filters CIDRs properly.
"""
from testit import helpers as th


@th.unit_test("restore script has correct structure")
def test_restore_script_structure(opts):
    from mojo.apps.incident.firewall import _build_restore_script

    script, count = _build_restore_script("test_set", ["10.0.0.0/8", "172.16.0.0/12"])
    lines = script.strip().splitlines()

    assert count == 2, f"Expected 2 valid CIDRs, got {count}"
    assert lines[0] == "create test_set_tmp hash:net -exist", \
        f"First line should create tmp set, got: {lines[0]}"
    assert lines[1] == "flush test_set_tmp", \
        f"Second line should flush tmp set, got: {lines[1]}"
    assert lines[2] == "add test_set_tmp 10.0.0.0/8", \
        f"Third line should add first CIDR, got: {lines[2]}"
    assert lines[3] == "add test_set_tmp 172.16.0.0/12", \
        f"Fourth line should add second CIDR, got: {lines[3]}"
    assert lines[4] == "swap test_set test_set_tmp", \
        f"Fifth line should swap sets, got: {lines[4]}"
    assert lines[5] == "destroy test_set_tmp", \
        f"Sixth line should destroy tmp set, got: {lines[5]}"


@th.unit_test("restore script filters invalid CIDRs")
def test_restore_script_filters_invalid(opts):
    from mojo.apps.incident.firewall import _build_restore_script

    cidrs = ["10.0.0.0/8", "not-an-ip", "", "192.168.1.0/24", None]
    script, count = _build_restore_script("test_set", cidrs)
    lines = script.strip().splitlines()

    assert count == 2, f"Expected 2 valid CIDRs out of 5 inputs, got {count}"
    add_lines = [l for l in lines if l.startswith("add ")]
    assert len(add_lines) == 2, f"Expected 2 add lines, got {len(add_lines)}"
    assert "10.0.0.0/8" in add_lines[0], f"First add should be 10.0.0.0/8, got: {add_lines[0]}"
    assert "192.168.1.0/24" in add_lines[1], f"Second add should be 192.168.1.0/24, got: {add_lines[1]}"


@th.unit_test("restore script skips swap on empty CIDR list")
def test_restore_script_empty(opts):
    from mojo.apps.incident.firewall import _build_restore_script

    script, count = _build_restore_script("empty_set", [])
    lines = script.strip().splitlines()

    assert count == 0, f"Expected 0 valid CIDRs, got {count}"
    # Should create and flush tmp, then destroy it — NO swap (prevents wiping live set)
    assert lines[0] == "create empty_set_tmp hash:net -exist", \
        f"Should create tmp set, got: {lines[0]}"
    assert lines[1] == "flush empty_set_tmp", \
        f"Should flush tmp set, got: {lines[1]}"
    assert lines[2] == "destroy empty_set_tmp", \
        f"Should destroy tmp set without swapping, got: {lines[2]}"
    assert "swap" not in script, "Should NOT swap when CIDR list is empty"


@th.unit_test("restore script rejects invalid name")
def test_restore_script_invalid_name(opts):
    from mojo.apps.incident.firewall import _build_restore_script

    script, count = _build_restore_script("bad name!", ["10.0.0.0/8"])
    assert script == "", f"Should return empty script for invalid name, got: {script!r}"
    assert count == 0, f"Should return 0 count for invalid name, got {count}"


@th.unit_test("restore script handles IPv6 CIDRs")
def test_restore_script_ipv6(opts):
    from mojo.apps.incident.firewall import _build_restore_script

    cidrs = ["2001:db8::/32", "fe80::1"]
    script, count = _build_restore_script("v6_set", cidrs)

    assert count == 2, f"Expected 2 valid IPv6 CIDRs, got {count}"
    assert "add v6_set_tmp 2001:db8::/32" in script, "Should include IPv6 CIDR"
    assert "add v6_set_tmp fe80::1" in script, "Should include IPv6 address"


@th.unit_test("restore script handles large CIDR list")
def test_restore_script_large(opts):
    from mojo.apps.incident.firewall import _build_restore_script

    # Simulate a country-sized list
    cidrs = [f"10.{i // 256}.{i % 256}.0/24" for i in range(3000)]
    script, count = _build_restore_script("big_set", cidrs)

    assert count == 3000, f"Expected 3000 valid CIDRs, got {count}"
    add_lines = [l for l in script.strip().splitlines() if l.startswith("add ")]
    assert len(add_lines) == 3000, f"Expected 3000 add lines, got {len(add_lines)}"


@th.unit_test("restore script uses tmp name for all operations")
def test_restore_script_uses_tmp_name(opts):
    from mojo.apps.incident.firewall import _build_restore_script

    script, _ = _build_restore_script("mojo_blocked", ["1.2.3.4"])
    lines = script.strip().splitlines()

    # create, flush, and add should all use _tmp
    for line in lines[:-2]:  # everything except swap and destroy
        assert "mojo_blocked_tmp" in line, \
            f"Line should use tmp name, got: {line}"
    # swap should reference both names
    assert "swap mojo_blocked mojo_blocked_tmp" in lines[-2], \
        f"Swap should reference both names, got: {lines[-2]}"
