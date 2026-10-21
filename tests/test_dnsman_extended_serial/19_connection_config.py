"""Protected topic policies, redaction and explicit candidate authentication."""
import io
import importlib
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, ExitStack
from types import SimpleNamespace
from unittest import mock

from testit import helpers as th

UUID = "d" * 32
SECRET = "TESTIT-protected-recipient-secret-canary"


def caps(*args, **kwargs):
    return dict(version=2, shared_validation=False, consumer_project_uuid=UUID,
                can_consume=False, can_manage_trust=False, trust_scope="apex_and_wildcard")


@contextmanager
def fixture():
    from mojo.apps.dnsman import fleet_settings as schema
    from mojo.apps.dnsman.services import connection_config as topic, acme_hub_client
    from mojo.apps.account.services import provider_setup as provider, fleet_config, fleet_apply
    from mojo.helpers.settings import settings
    from mojo.deploy import config_override as codec
    with ExitStack() as stack:
        stack.enter_context(mock.patch.dict(schema._PROFILES, {}, clear=True))
        schema.register_hub_profile("test_hub", label="Test hub", origin="https://hub.example",
                                    consumer_project_uuid=UUID, consumer_label="Original recipient")
        values = dict(zip(schema.KEYS, ("test_hub", "https://hub.example", SECRET)))
        static = dict(values, ADMIN_FLEET_CONFIG_ALLOWED_KEYS=list(schema.KEYS) + list(codec.DEFAULTS),
                      CONFIG_SYNC_OVERRIDE_ALLOWED_KEYS=list(schema.KEYS),
                      ADMIN_FLEET_CONFIG_SCHEMA_MODULES=["mojo.apps.dnsman.fleet_settings"],
                      CONFIG_SYNC_SCHEMA_MODULES=["mojo.apps.dnsman.fleet_settings"],
                      ADMIN_FLEET_CONFIG_BUCKET="test-owned-bucket", ADMIN_FLEET_CONFIG_PREFIX="config",
                      ADMIN_FLEET_CONFIG_KMS_KEY_ID="test-owned-kms", ADMIN_FLEET_CONFIG_RESTART_ENABLED=True,
                      ADMIN_FLEET_CONFIG_EXPECTED_NODES=["node-a", "offline"])
        original = settings.get_static
        stack.enter_context(mock.patch.object(settings, "get_static", side_effect=lambda key, default=None, **kw:
                                              static[key] if key in static else original(key, default, **kw)))
        actor = SimpleNamespace(pk=1, is_superuser=True)
        stack.enter_context(mock.patch.object(provider, "_superuser", return_value=actor))
        stack.enter_context(mock.patch.object(fleet_config, "_audit"))
        stack.enter_context(mock.patch.object(fleet_apply, "observe", return_value={"status": "pending", "healthy_everywhere": False,
                                                                                  "nodes": [{"hostname": "offline", "healthy": False}]}))
        probe = stack.enter_context(mock.patch.object(acme_hub_client, "capabilities_for", side_effect=caps))
        stack.enter_context(mock.patch.object(topic, "_has_connections", return_value=False))
        s3 = mock.Mock()
        s3.get_bucket_versioning.return_value = {"Status": "Enabled"}
        s3.put_object.return_value = {"VersionId": "test-version"}
        body = codec.encode_document(values, "a" * 32, "now", static["ADMIN_FLEET_CONFIG_ALLOWED_KEYS"])
        s3.get_object.side_effect = lambda **kw: {"Body": io.BytesIO(body), "ContentLength": len(body), "ETag": '"current"',
                                                  "VersionId": "current", "Metadata": {"sha256": codec.sha256(body)}}
        stack.enter_context(mock.patch.object(provider, "_s3_client", return_value=s3))
        payload = dict(action="preview", expected_revision="a" * 32, expected_catalog_revision=schema.catalog_revision(),
                       profile_id="test_hub", key_action="keep")
        yield topic, schema, provider, fleet_config, s3, actor, values, static, payload, probe


