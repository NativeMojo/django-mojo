"""Receipt-backed facts must be the only input to packaged MojoSec policy."""

import contextlib
import copy
import datetime
import uuid
from unittest import mock

from testit import helpers as th


PREFIX = "mojosec-rule-facts-test-"


@contextlib.contextmanager
def _fixture(label):
    from mojo.apps.account.models import ApiKey, Group
    from mojo.apps.incident.models import Event, Incident, MojoSecDeployment, MojoSecReceipt, RuleSet

    sensor_id = PREFIX + label

    def clean():
        Incident.objects.filter(events__metadata__mojosec__sensor_id=sensor_id).delete()
        MojoSecReceipt.objects.filter(sensor_id=sensor_id).delete()
        Event.objects.filter(metadata__mojosec__sensor_id=sensor_id).delete()
        Event.objects.filter(title=sensor_id).delete()
        RuleSet.objects.filter(name=sensor_id).delete()
        MojoSecDeployment.objects.filter(installation_key__name=sensor_id).delete()
        ApiKey.objects.filter(name=sensor_id).delete()
        Group.objects.filter(name=sensor_id).delete()

    clean()
    group = Group.objects.create(name=sensor_id, kind="organization")
    key, unused_token = ApiKey.create_for_group(
        group, sensor_id, permissions={"mojosec_ingest": True})
    try:
        yield key, sensor_id
    finally:
        clean()


def _receipt(key, sensor_id, kind, attributes, count=1, observed=None):
    from mojo.apps.incident.services import mojosec
    from mojo.mojosec import protocol

    observed = observed or protocol.utc_now()
    wire = {
        "id": uuid.uuid4().hex + uuid.uuid4().hex,
        "kind": kind, "observed_at": observed,
        "first_seen": observed, "last_seen": observed,
        "severity": "high", "summary": "Rule facts fixture",
        "count": count, "attributes": copy.deepcopy(attributes),
        "recommendation": "review",
    }
    batch = protocol.make_batch(sensor_id, [wire], policy_revision="facts-test")
    receipt, created = mojosec._create_receipt(
        key, batch, wire, mojosec._payload_digest(wire))
    th.assert_true(created, "each fixture must start with a distinct durable receipt")
    return receipt, receipt.event


def _web(path, source_ip="192.0.2.91", status=404, **extra):
    return dict({"source_ip": source_ip, "path": path, "status": status}, **extra)


@th.django_unit_test()
def test_reserved_schema_and_descriptive_facts_ignore_metadata(opts):
    from mojo.apps.incident.services import mojosec_rule_facts as facts

    expected = {
        "mojosec_kind": "str", "mojosec_user": "str",
        "mojosec_auth_method": "str", "mojosec_path": "str",
        "mojosec_status": "int", "mojosec_response_class": "str",
        "mojosec_command_path": "str", "mojosec_proof_status": "str",
        "mojosec_expected_change": "bool", "mojosec_routine": "bool",
        "mojosec_scanner_confirmed": "bool",
    }
    th.assert_eq(facts.FIELD_SCHEMAS, expected,
                 "the public rule vocabulary must expose fixed typed MojoSec facts")
    with _fixture("descriptive") as (key, sensor):
        unused_receipt, event = _receipt(key, sensor, "web.probe", _web(
            "/.git/config?token=private", status="404", response_class="impossible_path"))
        event.metadata = {
            "mojosec_kind": "auth.ssh_login", "mojosec_status": 200,
            "mojosec_routine": True,
            "mojosec": {"kind": "auth.ssh_login", "evidence": {"path": "/safe"}},
        }
        th.assert_eq(facts.field_value(event, "mojosec_kind"), "web.probe",
                     "the retained wire kind must win over flat and nested metadata")
        th.assert_eq(facts.field_value(event, "mojosec_status"), 404,
                     "status must be the typed receiver projection of retained evidence")
        th.assert_eq(facts.field_value(event, "mojosec_path"), "/.git/config",
                     "rule paths must omit query secrets through the existing projector")
        th.assert_eq(facts.field_value(event, "mojosec_response_class"), "impossible_path",
                     "a valid response-class claim may remain descriptive evidence")
        th.assert_eq(facts.field_value(event, "mojosec_routine"), False,
                     "a response-class claim alone must never buy routine suppression")


