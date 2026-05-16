"""DSL tests — pure-function tests against the rule parser/matcher.

No HTTP, no Redis, no server settings. Run inside the test process.
"""
from testit import helpers as th


@th.unit_test("dsl: empty rule allows everything")
def test_empty_rule(opts):
    from mojo.apps.account.services.geofence.dsl import evaluate_rule
    ok, reason = evaluate_rule({}, {"country_code": "RU"})
    assert ok is True, "empty rule must allow"
    assert reason is None, f"empty rule reason must be None, got {reason!r}"


@th.unit_test("dsl: country.in matches and rejects")
def test_country_in(opts):
    from mojo.apps.account.services.geofence.dsl import evaluate_rule
    rule = {"country": {"in": ["US", "CA"]}}
    ok, _ = evaluate_rule(rule, {"country_code": "US"})
    assert ok is True, "US must be allowed by country.in=[US,CA]"
    ok, reason = evaluate_rule(rule, {"country_code": "RU"})
    assert ok is False, "RU must be blocked by country.in=[US,CA]"
    assert reason == "country_not_allowed", f"got reason {reason!r}"


@th.unit_test("dsl: country.not_in matches and rejects")
def test_country_not_in(opts):
    from mojo.apps.account.services.geofence.dsl import evaluate_rule
    rule = {"country": {"not_in": ["KP", "IR"]}}
    ok, _ = evaluate_rule(rule, {"country_code": "US"})
    assert ok is True, "US must pass country.not_in=[KP,IR]"
    ok, reason = evaluate_rule(rule, {"country_code": "KP"})
    assert ok is False, "KP must be blocked by country.not_in=[KP,IR]"
    assert reason == "country_not_allowed", f"got reason {reason!r}"


@th.unit_test("dsl: country.eq exact match")
def test_country_eq(opts):
    from mojo.apps.account.services.geofence.dsl import evaluate_rule
    rule = {"country": {"eq": "US"}}
    ok, _ = evaluate_rule(rule, {"country_code": "US"})
    assert ok is True, "US must match country.eq=US"
    ok, reason = evaluate_rule(rule, {"country_code": "CA"})
    assert ok is False, "CA must not match country.eq=US"
    assert reason == "country_not_allowed", f"got reason {reason!r}"


@th.unit_test("dsl: region.in matches ISO 3166-2 codes")
def test_region_in(opts):
    from mojo.apps.account.services.geofence.dsl import evaluate_rule
    rule = {"region": {"in": ["US-FL", "US-NJ"]}}
    ok, _ = evaluate_rule(rule, {"country_code": "US", "region_code": "US-FL"})
    assert ok is True, "US-FL must pass region.in=[US-FL,US-NJ]"
    ok, reason = evaluate_rule(rule, {"country_code": "US", "region_code": "US-CA"})
    assert ok is False, "US-CA must be blocked by region.in=[US-FL,US-NJ]"
    assert reason == "region_not_allowed", f"got reason {reason!r}"


@th.unit_test("dsl: abuse.tor=false blocks Tor IPs")
def test_abuse_tor(opts):
    from mojo.apps.account.services.geofence.dsl import evaluate_rule
    rule = {"abuse": {"tor": False}}
    ok, _ = evaluate_rule(rule, {"is_tor": False})
    assert ok is True, "non-Tor IP must pass abuse.tor=False rule"
    ok, reason = evaluate_rule(rule, {"is_tor": True})
    assert ok is False, "Tor IP must be blocked by abuse.tor=False rule"
    assert reason == "tor_detected", f"got reason {reason!r}"


@th.unit_test("dsl: abuse.vpn=false blocks VPN IPs")
def test_abuse_vpn(opts):
    from mojo.apps.account.services.geofence.dsl import evaluate_rule
    rule = {"abuse": {"vpn": False}}
    ok, reason = evaluate_rule(rule, {"is_vpn": True})
    assert ok is False, "VPN IP must be blocked by abuse.vpn=False rule"
    assert reason == "vpn_detected", f"reason must be vpn_detected, got {reason!r}"


@th.unit_test("dsl: abuse.datacenter=false blocks datacenter IPs")
def test_abuse_datacenter(opts):
    from mojo.apps.account.services.geofence.dsl import evaluate_rule
    rule = {"abuse": {"datacenter": False}}
    ok, reason = evaluate_rule(rule, {"is_datacenter": True})
    assert ok is False, "datacenter IP must be blocked"
    assert reason == "datacenter_detected", f"got reason {reason!r}"


@th.unit_test("dsl: abuse null means don't care")
def test_abuse_null(opts):
    from mojo.apps.account.services.geofence.dsl import evaluate_rule
    rule = {"abuse": {"tor": None}}
    ok, _ = evaluate_rule(rule, {"is_tor": True})
    assert ok is True, "abuse.tor=None must allow Tor (don't care)"


@th.unit_test("dsl: validate_rule rejects unknown top-level key")
def test_validate_unknown_top(opts):
    from mojo.apps.account.services.geofence.dsl import validate_rule
    try:
        validate_rule({"city": {"in": ["NYC"]}})
        raise AssertionError("validate_rule must reject unknown top-level key 'city'")
    except ValueError as exc:
        assert "city" in str(exc), f"error must mention 'city', got: {exc}"


@th.unit_test("dsl: validate_rule rejects unknown operator")
def test_validate_unknown_op(opts):
    from mojo.apps.account.services.geofence.dsl import validate_rule
    try:
        validate_rule({"country": {"matches": ["US"]}})
        raise AssertionError("validate_rule must reject unknown operator 'matches'")
    except ValueError as exc:
        assert "matches" in str(exc), f"error must mention 'matches', got: {exc}"


@th.unit_test("dsl: validate_rule rejects non-list operand for in")
def test_validate_in_must_be_list(opts):
    from mojo.apps.account.services.geofence.dsl import validate_rule
    try:
        validate_rule({"country": {"in": "US"}})
        raise AssertionError("validate_rule must reject non-list operand for 'in'")
    except ValueError as exc:
        assert "list" in str(exc).lower(), f"error must mention list, got: {exc}"


@th.unit_test("dsl: validate_rule rejects unknown abuse flag")
def test_validate_unknown_abuse_flag(opts):
    from mojo.apps.account.services.geofence.dsl import validate_rule
    try:
        validate_rule({"abuse": {"hacker": True}})
        raise AssertionError("validate_rule must reject unknown abuse flag")
    except ValueError as exc:
        assert "hacker" in str(exc), f"error must mention 'hacker', got: {exc}"


@th.unit_test("dsl: validate_rule rejects non-boolean abuse value")
def test_validate_abuse_must_be_bool(opts):
    from mojo.apps.account.services.geofence.dsl import validate_rule
    try:
        validate_rule({"abuse": {"tor": "yes"}})
        raise AssertionError("validate_rule must reject non-bool abuse value")
    except ValueError as exc:
        assert "tor" in str(exc), f"error must mention 'tor', got: {exc}"
