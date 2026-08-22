"""Default-tier core of the handoff destination allowlist (maestro item #2558).

The token-delivery destination allowlist is a C2-exfiltration boundary: a
handoff code minted for an attacker-controlled destination hands over a
session. #1839 moved its coverage opt-in because the tests mutated
django.conf; these run the fail-closed and bypass-shape contracts through the
keyword-only `static_entries=` / `resolver_path=` seams on
redirect_allowlist — no process-global state touched. The exhaustive matrix
(userinfo confusables, resolver fixtures, dot-segment sweeps) stays in
tests/test_auth_extended_serial/handoff.py.
"""
from testit import helpers as th
from testit.helpers import assert_true


def assert_false(value, msg):
    assert not value, msg


@th.django_unit_test("allowlist core: nothing configured refuses everything")
def test_core_fails_closed_when_unconfigured(opts):
    from mojo.apps.account.services import redirect_allowlist as ra

    for dest in ("https://example.com/", "https://app.example.com/dash"):
        assert_false(
            ra.is_allowed_destination(dest, static_entries=None, resolver_path=""),
            f"with no allowlist and no resolver every destination must be "
            f"refused, {dest!r} was admitted")
    assert_false(
        ra.is_allowed_destination("https://example.com/",
                                  static_entries=[], resolver_path=""),
        "an EMPTY allowlist means 'enforce, allow nothing' — it must refuse")
    assert_true(
        ra.is_enforced(static_entries=[], resolver_path=""),
        "an empty list is a deliberate opt-in to enforcement")
    assert_false(
        ra.is_enforced(static_entries=None, resolver_path=""),
        "nothing configured at all is monitor mode, not enforcement")


@th.django_unit_test("allowlist core: the deny rule holds against every bypass shape")
def test_core_deny_rule_bypass_shapes(opts):
    from mojo.apps.account.services import redirect_allowlist as ra

    entries = ["https://app.example.com/"]
    bypasses = [
        ("https://evil.tld/steal", "an unlisted host"),
        ("https://app.example.com.evil.tld/", "a suffix-extended host"),
        ("https://app.example.com@evil.tld/", "a userinfo confusable"),
        ("https://evil.tld\\@app.example.com/", "a backslash-userinfo parser differential"),
        ("https://aPP.example.com.evil.tld/", "case games on a suffix host"),
        ("https://app.example.com:8443@evil.tld/", "port-bearing userinfo confusable"),
        ("//app.example.com/x", "a scheme-relative URL"),
        ("javascript:alert(1)", "a javascript: scheme"),
        ("https://app.example.com/a/../../x", "a dot-segment escape"),
    ]
    for dest, why in bypasses:
        assert_false(
            ra.is_allowed_destination(dest, static_entries=entries, resolver_path=""),
            f"{why} must be refused: {dest!r}")

    assert_true(
        ra.is_allowed_destination("https://app.example.com/dash",
                                  static_entries=entries, resolver_path=""),
        "control: the listed host itself must still be admitted — otherwise "
        "the refusals above prove a broken matcher, not a working deny rule")


@th.django_unit_test("allowlist core: an unimportable resolver path refuses everything")
def test_core_broken_resolver_is_closed(opts):
    from mojo.apps.account.services import redirect_allowlist as ra

    assert_false(
        ra.is_allowed_destination(
            "https://app.example.com/",
            static_entries=["https://app.example.com/"],
            resolver_path="no.such.module.testit_2558_resolver"),
        "a resolver dotted path that fails to import must refuse everything — "
        "even a statically-listed host must not fall through to the list")


@th.django_unit_test("allowlist core: a raw-string entry coerces like the settings read")
def test_core_string_entries_coerce(opts):
    from mojo.apps.account.services import redirect_allowlist as ra

    # A DB-backed Setting row holds a string; the seam must apply the same
    # kind="list" coercion, so a bare string is the single entry it spells —
    # never a char-shattered wildcard.
    assert_true(
        ra.is_allowed_destination("https://app.example.com/x",
                                  static_entries="https://app.example.com/",
                                  resolver_path=""),
        "a bare-string entry must admit the host it spells")
    assert_false(
        ra.is_allowed_destination("https://h.evil.tld/",
                                  static_entries="https://app.example.com/",
                                  resolver_path=""),
        "a bare-string entry must not admit anything else — the old "
        "char-shatter bug made 'h' admit every https host")