def refused(code, call):
    from mojo.apps.dnsman.services.connection_config import ConfigurationError
    try:
        call()
    except ConfigurationError as error:
        assert error.error_code == code, "The protected topic must return the selected stable failure code"
        assert SECRET not in str(error), "Safe failures must never contain the submitted credential"
        return error
    raise AssertionError("The invalid shared-validation operation was accepted")


@contextmanager
def competing_identity_owner():
    from django.db import connections
    from mojo.apps.dnsman.services import connection_config
    admitted, release, finished = threading.Event(), threading.Event(), threading.Event()
    outcome = []

    def own_identity():
        connections.close_all()
        try:
            with connection_config.identity_lock():
                admitted.set()
                if not release.wait(10):
                    raise AssertionError("Test must release its competing identity owner")
            outcome.append("released")
        except Exception as error:
            outcome.append(type(error).__name__)
        finally:
            connections.close_all()
            finished.set()

    thread = threading.Thread(target=own_identity)
    thread.start()
    try:
        assert admitted.wait(3), "A separate PostgreSQL connection must actually own identity admission"
        yield
    finally:
        release.set()
        assert finished.wait(5), "The competing identity owner must finish before fixture cleanup"
        thread.join(1)
        assert outcome == ["released"], f"The test-owned PostgreSQL transaction must release normally: {outcome}"


@th.django_unit_test("opposing Domain and identity owners refuse setup without a lock cycle")
def test_opposing_domain_identity_owners_refuse_without_mutation(opts):
    from django.db import connection, connections, transaction
    from mojo.apps.account.models import Group
    from mojo.apps.dnsman.models import AcmeDelegation, Domain
    from mojo.apps.dnsman.services import connection_config, delegation_setup as setup
    shared = importlib.import_module("test_dnsman.23_shared_setup")
    env = shared.fixture()
    old = setup._prepare(env.target, env.hub.target, env.hub.consumer, None, False,
                         identity_check=lambda consumer: None)
    ready, finished = threading.Event(), threading.Event()
    owner = {}
    observed = {}

    def opposing_owner():
        connections.close_all()
        try:
            with connection_config.identity_lock():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '4s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    owner["pid"] = cursor.fetchone()[0]
                ready.set()
                row = setup._prepare(env.target, env.hub.target, env.hub.consumer, None, False,
                                     identity_check=lambda consumer: None)
                owner["completed"] = row.pk
        except Exception as error:
            owner["error"] = type(error).__name__
        finally:
            connections.close_all()
            finished.set()

    thread = threading.Thread(target=opposing_owner)
    try:
        with transaction.atomic():
            Domain.objects.select_for_update().get(pk=env.domain.pk)
            Group.objects.select_for_update().get(pk=env.group.pk)
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '500ms'")
                cursor.execute("SELECT pg_backend_pid()")
                caller_pid = cursor.fetchone()[0]
            before = list(AcmeDelegation.objects.filter(group=env.group).values_list(
                "pk", "client_ref", "state", "retired_at", "hub_participant_ref"))
            thread.start()
            assert ready.wait(3), "The opposing connection must own the actual identity advisory lock"
            deadline = time.monotonic() + 2
            blocked = False
            while time.monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [caller_pid, owner["pid"]])
                    blocked = cursor.fetchone()[0]
                if blocked:
                    break
            assert blocked, "PostgreSQL must confirm the identity owner is blocked by this caller's Domain fence"
            started = time.monotonic()
            try:
                setup.connect(env.target, env.hub.target, restart=True, client=env.hub,
                              custody=lambda cert: True, identity_check=lambda consumer: None)
                observed["outcome"] = "accepted"
            except setup.SetupError as error:
                observed.update(outcome=error.error_code, status=error.status)
            except Exception as error:
                observed["outcome"] = type(error).__name__
            observed["duration"] = time.monotonic() - started
            after = list(AcmeDelegation.objects.filter(group=env.group).values_list(
                "pk", "client_ref", "state", "retired_at", "hub_participant_ref"))
            assert after == before, "Busy restart must not insert, rotate, retire or rebind immutable connection intent"
            assert not any(call[0] in ("join", "publish", "withdraw") for call in env.hub.calls), "Busy setup must not mutate the injected remote hub"
        assert finished.wait(5), "Releasing the Domain/tenant fences must let the opposing preparation complete"
        thread.join(1)
        assert observed.get("outcome") == "configuration_busy" and observed.get("status") == 409, f"Competing setup must return configuration_busy 409 instead of waiting or changing identity: {observed}"
        assert owner.get("completed") == old.pk and "error" not in owner, f"The identity owner must complete actual preparation after the other caller releases its fences: {owner}"
        assert observed["duration"] < 0.4, "Identity admission must refuse before the test-owned half-second PostgreSQL lock timeout"
    finally:
        if thread.ident is not None:
            thread.join(6)
        assert not thread.is_alive(), "The opposing PostgreSQL owner must release its connection before fixture cleanup"
        env.domain.delete()
        env.group.delete()


