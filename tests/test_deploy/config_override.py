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


@th.django_unit_test("the fleet tool composes byte-for-byte what a node composes (#5853)")
def test_compose_published_matches_the_node(opts):
    import json
    from mojo.deploy import config_override as override

    # WMWX's allow-list: exactly the five GEOIP_* keys, no revision key.
    allowed = set(override.DEFAULTS)
    values = dict(override.DEFAULTS)
    values["GEOIP_MOJO_PROVIDER_URL"] = "https://api.mojoverify.com/"
    revision = "d" * 32
    payload = json.dumps({
        "schema_version": override.SCHEMA_VERSION, "revision": revision,
        "published_at": "2026-09-29T00:00:00+00:00", "settings": values,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    base = b"BASE = True\nOTHER = 'x'\n"

    node = override.compose(base, override.decode_document(payload, allowed))
    tool = override.compose_published(base, payload)
    assert tool == node, f"tool and node files must be byte-identical:\n{tool!r}\n{node!r}"
    lines = node.decode("utf-8").rstrip("\n").splitlines()
    assert lines[-1] == f"MOJO_FLEET_CONFIG_REVISION = {revision!r}", \
        f"the last managed line must be the revision stamp, got {lines[-1]!r}"
    assert "GEOIP_MOJO_PROVIDER_URL = 'https://api.mojoverify.com'" in lines, \
        f"the provider URL's trailing slash must be stripped as the node strips it: {lines}"

    for bad in (b"not json", b"[]", json.dumps({"schema_version": 1}).encode(),
                payload.replace(revision.encode(), b"nothex"),
                payload.replace(b'"schema_version":1', b'"schema_version":2'),
                b"x" * (override.MAX_DOCUMENT_BYTES + 1)):
        with th.assert_raises(ValueError):
            override.compose_published(base, bad)
