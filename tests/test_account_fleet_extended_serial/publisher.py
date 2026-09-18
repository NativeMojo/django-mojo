"""Exact-object publishing, restoration and secret handling without AWS calls."""

import io
import json
from contextlib import contextmanager, ExitStack
from types import SimpleNamespace
from unittest import mock

from testit import helpers as th


@contextmanager
def fixture():
    from mojo.apps.account.services import fleet_config, provider_setup
    from mojo.deploy import config_override
    with ExitStack() as stack:
        stack.enter_context(mock.patch.dict(config_override._REGISTRY))
        config_override.register_setting(
            "TESTIT_FLEET_SECRET", label="Secret", section="Test", description="Test secret",
            value_type="string", sensitive=True, default="default-secret")
        config_override.register_setting(
            "TESTIT_FLEET_COUNT", label="Count", section="Test", description="Test count",
            value_type="integer", default=1, min_value=0, max_value=20)
        allowed = set(config_override.DEFAULTS) | {"TESTIT_FLEET_SECRET", "TESTIT_FLEET_COUNT"}
        static = {"ADMIN_FLEET_CONFIG_ALLOWED_KEYS": list(allowed),
                  "ADMIN_FLEET_CONFIG_BUCKET": "bucket", "ADMIN_FLEET_CONFIG_PREFIX": "fleet",
                  "ADMIN_FLEET_CONFIG_KMS_KEY_ID": "key", "ADMIN_FLEET_CONFIG_RESTART_ENABLED": True}
        stack.enter_context(mock.patch.object(provider_setup, "_static", side_effect=lambda key, default=None: static.get(key, default)))
        actor = SimpleNamespace(pk=1, is_superuser=True)
        gate = stack.enter_context(mock.patch.object(provider_setup, "_superuser", return_value=actor))
        audit = stack.enter_context(mock.patch.object(fleet_config, "_audit"))
        s3 = mock.Mock()
        s3.get_bucket_versioning.return_value = {"Status": "Enabled"}
        s3.put_object.return_value = {"VersionId": "new-version"}
        stack.enter_context(mock.patch.object(provider_setup, "_s3_client", return_value=s3))
        yield fleet_config, provider_setup, config_override, s3, actor, gate, audit


def remote(codec, values, revision="a" * 32, version="old-version"):
    body = codec.encode_document(values, revision, "2026-09-17T00:00:00Z",
                                 {row["key"] for row in codec.definitions()})
    return {"Body": io.BytesIO(body), "ContentLength": len(body), "ETag": '"old-etag"',
            "VersionId": version, "Metadata": {"sha256": codec.sha256(body)}}


@th.django_unit_test()
def test_publish_preserves_secrets_and_uses_cas(opts):
    with fixture() as (fleet, provider, codec, s3, actor, gate, audit):
        s3.get_object.return_value = remote(codec, {"TESTIT_FLEET_SECRET": "stored-secret"})
        result = fleet.publish(actor, {"expected_revision": "a" * 32, "changes": {
            "TESTIT_FLEET_SECRET": {"action": "set", "value": ""},
            "TESTIT_FLEET_COUNT": {"action": "set", "value": 2}}})
        sent = s3.put_object.call_args.kwargs
        assert json.loads(sent["Body"])["settings"] == {"TESTIT_FLEET_SECRET": "stored-secret", "TESTIT_FLEET_COUNT": 2}, "Empty secret replacement must preserve the stored secret"
        assert sent["IfMatch"] == '"old-etag"' and sent["Key"] == "fleet/django.override.json", "Publish must CAS the exact configured object"
        assert sent["ServerSideEncryption"] == "aws:kms" and sent["SSEKMSKeyId"] == "key", "Publish must require configured KMS encryption"
        assert gate.call_args_list[-1] == mock.call(actor, lock=True), "Publishing must recheck the actor under lock"
        assert result["published"] and result["applied"] is False, "Publication must not claim node convergence"
        assert "stored-secret" not in repr(result) + repr(audit.call_args), "Responses and audits must not contain secrets"