@th.django_unit_test()
def test_generic_events_cannot_forge_mojosec_facts(opts):
    from mojo.apps.incident.models import Event
    from mojo.apps.incident.services import mojosec_rule_facts as facts

    with _fixture("forgery") as (unused_key, sensor):
        event = Event.objects.create(
            title=sensor, scope="mojosec", category="mojosec.web.probe",
            source_ip="192.0.2.91", metadata={
                "mojosec_kind": "web.probe", "mojosec_scanner_confirmed": True,
                "mojosec_routine": True, "mojosec_expected_change": True,
                "mojosec": {"kind": "web.probe", "evidence": {
                    "path": "/.env", "status": 404, "proof_status": "proven"}},
            })
        for name in facts.FIELD_SCHEMAS:
            th.assert_eq(facts.field_value(event, name), None,
                         f"a generic Event without a receipt must not manufacture {name}")
        th.assert_eq(facts.field_value(event, "source_ip"), None,
                     "the sensor resolver must never resolve unrelated rule fields")


@th.django_unit_test()
def test_receipt_digest_identity_schema_and_authoritative_columns_are_checked(opts):
    from mojo.apps.incident.services import mojosec, mojosec_rule_facts as facts

    with _fixture("binding") as (key, sensor):
        for mutation in ("digest", "wire_id", "sensor", "schema", "version",
                         "protocol", "scope", "category", "model_id", "source", "null_source"):
            receipt, event = _receipt(key, sensor, "web.probe", _web("/.env"))
            replay = copy.deepcopy(receipt.replay_features)
            if mutation == "digest":
                replay["event"]["attributes"]["path"] = "/.git/config"
            elif mutation == "wire_id":
                replay["event"]["id"] = "f" * 64
                receipt.payload_digest = mojosec._payload_digest(replay["event"])
            elif mutation == "sensor":
                replay["sensor_id"] = "another-sensor"
            elif mutation == "schema":
                replay["feature_schema"] = "invented-replay"
            elif mutation == "version":
                replay["version"] = True
            elif mutation == "protocol":
                replay["event"]["count"] = True
                receipt.payload_digest = mojosec._payload_digest(replay["event"])
            elif mutation == "scope":
                event.scope = "global"
            elif mutation == "category":
                event.category = "mojosec.auth.ssh_login"
            elif mutation == "model_id":
                event.model_id = key.pk + 1
            else:
                event.source_ip = None if mutation == "null_source" else "192.0.2.92"
                event.metadata["source_ip"] = "192.0.2.91"
            receipt.replay_features = replay
            receipt.save(update_fields=["replay_features", "payload_digest"])
            th.assert_eq(facts.field_value(event, "mojosec_kind"), None,
                         f"{mutation} mismatch must invalidate every actionable sensor fact")
            th.assert_eq(facts.field_value(event, "mojosec_scanner_confirmed"), None,
                         f"{mutation} mismatch must not become a scanner decision")


@th.django_unit_test()
def test_multiple_receipts_for_one_event_fail_closed(opts):
    from mojo.apps.incident.models import MojoSecReceipt
    from mojo.apps.incident.services import mojosec_rule_facts as facts

    with _fixture("ambiguous") as (key, sensor):
        unused_first, event = _receipt(key, sensor, "web.probe", _web("/.env"))
        second, unused_event = _receipt(key, sensor, "web.probe", _web("/.git/config"))
        MojoSecReceipt.objects.filter(pk=second.pk).update(event=event)
        th.assert_eq(facts.field_value(event, "mojosec_kind"), None,
                     "an Event with ambiguous durable provenance must not yield facts")


