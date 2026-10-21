"""MojoSec-only installations receive useful, repeatable incident defaults."""
from testit import helpers as th


@th.django_unit_test()
def test_default_setup_includes_mojosec_without_ossec_ingest(opts):
    from mojo.apps.incident.models import RuleSet

    RuleSet.objects.filter(category__startswith="mojosec.").delete()
    try:
        RuleSet.ensure_default_rules()
        assert RuleSet.objects.filter(
            category="mojosec.auth.ssh_login", handler="ignore").exists(), \
            "Default setup must seed routine MojoSec SSH handling without an OSSEC request"
        assert RuleSet.objects.filter(
            category="mojosec.web.probe", handler__startswith="block://").exists(), \
            "Default setup must seed bounded MojoSec scanner blocking"
        assert RuleSet.objects.filter(category="mojosec.fim.overflow").exists(), \
            "Monitoring failures must remain visible on MojoSec-only installations"
    finally:
        RuleSet.objects.filter(category__startswith="mojosec.").delete()


def _clear():
    from mojo.apps.incident.models import RuleSet
    RuleSet.objects.filter(category__startswith="mojosec.").delete()


@th.django_unit_test()
def test_repeated_setup_preserves_customized_and_disabled_defaults(opts):
    from mojo.apps.incident.models import RuleSet
    from mojo.apps.incident.services import mojosec_defaults, rule_validation

    _clear()
    try:
        first = mojosec_defaults.ensure_defaults()
        assert first["created"], "Fresh setup must create MojoSec policies"
        row = RuleSet.objects.get(name="MojoSec - Routine public-key SSH")
        row.priority = 77
        row.is_active = False
        row.save(update_fields=["priority", "is_active", "modified"])
        before = RuleSet.objects.filter(category__startswith="mojosec.").count()
        again = mojosec_defaults.ensure_defaults()
        row.refresh_from_db()
        assert not again["create"], "Repeated setup must not duplicate defaults"
        assert row.priority == 77 and row.is_active is False, \
            "Setup must preserve operator edits and deliberate deactivation"
        assert RuleSet.objects.filter(category__startswith="mojosec.").count() == before, \
            "Idempotent setup must preserve the policy count"
        for policy in RuleSet.objects.filter(category__startswith="mojosec."):
            assert rule_validation.validation_summary(policy)["status"] == "valid", \
                "Every new default must be accepted by the governed policy authority"
    finally:
        _clear()


@th.django_unit_test()
def test_existing_operator_category_is_not_preempted(opts):
    from mojo.apps.incident.models import RuleSet
    from mojo.apps.incident.services import mojosec_defaults

    _clear()
    try:
        row = RuleSet.objects.create(
            category="mojosec.web.probe", name="My scanner policy", priority=500,
            handler="", metadata={"operator": True})
        revision = row.modified
        result = mojosec_defaults.ensure_defaults()
        row.refresh_from_db()
        assert RuleSet.objects.filter(category="mojosec.web.probe").count() == 1, \
            "Automatic defaults must not preempt an existing operator policy"
        assert row.modified == revision and row.priority == 500, \
            "Automatic setup must not rewrite customized policy"
        assert any(item["category"] == row.category for item in result["preserved"]), \
            "Setup must report the category requiring operator review"
    finally:
        _clear()


def _visibility(kind):
    from mojo.apps.incident.models import RuleSet
    return RuleSet.objects.create(
        category="mojosec." + kind, name="WMWX mojosec." + kind,
        priority=20, bundle_by=1, bundle_minutes=60, match_by=0,
        handler=None, metadata={
            "managed_by": "wmx", "purpose": "mojosec_incident_visibility"})