@th.django_unit_test("busy revoked-participant restart preserves every diagnostic in autocommit")
def test_busy_revoked_restart_preserves_complete_connection_snapshot(opts):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from mojo.apps.dnsman.models import AcmeDelegation
    from mojo.apps.dnsman.rest import delegation_setup as api
    from mojo.apps.dnsman.services import delegation_setup as setup
    shared = importlib.import_module("test_dnsman.23_shared_setup")
    env = shared.fixture()
    cleanup_calls = []
    try:
        old = shared.verified(env)
        AcmeDelegation.objects.filter(pk=old.pk).update(last_error_code="alias_lookup_failed")
        old.refresh_from_db()
        participant = env.hub.participants[str(old.client_ref)]
        participant.trust_state, participant.can_publish = "revoked", False
        env.hub.calls.clear()
        assert not connection.in_atomic_block, "The direct Native action regression must use autocommit, without an outer rollback hiding writes"
        before = list(AcmeDelegation.objects.filter(group=env.group).order_by("pk").values())
        with competing_identity_owner():
            with CaptureQueriesContext(connection) as queries:
                result = api._action(setup.connect, env.target, env.hub.target, restart=True,
                                     client=env.hub, custody=lambda cert: True,
                                     cleanup=lambda *args: cleanup_calls.append("cleanup"),
                                     identity_check=lambda consumer: None)
            after = list(AcmeDelegation.objects.filter(group=env.group).order_by("pk").values())
            assert result.get("error_code") == "configuration_busy" and result.get("code") == 409, "The direct action must preserve busy409 for a newly observed revoked participant"
            changed = [key for key in before[0] if before[0][key] != after[0][key]] if len(before) == len(after) == 1 else ["row_count"]
            assert after == before, f"Busy restart must preserve all connection fields, including diagnostics and modified timestamps: changed={changed}"
            writes = [q["sql"] for q in queries.captured_queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
            assert not writes, "Busy revoked restart must issue no local write, even if a caller catches and commits the refusal"
            assert not cleanup_calls and not any(call[0] in ("join", "publish", "withdraw") for call in env.hub.calls), "Busy revoked restart must not invoke cleanup or mutate the injected hub"
        # A deliberate retry after admission succeeds still records the explicit
        # revocation observation on the retired immutable predecessor.
        success = setup.connect(env.target, env.hub.target, restart=True, client=env.hub,
                                custody=lambda cert: True,
                                cleanup=lambda *args: cleanup_calls.append("cleanup"),
                                identity_check=lambda consumer: None)
        old.refresh_from_db()
        new = AcmeDelegation.objects.get(pk=success["connection"]["id"])
        assert old.state == "retired" and old.last_error_code == "trust_revoked", "Admitted explicit restart must retain the observed revocation diagnostic on its predecessor"
        assert new.pk != old.pk and new.client_ref != old.client_ref and AcmeDelegation.objects.filter(group=env.group).count() == 2, "Admitted restart must create exactly one isolated generation"
        assert new.hub_allocation_ref == old.hub_allocation_ref and new.hub_consumer_project_uuid == old.hub_consumer_project_uuid, "Admitted restart must preserve immutable allocation and consumer identity"
        assert cleanup_calls == ["cleanup"], "Cleanup must run exactly once, only after successful identity admission"
    finally:
        env.domain.delete()
        env.group.delete()


@th.django_unit_test("busy publication is bounded across topic, common writer and restore")
def test_busy_publication_boundaries_and_unchanged_tuple_bypass(opts):
    from mojo import errors as me
    from mojo.apps.account.models import User
    from mojo.apps.account.services import provider_setup
    from mojo.deploy import config_override as codec
    real_superuser = provider_setup._superuser
    with fixture() as (topic, schema, provider, fleet, s3, actor, values, static, payload, probe):
        original = s3.get_object.side_effect
        historical_values = dict(values, **{schema.KEYS[2]: "test-owned-historical-key"})
        historical = codec.encode_document(historical_values, "b" * 32, "then", static["ADMIN_FLEET_CONFIG_ALLOWED_KEYS"])
        s3.get_object.side_effect = lambda **kw: ({"Body": io.BytesIO(historical), "ContentLength": len(historical),
                                                "ETag": '"history"', "Metadata": {"sha256": codec.sha256(historical)}}
                                                if "VersionId" in kw else original(**kw))
        with competing_identity_owner():
            for operation in (
                lambda: topic.mutate(actor, payload),
                lambda: topic.mutate(actor, dict(payload, action="publish", key_action="replace", replacement_key=SECRET)),
                lambda: fleet.publish(actor, {"expected_revision": "a" * 32, "changes": {schema.KEYS[2]: {"action": "set", "value": "test-owned-new-key"}}}),
                lambda: fleet.restore(actor, {"expected_revision": "a" * 32, "version_id": "historical"}),
            ):
                error = refused("configuration_busy", operation)
                assert error.status == 409 and str(error) == "configuration_busy", "Every publisher must preserve the bounded busy 409 rather than uncertain-write or identity errors"
            assert not s3.put_object.called and not probe.called, "Busy publication must neither probe credentials nor attempt a conditional S3 PUT"
            stale = User.objects.create(username="testit-busy-stale-actor", is_active=True, is_superuser=True)
            try:
                User.objects.filter(pk=stale.pk).update(is_superuser=False, permissions={"admin": True})
                with mock.patch.object(provider, "_superuser", side_effect=real_superuser):
                    for operation in (lambda: topic.mutate(stale, payload),
                                      lambda: fleet.publish(stale, {"expected_revision": "a" * 32, "changes": {"GEOIP_PRIMARY_PROVIDER": {"action": "set", "value": "mojo"}}})):
                        with th.assert_raises(me.PermissionDeniedException):
                            operation()
                assert not s3.put_object.called, "A stale actor must be denied before identity contention can mask lost authority"
            finally:
                stale.delete()
            probe.side_effect = RuntimeError(SECRET)
            result = fleet.publish(actor, {"expected_revision": "a" * 32, "changes": {"GEOIP_PRIMARY_PROVIDER": {"action": "set", "value": "mojo"}}})
            assert result["published"] and s3.put_object.call_count == 1, "An unchanged full managed tuple must allow unrelated publication even while identity admission is owned elsewhere"
            assert not probe.called, "Unchanged owned values must bypass identity admission and the unavailable hub"
            sent = s3.put_object.call_args.kwargs
            assert sent["IfMatch"] == '"current"', "Unrelated publication must retain the common conditional-writer fence"
            assert {key: json.loads(sent["Body"])["settings"][key] for key in schema.KEYS} == values, "Unrelated publication must preserve the complete protected tuple"
            s3.put_object.reset_mock()
            with ExitStack() as stack:
                for name in ("_write_configuration_revision", "_write_verify_state", "_save_geoip_secret", "_audit"):
                    stack.enter_context(mock.patch.object(provider, name))
                stack.enter_context(mock.patch.object(provider, "_configuration_revision", return_value=None))
                stack.enter_context(mock.patch.object(provider, "_test_provider_credentials", return_value=({"geoip": {"success": True}}, False)))
                legacy = provider.apply(actor, "geoip", {"expected_revision": "a" * 32, "geoip": {
                    **codec.DEFAULTS, "GEOIP_MOJO_SYNC_ENABLED": True}})
            assert legacy["published"] and s3.put_object.call_count == 1, "The legacy GeoIP publisher must also remain usable while another identity owner holds admission"
            sent = s3.put_object.call_args.kwargs
            assert sent["IfMatch"] == '"current"' and not probe.called, "Legacy GeoIP must preserve conditional publication without contacting the hub"
            assert {key: json.loads(sent["Body"])["settings"][key] for key in schema.KEYS} == values, "Legacy GeoIP publication must retain every protected DNS value"


@th.django_unit_test("identity admission is reentrant and survives inner savepoint exit")
def test_identity_admission_lasts_through_outer_publication_transaction(opts):
    from django.db import connections, transaction
    from mojo.apps.dnsman.services import connection_config
    outcomes = []

    def try_competitor():
        connections.close_all()
        try:
            with connection_config.identity_lock():
                outcomes.append("admitted")
        except connection_config.ConfigurationError as error:
            outcomes.append(error.error_code)
        finally:
            connections.close_all()

    def compete():
        thread = threading.Thread(target=try_competitor)
        thread.start()
        thread.join(3)
        assert not thread.is_alive(), "A competing identity attempt must return without waiting for this transaction"

    with fixture() as (topic, schema, provider, fleet, s3, actor, values, static, payload, probe):
        with transaction.atomic():
            with connection_config.identity_lock():
                with connection_config.identity_lock():
                    topic.publication_policy(values, dict(values, **{schema.KEYS[2]: "test-owned-reentrant-key"}))
            compete()
            assert outcomes == ["configuration_busy"], "Inner/savepoint exit must retain identity ownership until the surrounding publication commits"
        compete()
        assert outcomes == ["configuration_busy", "admitted"], "The outer transaction commit must release identity admission for a deliberate later attempt"


@th.django_unit_test()
def test_installed_catalog_imports_before_django_and_conflicts_fail(opts):
    code = "from mojo.apps.dnsman.fleet_settings import register_hub_profile; register_hub_profile('test',label='Test',origin='https://hub.example',consumer_project_uuid='" + UUID + "',consumer_label='Recipient'); from django.conf import settings; assert not settings.configured, 'schema imports must not initialize Django'"
    env = dict(os.environ)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, timeout=15)
    assert result.returncode == 0, "A trusted profile module must load without Django initialization"
    with fixture() as (topic, schema, *unused):
        with th.assert_raises(ValueError):
            schema.register_hub_profile("test_hub", label="Other", origin="https://hub.example",
                                        consumer_project_uuid=UUID, consumer_label="Recipient")
        for invalid in ("http://public.example", "https://hub.example/path", "https://a:b@hub.example", "https://hub.example?secret=x"):
            with th.assert_raises(ValueError):
                schema.normalize_origin(invalid)
        assert schema.normalize_origin("http://127.0.0.1:8099/") == "http://127.0.0.1:8099", "An installed loopback HTTP profile must retain the client origin semantics"