@th.django_unit_test()
def test_publickey_ssh_success_is_routine_until_recent_source_failure(opts):
    from mojo.apps.incident.services import mojosec_rule_facts as facts

    with _fixture("ssh") as (key, sensor):
        attributes = {"source_ip": "192.0.2.93", "user": "deploy", "auth_method": "publickey"}
        unused_receipt, clean = _receipt(key, sensor, "auth.ssh_login", attributes)
        th.assert_eq(facts.field_value(clean, "mojosec_user"), "deploy",
                     "the trusted successful SSH account must be available to policy")
        th.assert_eq(facts.field_value(clean, "mojosec_auth_method"), "PUBLICKEY",
                     "authentication methods must use the receiver's canonical form")
        th.assert_eq(facts.field_value(clean, "mojosec_routine"), True,
                     "a clean validated public-key SSH success should stay quiet")
        _receipt(key, sensor, "auth.ssh_failure", dict(attributes, user="root"))
        unused_receipt, after_failure = _receipt(key, sensor, "auth.ssh_login", attributes)
        th.assert_eq(facts.field_value(after_failure, "mojosec_routine"), False,
                     "success from a source with recent failed SSH attempts must remain visible")
        for malformed in (dict(attributes, auth_method="password"),
                          dict(attributes, user=7), dict(attributes, auth_method=7)):
            unused_receipt, event = _receipt(key, sensor, "auth.ssh_login", malformed)
            th.assert_eq(facts.field_value(event, "mojosec_routine"), False,
                         f"unproved user or authentication evidence must remain visible: {malformed}")


@th.django_unit_test()
def test_expected_fim_requires_bounded_annotation_and_live_central_registration(opts):
    from mojo.apps.incident.models import MojoSecDeployment
    from mojo.apps.incident.services import mojosec_rule_facts as facts
    from mojo.helpers import dates
    from mojo.mojosec import protocol

    with _fixture("fim") as (key, sensor):
        now = dates.utcnow()
        observed = protocol.normalize_timestamp(now.isoformat())
        annotation = {"deployment_id": "facts-deploy", "expires_at":
                      (now + datetime.timedelta(minutes=5)).isoformat()}
        attributes = {"path": "/etc/ssh/sshd_config", "expected_change": annotation}
        unused_receipt, unregistered = _receipt(
            key, sensor, "fim.change", attributes, observed=observed)
        th.assert_eq(facts.field_value(unregistered, "mojosec_expected_change"), False,
                     "sensor annotation alone must not suppress an unregistered change")
        registration = MojoSecDeployment.objects.create(
            installation_key=key, deployment_id="facts-deploy",
            expires_at=now + datetime.timedelta(minutes=10))
        unused_receipt, registered = _receipt(
            key, sensor, "fim.change", attributes, observed=observed)
        th.assert_eq(facts.field_value(registered, "mojosec_expected_change"), True,
                     "an exact bounded annotation and live registration should prove expected change")
        th.assert_eq(facts.field_value(registered, "mojosec_routine"), True,
                     "a centrally registered expected change should be routine")
        for expiry in (now - datetime.timedelta(seconds=1),
                       now + datetime.timedelta(seconds=901)):
            invalid = dict(attributes, expected_change=dict(annotation, expires_at=expiry.isoformat()))
            unused_receipt, event = _receipt(key, sensor, "fim.change", invalid, observed=observed)
            th.assert_eq(facts.field_value(event, "mojosec_expected_change"), False,
                         "expired or overlong annotations must not buy suppression")
        registration.expires_at = now - datetime.timedelta(seconds=1)
        registration.save(update_fields=["expires_at"])
        unused_receipt, stale = _receipt(key, sensor, "fim.change", attributes, observed=observed)
        th.assert_eq(facts.field_value(stale, "mojosec_expected_change"), False,
                     "a backdated wire observation must not revive expired central registration")