@th.django_unit_test()
def test_visibility_repair_is_explicit_versioned_and_preserves_customizations(opts):
    from mojo.apps.incident.models import RuleSet
    from mojo.apps.incident.services import mojosec_defaults

    _clear()
    try:
        legacy = _visibility("auth.ssh_login")
        custom = _visibility("web.probe")
        custom.priority = 21
        custom.save(update_fields=["priority", "modified"])
        mojosec_defaults.ensure_defaults()
        assert RuleSet.objects.filter(category=legacy.category).count() == 1, \
            "Automatic setup must leave legacy visibility repair to an explicit action"
        preview = mojosec_defaults.setup_defaults(repair_visibility=True)
        legacy.refresh_from_db()
        assert preview["retire"] == [legacy.pk] and legacy.is_active, \
            "Preview must select only the exact legacy shape and perform no writes"
        try:
            mojosec_defaults.setup_defaults(apply=True, repair_visibility=True)
        except ValueError:
            pass
        else:
            raise AssertionError("Visibility repair must require a preview revision")
        legacy.priority = 22
        legacy.save(update_fields=["priority", "modified"])
        try:
            mojosec_defaults.setup_defaults(
                apply=True, repair_visibility=True, expected_revision=preview["revision"])
        except ValueError:
            pass
        else:
            raise AssertionError("Changed policies must invalidate an older repair preview")
        legacy.priority = 20
        legacy.save(update_fields=["priority", "modified"])
        preview = mojosec_defaults.setup_defaults(repair_visibility=True)
        result = mojosec_defaults.setup_defaults(
            apply=True, repair_visibility=True, expected_revision=preview["revision"])
        legacy.refresh_from_db()
        custom.refresh_from_db()
        assert legacy.is_active is False and custom.is_active is True, \
            "Repair must retire only the recognized unchanged visibility rule"
        assert result["created"] and RuleSet.objects.filter(
            name="MojoSec - Routine public-key SSH", is_active=True).exists(), \
            "Explicit repair must install the replacement default behavior"
        again = mojosec_defaults.setup_defaults(repair_visibility=True)
        assert not again["retire"] and not again["create"], \
            "Repeating visibility repair must be idempotent"
        assert RuleSet.objects.filter(category=custom.category).count() == 1, \
            "The edited visibility rule must remain the category's sole policy"
    finally:
        _clear()


@th.django_unit_test()
def test_lifecycle_setup_and_real_failures_stay_visible(opts):
    from mojo.apps.incident.models import Event
    from mojo.apps.incident.services import mojosec_defaults

    _clear()
    try:
        mojosec_defaults.after_migrate(sender=None)
        for kind, level in (
                ("auth.ssh_failure", 5), ("auth.sudo_failure", 8),
                ("auth.sudo_command", 8), ("fim.change", 8),
                ("fim.overflow", 12), ("fim.expected_change_error", 8),
                ("system.oom", 12), ("system.service_error", 8)):
            event = Event.objects.create(category="mojosec." + kind,
                scope="mojosec", level=level, hostname="defaults-test")
            publication = event.publish(exact_category=True, use_catchall=False,
                dispatch_handlers=False, allow_default_llm=False)
            assert publication["incident"] is not None, \
                "Unproven or failed %s evidence must remain actionable" % kind
    finally:
        Event.objects.filter(hostname="defaults-test").delete()
        _clear()


@th.django_unit_test()
def test_first_sensor_batch_seeds_defaults_and_replay_keeps_audit_without_noise(opts):
    from mojo.apps.incident.models import Event, MojoSecReceipt, RuleSet
    from mojo.apps.incident.services import mojosec
    from mojo.mojosec import protocol
    from .mojosec_rule_facts import _fixture, _receipt

    _clear()
    try:
        with _fixture("defaults-ingest") as (key, sensor):
            initial, initial_event = _receipt(key, sensor, "auth.ssh_login", {
                "source_ip": "192.0.2.187", "user": "deploy", "auth_method": "publickey"})
            wire = initial.replay_features["event"]
            initial.delete()
            initial_event.delete()
            batch = protocol.make_batch(sensor, [wire], policy_revision="facts-test")
            result = mojosec.ingest_batch(key, batch, volume_observer=lambda *args: None)
            assert result["results"][0]["status"] == "accepted", \
                "First sensor use must install defaults and acknowledge durable evidence"
            receipt = MojoSecReceipt.objects.get(api_key=key, wire_event_id=wire["id"])
            assert receipt.incident_id is None and receipt.event_id is not None, \
                "A routine successful sign-in must retain its Event without creating incident noise"
            assert receipt.event.model_id == key.pk, \
                "Sensor enrollment must provide a non-null host bundling identity"
            assert RuleSet.objects.filter(category="mojosec.web.probe").exists(), \
                "MojoSec ingestion must seed defaults without legacy OSSEC traffic"
            again = mojosec.ingest_batch(key, batch, volume_observer=lambda *args: None)
            assert again["results"][0]["status"] == "duplicate", \
                "Redelivery must acknowledge the existing receipt without duplicate publication"
            assert MojoSecReceipt.objects.filter(api_key=key, wire_event_id=wire["id"]).count() == 1, \
                "Redelivery must not grow the receipt ledger"
            assert Event.objects.filter(pk=receipt.event_id).exists(), \
                "Ignore handling must preserve the raw Event audit"
    finally:
        _clear()


@th.django_unit_test()
def test_concurrent_setup_does_not_duplicate_rulesets(opts):
    from concurrent.futures import ThreadPoolExecutor
    from django.db import connections
    from mojo.apps.incident.models import RuleSet
    from mojo.apps.incident.services import mojosec_defaults

    _clear()

    def seed():
        try:
            return mojosec_defaults.ensure_defaults()
        finally:
            connections.close_all()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda unused: seed(), range(2)))
        assert all(isinstance(result, dict) for result in results), \
            "Concurrent worker startup must complete setup successfully"
        assert RuleSet.objects.filter(category__startswith="mojosec.").count() == len(mojosec_defaults.policies()), \
            "Concurrent setup must create exactly one copy of every named default"
    finally:
        _clear()