@th.django_unit_test()
def test_compatibility_credentials_preview_publish_and_write_only_state(opts):
    with fixture() as (topic, schema, provider, fleet, s3, actor, values, static, payload, probe):
        preview = topic.mutate(actor, payload)
        assert preview["can_publish"] and preview["identity"]["sharing_ready"] is False, "Authenticated v2 compatibility mode must allow configuration before activation"
        assert not s3.put_object.called, "Preview must not save a secret or verification state"
        published = topic.mutate(actor, dict(payload, action="publish", key_action="replace", replacement_key="replacement-test-credential"))
        assert published["published"] and published["applied"] is False, "Publishing a verified candidate must not dispatch Apply"
        sent = json.loads(s3.put_object.call_args.kwargs["Body"])["settings"]
        assert sent[schema.KEYS[2]] == "replacement-test-credential", "The conditional writer must receive the actual replacement credential"
        state = topic.state(actor)
        assert state["credential"] == dict(configured=True, source="published", overridden=True), "State must expose credential provenance without a value"
        assert state["loaded_identity"]["observed_project_uuid"] == UUID, "Loaded identity must come from authenticated capabilities"
        assert SECRET not in repr(state) + repr(preview) + repr(published), "State, preview and publication receipts must be write-only"
        assert state["fleet"]["nodes"][0]["hostname"] == "offline", "Topic state must retain the expected offline node"