@th.django_unit_test()
def test_only_complete_exact_routine_command_proof_can_suppress_sudo(opts):
    from mojo.apps.incident.services import mojosec_rule_facts as facts
    from mojo.mojosec.disposition import APP_USER_FIREWALL_BROKER_CLASSIFIER

    with _fixture("sudo") as (key, sensor):
        attributes = {
            "actor": "ec2-user", "target_user": "root",
            "command": "/usr/local/sbin/mojo-firewall-broker",
            "command_path": "/usr/local/sbin/mojo-firewall-broker",
            "attribution_provenance": "none",
            "local_disposition": APP_USER_FIREWALL_BROKER_CLASSIFIER,
            "audit_loginuid": 1000, "boot_id": "a" * 32, "audit_session": 9,
        }
        unused_receipt, expected = _receipt(key, sensor, "auth.sudo_command", attributes)
        th.assert_eq(facts.field_value(expected, "mojosec_routine"), True,
                     "the exact expected app-user broker sudo may stay quiet")
        for changes in ({"local_disposition": "jobman_firewall_operation_v1"},
                        {"tty": "pts/1"}, {"command_truncated": True},
                        {"command": attributes["command"] + " --status"},
                        {"target_user": "ec2-user"}, {"audit_loginuid": None},
                        {"attribution_provenance": "audit_session", "source_ip": "192.0.2.91"},
                        {"source_ip": "192.0.2.91"},
                        {"command_path": "/usr/bin/systemctl", "command": "/usr/bin/systemctl restart api"}):
            unused_receipt, event = _receipt(
                key, sensor, "auth.sudo_command", dict(attributes, **changes))
            th.assert_eq(facts.field_value(event, "mojosec_routine"), False,
                         f"a routine command near match must remain visible: {changes}")


@th.django_unit_test()
def test_scanner_requires_distinct_sensitive_paths_and_receipts(opts):
    from mojo.apps.incident.services import mojosec, mojosec_rule_facts as facts
    from mojo.mojosec import protocol

    with _fixture("scanner") as (key, sensor):
        first, event = _receipt(key, sensor, "web.probe", _web("/.env"), count=1000000000)
        th.assert_eq(facts.field_value(event, "mojosec_scanner_confirmed"), False,
                     "one oversized wire count must never substitute for distinct receipts")
        wire = first.replay_features["event"]
        duplicate, created = mojosec._create_receipt(
            key, protocol.make_batch(sensor, [wire], policy_revision="facts-test"),
            wire, mojosec._payload_digest(wire))
        th.assert_true(not created and duplicate.pk == first.pk,
                       "redelivery must retain the original receipt rather than add scanner weight")
        for path in ("/.env", "/normal-missing-page", "/contact.php", "/about.php"):
            unused_receipt, event = _receipt(key, sensor, "web.probe", _web(path))
            th.assert_eq(facts.field_value(event, "mojosec_scanner_confirmed"), False,
                         f"duplicates, a normal 404, or a broad PHP suffix must not prove scanning: {path}")
        unused_receipt, second = _receipt(key, sensor, "web.probe", _web("/.git/config"))
        th.assert_eq(facts.field_value(second, "mojosec_scanner_confirmed"), False,
                     "two distinct sensitive paths must remain below the blocking threshold")
        unused_receipt, third = _receipt(key, sensor, "web.probe", _web(
            "/vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php"))
        th.assert_eq(facts.field_value(third, "mojosec_scanner_confirmed"), True,
                     "three validated receipts on distinct sensitive paths should prove repeated scanning")


