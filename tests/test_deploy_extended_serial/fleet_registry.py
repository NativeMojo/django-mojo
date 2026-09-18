"""Import-light application fleet settings contract."""

from unittest import mock

from testit import helpers as th


def _register(codec, key="EXAMPLE_FLEET_FLAG", **kwargs):
    fields = dict(label="Example", section="Example application",
                  description="Application-owned fleet setting.", value_type="boolean")
    fields.update(kwargs)
    return codec.register_setting(key, **fields)


@th.django_unit_test("fleet schemas validate types bounds and secret metadata")
def test_fleet_schema_types(opts):
    from mojo.deploy import config_override as codec

    with mock.patch.dict(codec._REGISTRY, clear=False):
        _register(codec)
        assert codec.validate_settings({"EXAMPLE_FLEET_FLAG": True}, ["EXAMPLE_FLEET_FLAG"]), "A delegated boolean must validate"
        with th.assert_raises(ValueError):
            codec.validate_settings({"EXAMPLE_FLEET_FLAG": 1}, ["EXAMPLE_FLEET_FLAG"])
        _register(codec, "EXAMPLE_FLEET_LIMIT", value_type="integer", min_value=1, max_value=10)
        for value in (True, 0, 11, "5"):
            with th.assert_raises(ValueError):
                codec.validate_settings({"EXAMPLE_FLEET_LIMIT": value}, ["EXAMPLE_FLEET_LIMIT"])
        definition = _register(codec, "EXAMPLE_FLEET_TOKEN", value_type="string",
                               default="private-default", sensitive=True, max_length=20)
        assert "default" not in definition and "validator" not in definition, "Public schema must omit secret defaults and validators"
        assert "private-default" not in str(codec.definitions()), "Registry listing must never disclose secret defaults"
        with th.assert_raises(ValueError):
            codec.validate_settings({"EXAMPLE_FLEET_TOKEN": "x" * 21}, ["EXAMPLE_FLEET_TOKEN"])
        _register(codec, "EXAMPLE_FLEET_OBJECT", value_type="object")
        with th.assert_raises(ValueError):
            codec.validate_settings({"EXAMPLE_FLEET_OBJECT": {"value": float("nan")}},
                                    ["EXAMPLE_FLEET_OBJECT"])


@th.django_unit_test("fleet schema registration cannot delegate bootstrap authority")
def test_fleet_schema_reserved_and_conflict(opts):
    from mojo.deploy import config_override as codec

    with mock.patch.dict(codec._REGISTRY, clear=False):
        for key in ("SECRET_KEY", "CONFIG_SYNC_SCHEMA_MODULES", "AWS_CONFIG_BUCKET",
                    "ADMIN_FLEET_CONFIG_ALLOWED_KEYS", "INSTALLED_APPS", "KMS_KEY_ID",
                    "FRESH_AUTH_ENFORCE", "ALLOW_EMAIL_CHANGE", "ALLOW_PHONE_CHANGE",
                    "ALLOW_USERNAME_CHANGE", "ALLOW_SELF_DEACTIVATION", "WEBAPP_BASE_URL",
                    "GEOIP_API_KEY_MOJO", "BASE_URL", "AWS_CLOUDWATCH_ALARM_TOPIC_ARNS"):
            with th.assert_raises(ValueError):
                _register(codec, key)
            assert codec.get_definition(key) is None, "Rejected independently owned keys must not enter the fleet registry"
        original = _register(codec)
        assert _register(codec) == original, "Identical registration must remain idempotent"
        with th.assert_raises(ValueError):
            _register(codec, default=True)
        with th.assert_raises(ValueError):
            codec.validate_settings({"EXAMPLE_FLEET_FLAG": True}, [])
        assert set(codec.DEFAULTS) <= {row["key"] for row in codec.definitions()}, "Built-in GeoIP definitions must remain registered"


@th.django_unit_test("fleet schema modules are explicit trusted imports and sanitize errors")
def test_fleet_schema_loader(opts):
    from mojo.deploy import config_override as codec

    with mock.patch.object(codec.importlib, "import_module") as importer:
        codec.load_schema_modules(["example.fleet_settings"])
        importer.assert_called_once_with("example.fleet_settings")
        with th.assert_raises(ValueError):
            codec.load_schema_modules(["example;import os"])
    with mock.patch.object(codec.importlib, "import_module", side_effect=RuntimeError("secret")):
        try:
            codec.load_schema_modules(["example.fleet_settings"])
        except ValueError as error:
            assert "secret" not in str(error), "Schema loader errors must not expose exception details"
        else:
            assert False, "failed schema import must refuse publication"


@th.django_unit_test("application fleet descriptors stay protected and sensitive")
def test_fleet_schema_catalog_bridge(opts):
    from mojo.deploy import config_override as codec
    from mojo.apps.account.services import admin_settings

    with mock.patch.dict(codec._REGISTRY, clear=False):
        _register(codec, "EXAMPLE_FLEET_TOKEN", value_type="string", sensitive=True)
        rows = admin_settings.descriptors()
        descriptor = next(row for row in rows if row.key == "EXAMPLE_FLEET_TOKEN")
        assert descriptor.sensitivity == "configured_only", "Catalog must redact registered secrets"
        assert descriptor.default is None, "Catalog must not expose a secret default"
        assert admin_settings.is_catalog_protected(descriptor.key), "Registered fleet keys must reject generic global writes"
        assert "Example application" in admin_settings._section_names(rows), "Application sections must appear in the catalog"