@th.django_unit_test()
def test_invalid_shape_catalog_revision_and_key_actions(opts):
    with fixture() as (topic, schema, provider, fleet, s3, actor, values, static, payload, probe):
        for delta in ({"origin": "https://attacker.example"}, {"socket": "/tmp/evil"}, {"replacement_key": SECRET}, {"key_action": "erase"}):
            refused("invalid_request", lambda: topic.mutate(actor, dict(payload, **delta)))
        refused("profile_catalog_changed", lambda: topic.mutate(actor, dict(payload, expected_catalog_revision="0" * 64)))
        refused("fleet_revision_changed", lambda: topic.mutate(actor, dict(payload, expected_revision="b" * 32)))
        refused("profile_not_approved", lambda: topic.mutate(actor, dict(payload, profile_id="other")))
        probe.side_effect = lambda *a, **kw: dict(caps(), consumer_project_uuid="e" * 32)
        refused("hub_identity_mismatch", lambda: topic.mutate(actor, payload))
        assert not s3.put_object.called, "No invalid draft may reach S3 publication"


@th.django_unit_test()
def test_common_writer_generic_clear_restore_and_changed_tuple_policy(opts):
    from mojo.deploy import config_override as codec
    with fixture() as (topic, schema, provider, fleet, s3, actor, values, static, payload, probe):
        for change in ({schema.KEYS[0]: {"action": "clear"}}, {schema.KEYS[1]: {"action": "set", "value": "https://elsewhere.example"}}):
            with th.assert_raises(topic.ConfigurationError):
                fleet.publish(actor, {"expected_revision": "a" * 32, "changes": change})
        legacy = codec.encode_document({}, "b" * 32, "then", static["ADMIN_FLEET_CONFIG_ALLOWED_KEYS"])
        original = s3.get_object.side_effect
        s3.get_object.side_effect = lambda **kw: ({"Body": io.BytesIO(legacy), "ContentLength": len(legacy), "ETag": '"old"',
                                                "Metadata": {"sha256": codec.sha256(legacy)}} if "VersionId" in kw else original(**kw))
        refused("hub_identity_migration_required", lambda: fleet.restore(actor, {"expected_revision": "a" * 32, "version_id": "before-topic"}))
        assert not s3.put_object.called, "Generic clear and historical removal must not expose an unvalidated base fallback"
        probe.side_effect = RuntimeError(SECRET)
        published = fleet.publish(actor, {"expected_revision": "a" * 32, "changes": {"GEOIP_PRIMARY_PROVIDER": {"action": "set", "value": "mojo"}}})
        assert published["published"], "Unchanged managed tuples must allow unrelated GeoIP publication during a hub outage"
        assert not probe.called, "Unchanged owned bundle must not probe the unavailable hub"