@th.django_unit_test()
def test_scanner_window_uses_server_receipts_and_rejects_corrupted_candidates(opts):
    from mojo.apps.incident.models import MojoSecReceipt
    from mojo.apps.incident.services import mojosec_rule_facts as facts
    from mojo.helpers import dates

    with _fixture("scanner-window") as (key, sensor):
        old, unused_event = _receipt(key, sensor, "web.probe", _web("/.env"))
        MojoSecReceipt.objects.filter(pk=old.pk).update(
            created=dates.utcnow() - datetime.timedelta(minutes=11))
        invalid, invalid_event = _receipt(key, sensor, "web.probe", _web("/.git/config"))
        invalid.payload_digest = "0" * 64
        invalid.save(update_fields=["payload_digest"])
        _receipt(key, sensor, "web.probe", _web("/.git/HEAD", source_ip="192.0.2.92"))
        unused_receipt, event = _receipt(key, sensor, "web.probe", _web(
            "/vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php"))
        th.assert_eq(facts.field_value(event, "mojosec_scanner_confirmed"), False,
                     "expired, corrupt, or different-source receipts must not complete a scanner threshold")
        _receipt(key, sensor, "web.probe", _web("/.env"), observed="2000-01-01T00:00:00Z")
        unused_receipt, event = _receipt(key, sensor, "web.probe", _web("/.git/config"))
        th.assert_eq(facts.field_value(event, "mojosec_scanner_confirmed"), False,
                     "a newly received stale spool event must not complete an automatic-block threshold")
        unused_receipt, event = _receipt(key, sensor, "web.probe", _web("/.env"))
        th.assert_eq(facts.field_value(event, "mojosec_scanner_confirmed"), True,
                     "three current valid observations and server receipts should still prove scanning")


@th.django_unit_test()
def test_real_failures_never_become_routine_from_host_claims(opts):
    from mojo.apps.incident.services import mojosec_rule_facts as facts

    with _fixture("failures") as (key, sensor):
        for kind in ("system.oom", "system.service_error", "auth.sudo_failure",
                     "auth.ssh_failure", "fim.overflow", "fim.expected_change_error"):
            unused_receipt, event = _receipt(key, sensor, kind, {
                "user": "deploy", "auth_method": "publickey",
                "proof_status": "proven", "response_class": "spa_fallback",
                "status": 200, "expected_change": {"deployment_id": "claimed"},
            })
            th.assert_eq(facts.field_value(event, "mojosec_routine"), False,
                         f"{kind} must remain visible despite unrelated host claims")
            th.assert_eq(facts.field_value(event, "mojosec_expected_change"), False,
                         f"{kind} must not inherit an expected-change exemption")


@th.django_unit_test()
def test_facts_cache_and_context_queries_are_bounded(opts):
    from django.db import connection
    from mojo.apps.incident.services import mojosec_rule_facts as facts

    queries = []

    def count_query(execute, sql, params, many, context):
        queries.append(sql)
        return execute(sql, params, many, context)

    with _fixture("cache") as (key, sensor):
        unused_receipt, event = _receipt(key, sensor, "web.probe", _web("/.env"))
        with connection.execute_wrapper(count_query):
            for name in ("mojosec_kind", "mojosec_path", "mojosec_status", "mojosec_response_class"):
                facts.field_value(event, name)
        th.assert_eq(len(queries), 1,
                     "all descriptive fields for one Event must share one receipt lookup")
        queries.clear()
        with connection.execute_wrapper(count_query):
            facts.field_value(event, "mojosec_scanner_confirmed")
        th.assert_eq(len(queries), 1,
                     "scanner context must use one bounded query rather than one query per candidate")
        th.assert_true("LIMIT 256" in queries[0].upper(),
                       "scanner context must never scan unbounded historical receipts")
        queries.clear()
        with connection.execute_wrapper(count_query):
            facts.field_value(event, "mojosec_kind")
            facts.field_value(event, "mojosec_scanner_confirmed")
        th.assert_eq(len(queries), 0,
                     "repeated rule evaluation must reuse the Event's validated receipt and scanner cache")