@th.django_unit_test()
def test_clear_removes_override_and_state_redacts(opts):
    with fixture() as (fleet, provider, codec, s3, actor, gate, audit):
        s3.get_object.side_effect = lambda **kw: remote(codec, {"TESTIT_FLEET_SECRET": "stored-secret"})
        result = fleet.state(actor)
        secret = next(row for row in result["entries"] if row["key"] == "TESTIT_FLEET_SECRET")
        assert secret["configured"] and "current" not in secret and "default" not in secret, "Secret state must reveal only configured status"
        assert "stored-secret" not in repr(result) and "default-secret" not in repr(result), "State must not disclose secret defaults or values"
        fleet.publish(actor, {"expected_revision": "a" * 32, "changes": {"TESTIT_FLEET_SECRET": {"action": "clear"}}})
        assert "TESTIT_FLEET_SECRET" not in json.loads(s3.put_object.call_args.kwargs["Body"])["settings"], "Clear must remove the override to fall back to base"


@th.django_unit_test()
def test_invalid_changes_and_stale_revision_fail_closed(opts):
    from mojo import errors as me
    with fixture() as (fleet, provider, codec, s3, actor, gate, audit):
        s3.get_object.side_effect = lambda **kw: remote(codec, {})
        cases = [
            {"expected_revision": "b" * 32, "changes": {"TESTIT_FLEET_COUNT": {"action": "set", "value": 2}}},
            {"expected_revision": "a" * 32, "changes": {"TESTIT_FLEET_COUNT": {"action": "set", "value": True}}},
            {"expected_revision": "a" * 32, "changes": {"TESTIT_FLEET_COUNT": {"action": "clear", "value": 1}}},
            {"expected_revision": "a" * 32, "changes": {"NOT_REGISTERED": {"action": "set", "value": 2}}},
            {"expected_revision": "a" * 32, "changes": {}, "object_key": "other"},
        ]
        for payload in cases:
            with th.assert_raises(me.ValueException):
                fleet.publish(actor, payload)
        assert not s3.put_object.called, "Invalid edits and stale revisions must never write"


@th.django_unit_test()
def test_versioning_integrity_and_actor_revocation(opts):
    from mojo import errors as me
    with fixture() as (fleet, provider, codec, s3, actor, gate, audit):
        payload = {"expected_revision": "a" * 32, "changes": {"TESTIT_FLEET_COUNT": {"action": "set", "value": 2}}}
        s3.get_object.side_effect = lambda **kw: remote(codec, {})
        s3.get_bucket_versioning.return_value = {"Status": "Suspended"}
        with th.assert_raises(me.ValueException):
            fleet.publish(actor, payload)
        s3.get_bucket_versioning.return_value = {"Status": "Enabled"}
        gate.side_effect = [actor, me.PermissionDeniedException("revoked")]
        with th.assert_raises(me.PermissionDeniedException):
            fleet.publish(actor, payload)
        gate.side_effect = None
        corrupt = remote(codec, {})
        corrupt["Metadata"]["sha256"] = "incorrect"
        s3.get_object.side_effect = None
        s3.get_object.return_value = corrupt
        with th.assert_raises(me.ValueException):
            fleet.publish(actor, payload)
        assert not s3.put_object.called, "Versioning, actor or integrity failures must prevent publication"


@th.django_unit_test()
def test_restore_republishes_exact_version_with_new_revision(opts):
    with fixture() as (fleet, provider, codec, s3, actor, gate, audit):
        s3.get_object.side_effect = [remote(codec, {"TESTIT_FLEET_COUNT": 9}), remote(codec, {"TESTIT_FLEET_COUNT": 3}, "b" * 32, "prior-version")]
        result = fleet.restore(actor, {"expected_revision": "a" * 32, "version_id": "prior-version"})
        assert s3.get_object.call_args.kwargs == {"Bucket": "bucket", "Key": "fleet/django.override.json", "VersionId": "prior-version"}, "Rollback must read the selected version of the exact configured object"
        restored = json.loads(s3.put_object.call_args.kwargs["Body"])
        assert restored["settings"]["TESTIT_FLEET_COUNT"] == 3 and restored["revision"] not in ("a" * 32, "b" * 32), "Rollback must republish historical values under a fresh revision"
        assert result["applied"] is False, "Rollback publication must not imply fleet convergence"