@th.django_unit_test()
def test_explicit_disable_masks_base_and_connections_block_it(opts):
    with fixture() as (topic, schema, provider, fleet, s3, actor, values, static, payload, probe):
        result = topic.mutate(actor, dict(payload, action="publish", key_action="clear"))
        sent = json.loads(s3.put_object.call_args.kwargs["Body"])["settings"]
        assert all(sent[key] == "" for key in schema.KEYS), "Explicit disable must mask every base tuple value, including the credential"
        assert result["pending_restart"] is True, "Disabled credentials still require node activation"
        with mock.patch.object(topic, "_has_connections", return_value=True):
            refused("connection_disable_ineligible", lambda: topic.mutate(actor, dict(payload, key_action="clear")))
            schema.register_hub_profile("other", label="Other", origin="https://other.example", consumer_project_uuid=UUID, consumer_label="Recipient")
            other = dict(zip(schema.KEYS, ("other", "https://other.example", SECRET)))
            refused("hub_identity_migration_required", lambda: topic.validate_candidate(values, other))


@th.django_unit_test()
def test_empty_replacement_keeps_credential_and_legacy_adoption_requires_origin(opts):
    with fixture() as (topic, schema, provider, fleet, s3, actor, values, static, payload, probe):
        candidate = topic.candidate(dict(payload, key_action="replace", replacement_key=""), values)
        assert candidate[schema.KEYS[2]] == SECRET, "Blank replacement must preserve the effective credential"
        adopted = topic.candidate(payload, {}, loaded=values)
        assert adopted == values, "Same-origin legacy loaded credential must be adopted as a complete tuple"
        refused("hub_identity_mismatch", lambda: topic.candidate(payload, {}, loaded=dict(values, **{schema.KEYS[1]: "https://different.example"})))
        assert not probe.called, "Pure candidate construction must not mutate settings or make a remote call"