@th.django_unit_test()
def test_sensitive_path_redirect_sweeps_still_prove_repeated_scanning(opts):
    from mojo.apps.incident.services import mojosec_rule_facts as facts

    with _fixture("redirects") as (key, sensor):
        paths = ("/.env", "/.git/config", "/vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php")
        for index, path in enumerate(paths):
            unused_receipt, event = _receipt(key, sensor, "web.probe", _web(
                path, source_ip="192.0.2.194", status=301, response_class="redirect"))
            th.assert_eq(facts.field_value(event, "mojosec_scanner_confirmed"), index == 2,
                         "redirects must still require three distinct receipts and sensitive paths")
        for status in (200, 500):
            unused_receipt, event = _receipt(key, sensor, "web.probe", _web(
                "/.env", source_ip="192.0.2.194", status=status))
            th.assert_eq(facts.field_value(event, "mojosec_scanner_confirmed"), False,
                         f"successful responses and server failures must remain visible: status {status}")


@th.django_unit_test()
def test_rule_runtime_never_falls_back_to_forged_sensor_metadata(opts):
    from mojo.apps.incident.models import Event, Rule, RuleSet
    from mojo.apps.incident.services import rule_validation

    with _fixture("rule-runtime") as (key, sensor):
        parent = RuleSet.objects.create(
            name=sensor, category="mojosec.web.probe", is_active=False,
            metadata=rule_validation.mark_governed())
        forged = Event.objects.create(
            title=sensor, scope="mojosec", category="mojosec.web.probe",
            source_ip="192.0.2.195", metadata={
                "mojosec_routine": True, "mojosec_expected_change": True,
                "mojosec_scanner_confirmed": True,
                "mojosec": {"routine": True, "expected_change": True,
                            "scanner_confirmed": True, "evidence": {"proof_status": "proven"}},
            })
        for name in ("mojosec_routine", "mojosec_expected_change", "mojosec_scanner_confirmed"):
            for field in (name, "metadata." + name):
                rule = Rule(parent=parent, field_name=field, comparator="==",
                            value_type="bool", value="true")
                th.assert_eq(rule.check_rule(forged), False,
                             f"runtime policy must reject forged flat or nested {field} without a receipt")
        unused_receipt, real = _receipt(key, sensor, "auth.ssh_login", {
            "source_ip": "192.0.2.195", "user": "deploy", "auth_method": "publickey"})
        real.metadata["mojosec_routine"] = False
        rule = Rule(parent=parent, field_name="mojosec_routine", comparator="==",
                    value_type="bool", value="true")
        th.assert_eq(rule.check_rule(real), True,
                     "runtime policy must use validated wire proof even when metadata contradicts it")


@th.django_unit_test()
def test_failed_receipt_lookup_is_cached_for_the_event(opts):
    from django.db import Error
    from mojo.apps.incident.models import Event
    from mojo.apps.incident.services import mojosec_rule_facts as facts

    event = Event(pk=123, scope="mojosec", category="mojosec.web.probe")
    with mock.patch.object(facts, "_load", side_effect=Error("fixture database unavailable")) as lookup:
        for name in facts.FIELD_SCHEMAS:
            th.assert_eq(facts.field_value(event, name), None,
                         f"unavailable durable evidence must fail closed for {name}")
        th.assert_eq(lookup.call_count, 1,
                     "one unavailable receipt lookup must not be retried for every condition on the Event")