@th.django_unit_test()
def test_history_bounds_and_filters_sibling_keys(opts):
    with fixture() as (fleet, provider, codec, s3, actor, gate, audit):
        s3.list_object_versions.return_value = {"Versions": [
            {"Key": "fleet/django.override.json", "VersionId": "one", "IsLatest": True},
            {"Key": "fleet/django.override.json.other", "VersionId": "other"}], "IsTruncated": True}
        result = fleet.history(actor)
        assert len(result["versions"]) == 1 and result["versions"][0]["version_id"] == "one", "History must exclude prefix siblings"
        assert result["truncated"] and s3.list_object_versions.call_args.kwargs["MaxKeys"] == 50, "History work must stay bounded"
        assert not s3.get_object.called, "History must never download settings values"


@th.django_unit_test()
def test_geoip_publisher_preserves_app_secret_without_disclosure(opts):
    with fixture() as (fleet, provider, codec, s3, actor, gate, audit):
        values = {**codec.DEFAULTS, "TESTIT_FLEET_SECRET": "stored-secret"}
        s3.get_object.side_effect = lambda **kw: remote(codec, values)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(provider, "_configuration_revision", return_value=None))
            stack.enter_context(mock.patch.object(provider, "_write_configuration_revision"))
            stack.enter_context(mock.patch.object(provider, "_write_verify_state"))
            stack.enter_context(mock.patch.object(provider, "_save_geoip_secret"))
            stack.enter_context(mock.patch.object(provider, "_audit"))
            stack.enter_context(mock.patch.object(provider, "_test_provider_credentials", return_value=({"geoip": {"success": True}}, False)))
            provider.apply(actor, "geoip", {"expected_revision": "a" * 32, "geoip": {
                **codec.DEFAULTS, "GEOIP_MOJO_SYNC_ENABLED": True}})
            sent = json.loads(s3.put_object.call_args.kwargs["Body"])["settings"]
            assert sent["TESTIT_FLEET_SECRET"] == "stored-secret", "GeoIP publish must preserve application-owned overrides"
            result = provider.state()
            assert "TESTIT_FLEET_SECRET" not in result["geoip"] and "stored-secret" not in repr(result), "Legacy provider state must not leak application secrets"


@th.django_unit_test()
def test_first_publish_and_external_writer_race(opts):
    from botocore.exceptions import ClientError
    from mojo import errors as me
    with fixture() as (fleet, provider, codec, s3, actor, gate, audit):
        s3.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        payload = {"expected_revision": None, "changes": {"TESTIT_FLEET_COUNT": {"action": "set", "value": 2}}}
        fleet.publish(actor, payload)
        assert s3.put_object.call_args.kwargs.get("IfNoneMatch") == "*", "First publication must never overwrite a concurrently created document"
        s3.put_object.reset_mock()
        s3.put_object.side_effect = ClientError({"Error": {"Code": "PreconditionFailed", "Message": "secret-error-body"}}, "PutObject")
        with th.assert_raises(me.ValueException) as error:
            fleet.publish(actor, payload)
        assert "secret-error-body" not in str(error.exception), "S3 errors must not expose raw remote exception details"
        assert s3.put_object.call_count == 1, "A CAS failure must not retry and overwrite the winning admin"


@th.django_unit_test()
def test_restore_rejects_corrupt_history_without_write(opts):
    from mojo import errors as me
    with fixture() as (fleet, provider, codec, s3, actor, gate, audit):
        historical = remote(codec, {"TESTIT_FLEET_SECRET": "stored-secret"})
        historical["Metadata"] = {}
        s3.get_object.side_effect = [remote(codec, {}), historical]
        with th.assert_raises(me.ValueException):
            fleet.restore(actor, {"expected_revision": "a" * 32, "version_id": "prior-version"})
        assert not s3.put_object.called, "An unverified historical version must never be restored"