@th.django_unit_test()
def test_missing_installation_and_uncertain_publication_are_typed(opts):
    with fixture() as (topic, schema, provider, fleet, s3, actor, values, static, payload, probe):
        for name, code in (("ADMIN_FLEET_CONFIG_ALLOWED_KEYS", "fleet_delegation_unavailable"),
                           ("CONFIG_SYNC_OVERRIDE_ALLOWED_KEYS", "fleet_delegation_unavailable"),
                           ("CONFIG_SYNC_SCHEMA_MODULES", "fleet_schema_unavailable")):
            original = static[name]
            static[name] = []
            assert code in topic.enrollment_reasons(), "Either missing deployment plane must produce an actionable unavailable reason"
            static[name] = original
        with mock.patch.dict(schema._PROFILES, {}, clear=True):
            assert "profile_catalog_unavailable" in topic.enrollment_reasons(), "A deployment without approved catalog must remain unavailable"
        s3.put_object.side_effect = RuntimeError(SECRET)
        refused("publication_uncertain", lambda: topic.mutate(actor, dict(payload, action="publish", key_action="replace", replacement_key="new-test-secret")))
        assert topic.state(actor)["revision"] == "a" * 32, "After uncertain write the authoritative head must be refetchable without replay"


@th.django_unit_test()
def test_preparation_checks_current_published_tuple_against_loaded_identity(opts):
    with fixture() as (topic, schema, provider, fleet, s3, actor, values, static, payload, probe):
        topic.require_current_identity(UUID, current_loader=lambda: values, static_values=values)
        changed = dict(values, **{schema.KEYS[2]: "replacement-test-credential"})
        refused("hub_identity_migration_required", lambda: topic.require_current_identity(UUID, current_loader=lambda: changed, static_values=values))
        refused("hub_identity_migration_required", lambda: topic.require_current_identity("e" * 32, current_loader=lambda: values, static_values=values))
        refused("hub_identity_migration_required", lambda: topic.require_current_identity(UUID, current_loader=lambda: dict.fromkeys(schema.KEYS, ""), static_values=values))