@th.django_unit_test()
def test_partial_migration_does_not_seed_against_a_newer_model_schema(opts):
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from mojo.apps.incident.models import RuleSet
    from mojo.apps.incident.services import mojosec_defaults

    _clear()
    try:
        historical = MigrationExecutor(connection).loader.project_state([
            ("incident", "0001_initial")]).apps
        mojosec_defaults.after_migrate(sender=None, apps=historical)
        assert not RuleSet.objects.filter(category__startswith="mojosec.").exists(), \
            "A partial incident migration must not seed policies using the current schema"
        mojosec_defaults.after_migrate(sender=None, using="replica")
        assert not RuleSet.objects.filter(category__startswith="mojosec.").exists(), \
            "A non-default database migration must not seed the default database"
        mojosec_defaults.after_migrate(sender=None)
        assert RuleSet.objects.filter(category="mojosec.web.probe").exists(), \
            "Normal complete migrations must still seed MojoSec defaults"
    finally:
        _clear()


@th.django_unit_test()
def test_default_scanner_rechecks_freshness_and_requires_verified_fleet_enforcement(opts):
    from types import SimpleNamespace
    from unittest import mock
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.models import Incident, MojoSecReceipt
    from mojo.apps.incident.services import mojosec_defaults
    from mojo.apps.incident.handlers.event_handlers import execute_handler
    from mojo.helpers import dates
    from .mojosec_rule_facts import _fixture, _receipt

    _clear()
    source_ip = "203.0.115.187"
    GeoLocatedIP.objects.filter(ip_address=source_ip).delete()
    try:
        mojosec_defaults.ensure_defaults()
        geo = GeoLocatedIP.objects.create(ip_address=source_ip)
        with _fixture("defaults-block") as (key, sensor):
            for path in ("/.env", "/.git/config", "/.git/HEAD",
                         "/vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php"):
                receipt, event = _receipt(key, sensor, "web.probe", {
                    "source_ip": source_ip, "path": path, "status": 404})
            with mock.patch.object(GeoLocatedIP, "geolocate", return_value=geo):
                publication = event.publish(exact_category=True, use_catchall=False,
                    dispatch_handlers=False, allow_default_llm=False)
            policy = publication["rule_set"]
            incident = publication["incident"]
            assert policy.name == "MojoSec - Repeated sensitive-path scanner", \
                "Repeated confirmed scanning must select the bounded fleet block policy"
            queued = []

            def capture(function, payload, **kwargs):
                queued.append(payload)
                return "test-default-scanner-job"

            assert policy.run_handler(event, incident, publisher=capture), \
                "The default scanner action must be durably dispatchable"
            assert queued[0].get("require_fresh_mojosec_scanner") is True, \
                "The queued default action must carry an enforcement-time freshness requirement"
            job = SimpleNamespace(payload=queued[0])
            MojoSecReceipt.objects.filter(pk=receipt.pk).update(
                created=dates.utcnow() - dates.timedelta(minutes=11))
            with mock.patch.object(GeoLocatedIP, "block_checked") as block:
                assert execute_handler(job) is False, \
                    "A delayed default job must not block using stale scanner evidence"
                assert not block.called, "Stale evidence must be refused before firewall writes"
            incident.refresh_from_db()
            assert incident.status not in ("resolved", "ignored"), \
                "Skipping a stale action must leave its incident visible"
            MojoSecReceipt.objects.filter(pk=receipt.pk).update(created=dates.utcnow())
            with mock.patch.object(GeoLocatedIP, "block_checked", return_value={
                    "status": "partial", "ok": False, "error": {"code": "missing_host"}}):
                assert execute_handler(job) is False, \
                    "Partial fleet proof must not count as a successful default block"
            incident.refresh_from_db()
            assert incident.status not in ("resolved", "ignored"), \
                "An unverified default block must keep the incident open"
            with mock.patch.object(GeoLocatedIP, "block_checked", return_value={
                    "status": "verified", "ok": True}):
                assert execute_handler(job) is True, \
                    "A fresh scanner action with checked fleet proof should succeed"
            incident.refresh_from_db()
            assert incident.status == "resolved", \
                "Only verified fleet enforcement may resolve the default scanner incident"
            assert incident.events.exists(), "Successful enforcement must preserve Event audit"
            Incident.objects.filter(pk=incident.pk).delete()
    finally:
        GeoLocatedIP.objects.filter(ip_address=source_ip).delete()
        _clear()