@th.django_unit_test()
def test_ingestion_uses_the_same_strict_local_command_proof_as_policy(opts):
    from mojo.apps.incident.models import Incident, MojoSecReceipt
    from mojo.apps.incident.services import mojosec, mojosec_rule_facts as facts
    from mojo.mojosec import protocol
    from mojo.mojosec.disposition import APP_USER_FIREWALL_BROKER_CLASSIFIER

    with _fixture("ingest-local") as (key, sensor):
        attributes = {
            "actor": "ec2-user", "target_user": "root",
            "command": "/usr/local/sbin/mojo-firewall-broker",
            "command_path": "/usr/local/sbin/mojo-firewall-broker",
            "attribution_provenance": "none",
            "local_disposition": APP_USER_FIREWALL_BROKER_CLASSIFIER,
            "audit_loginuid": 1000, "boot_id": "a" * 32, "audit_session": 9,
        }
        observed = protocol.utc_now()
        template = {
            "kind": "auth.sudo_command", "observed_at": observed,
            "first_seen": observed, "last_seen": observed,
            "severity": "high", "summary": "Privileged sudo command executed",
            "count": 1, "recommendation": "review",
        }
        events = []
        for changes in ({}, {"tty": "pts/1"}, {"command_truncated": True}):
            wire = dict(template, id=uuid.uuid4().hex + uuid.uuid4().hex,
                        attributes=dict(attributes, **changes))
            events.append(wire)
        result = mojosec.ingest_batch(
            key, protocol.make_batch(sensor, events, policy_revision="facts-test"),
            volume_observer=lambda *unused_args: None)
        th.assert_eq([row["status"] for row in result["results"]], ["accepted"] * 3,
                     "both exact routine proof and visible near matches must be durably accepted")
        for index, wire in enumerate(events):
            receipt = MojoSecReceipt.objects.get(api_key=key, wire_event_id=wire["id"])
            if index == 0:
                th.assert_true(receipt.event_id is None and receipt.incident_id is None,
                               "an exact expected local broker sudo may remain an eventless receipt")
                th.assert_eq(receipt.replay_features["feature_schema"], "local_only_receipt_v1",
                             "only the exact expected broker sudo may use the local-only replay schema")
            else:
                th.assert_true(receipt.event_id is not None,
                               "a broker sudo near match must retain the central Event")
                th.assert_true(receipt.incident_id is not None and Incident.objects.filter(
                    pk=receipt.incident_id).exists(),
                    "a broker sudo near match must remain visible as a review Incident")
                th.assert_eq(facts.field_value(receipt.event, "mojosec_routine"), False,
                             "ingestion and policy must agree that broker near matches are not routine")


@th.django_unit_test()
def test_scanner_requires_fresh_wire_and_triggering_receipt(opts):
    from mojo.apps.incident.models import MojoSecReceipt
    from mojo.apps.incident.services import mojosec_rule_facts as facts
    from mojo.helpers import dates

    with _fixture("scanner-freshness") as (key, sensor):
        source_ip = "192.0.2.196"
        _receipt(key, sensor, "web.probe", _web("/.env", source_ip=source_ip))
        _receipt(key, sensor, "web.probe", _web("/.git/config", source_ip=source_ip))
        now = dates.utcnow()
        for path, observed in (
                ("/.git/HEAD", now + datetime.timedelta(seconds=600)),
                ("/.git/index", now - datetime.timedelta(seconds=660))):
            unused_receipt, event = _receipt(
                key, sensor, "web.probe", _web(path, source_ip=source_ip),
                observed=observed.isoformat())
            th.assert_eq(facts.field_value(event, "mojosec_scanner_confirmed"), False,
                         "stale or out-of-bounds future wire time must not trigger automatic blocking")
            unused_receipt, repeat = _receipt(
                key, sensor, "web.probe", _web("/.env", source_ip=source_ip))
            th.assert_eq(facts.field_value(repeat, "mojosec_scanner_confirmed"), False,
                         "stale or future counter candidates must not complete a fresh event's threshold")
        unused_receipt, current = _receipt(key, sensor, "web.probe", _web(
            "/vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php", source_ip=source_ip))
        th.assert_eq(facts.field_value(current, "mojosec_scanner_confirmed"), True,
                     "fresh timestamps must permit the positive scanner threshold")
        old, old_event = _receipt(
            key, sensor, "web.probe", _web("/.git/HEAD", source_ip=source_ip))
        MojoSecReceipt.objects.filter(pk=old.pk).update(
            created=dates.utcnow() - datetime.timedelta(seconds=660))
        th.assert_eq(facts.field_value(old_event, "mojosec_scanner_confirmed"), False,
                     "an aged triggering receipt must not borrow a current source's valid scanner context")