@th.django_unit_test()
def test_ambiguous_success_refetches_head_without_replaying_secret_mutation(opts):
    from mojo.deploy import config_override as codec
    with fixture() as (topic, schema, provider, fleet, s3, actor, values, static, payload, probe):
        previous = s3.get_object.side_effect
        stored = {}
        def put_then_lose_reply(**arguments):
            stored["Body"] = arguments["Body"]
            raise TimeoutError("test-owned lost response")
        def authoritative_head(**arguments):
            if "Body" not in stored:
                return previous(**arguments)
            body = stored["Body"]
            return {"Body": io.BytesIO(body), "ContentLength": len(body), "ETag": '"new"',
                    "VersionId": "new", "Metadata": {"sha256": codec.sha256(body)}}
        s3.put_object.side_effect = put_then_lose_reply
        s3.get_object.side_effect = authoritative_head
        refused("publication_uncertain", lambda: topic.mutate(actor, dict(payload, action="publish", key_action="replace", replacement_key="new-test-secret")))
        state = topic.state(actor)
        assert state["revision"] == json.loads(stored["Body"])["revision"] and state["revision"] != "a" * 32, "Authoritative refetch must reveal an S3 write that succeeded despite a lost response"
        assert s3.put_object.call_count == 1, "Uncertain publication must never automatically replay the secret mutation"
        assert "new-test-secret" not in repr(state), "Reconciled authoritative state must remain credential-free"


@th.django_unit_test()
def test_candidate_transport_is_explicit_static_independent_and_proof_bounded(opts):
    from mojo.apps.dnsman.services import acme_hub_client as client
    from unittest import mock as mocking
    data = caps()
    response = mocking.Mock(status_code=200, headers={"Content-Type": "application/json"})
    response.iter_content.return_value = [json.dumps({"status": True, "data": data}).encode()]
    post = mocking.Mock(return_value=response)
    result = client.capabilities_for("https://hub.example", SECRET, post=post, proof=True)
    assert result["consumer_project_uuid"] == UUID and result["shared_validation"] is False, "The explicit transport must validate authenticated v2 compatibility capabilities"
    assert post.call_args.args == ("https://hub.example/api/dnsman/acme/capabilities",), "Candidate transport may use only the approved fixed capabilities endpoint"
    assert post.call_args.kwargs["timeout"] == (0.5, 1.5) and post.call_count == 1, "Loaded credential proof must use bounded proof timeouts without retries"
    assert post.call_args.kwargs["allow_redirects"] is False and response.close.called, "Candidate proof must never follow redirects or leave response streams open"


@th.django_unit_test()
def test_legacy_provider_get_and_unmanaged_health_never_expose_or_probe_dns_key(opts):
    from mojo.deploy import config_override as codec, fleet_config_node as node
    with fixture() as (topic, schema, provider, fleet, s3, actor, values, static, payload, probe):
        assert not set(schema.KEYS) & set(codec.DEFAULTS), "DNS schema registration must not expand legacy GeoIP defaults"
        assert not set(schema.KEYS) & set(provider.FLEET_KEYS), "Legacy provider fields must remain GeoIP-only"
        assert SECRET not in repr(provider._loaded_values()), "Legacy loaded provider values must never include DNS credentials"
        # Provider state reads its own database/setup metadata; the external
        # credential boundary remains test-owned and cannot contact providers.
        state = provider.state(actor)
        assert SECRET not in repr(state), "Legacy provider GET state must remain credential-free after DNS schema registration"
        for mode in ("unmanaged", "undelegated", "disabled"):
            static[schema.KEYS[0]] = "" if mode != "undelegated" else "test_hub"
            static["ADMIN_FLEET_CONFIG_ALLOWED_KEYS"] = list(codec.DEFAULTS) if mode == "undelegated" else list(schema.KEYS)
            evidence = node.loaded_topic_identity()
            assert evidence["state"] == "disabled", "Unmanaged, undelegated and disabled topics must not block generic Fleet proof"
        assert not probe.called, "Unmanaged legacy Fleet proof and provider GET must never contact the hub"
