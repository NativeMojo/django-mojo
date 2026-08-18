"""Typed Admin fleet override codec."""

from testit import helpers as th


@th.django_unit_test("fleet override round-trips and composes deterministic assignments")
def test_override_roundtrip(opts):
    from mojo.deploy import config_override as override

    values = dict(override.DEFAULTS)
    payload = override.encode_document(
        values, "a" * 32, "2026-08-18T00:00:00+00:00", values)
    document = override.decode_document(payload, values)
    combined = override.compose(b"BASE = True\n", document).decode("utf-8")

    assert "GEOIP_PRIMARY_PROVIDER = 'mojo'" in combined, \
        f"the typed provider assignment was not rendered: {combined!r}"
    assert "GEOIP_MOJO_SYNC_ENABLED = False" in combined, \
        f"the boolean assignment was not rendered: {combined!r}"
    assert "MOJO_FLEET_CONFIG_REVISION = 'aaaaaaaa" in combined, \
        "the loaded revision marker was omitted"


@th.django_unit_test("fleet override rejects keys outside deployment delegation")
def test_override_rejects_undelegated_key(opts):
    from mojo.deploy import config_override as override

    with th.assert_raises(ValueError):
        override.validate_settings(
            {"GEOIP_PRIMARY_PROVIDER": "mojo"},
            {"GEOIP_FALLBACK_PROVIDER"})


@th.django_unit_test("fleet override rejects secrets and arbitrary settings")
def test_override_rejects_unregistered_key(opts):
    from mojo.deploy import config_override as override

    with th.assert_raises(ValueError):
        override.validate_settings(
            {"SECRET_KEY": "do-not-publish"}, {"SECRET_KEY"})
