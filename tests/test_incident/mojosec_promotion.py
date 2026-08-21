"""Auth/host correlation, SSH progression, corroboration, routing (#2105)."""

import datetime
import hashlib
import json
import uuid
from unittest import mock

from testit import helpers as th


PREFIX = f"mojosec_promo_test_{uuid.uuid4().hex[:10]}"
SENSOR_A = f"{PREFIX}-node-a"
SENSOR_B = f"{PREFIX}-node-b"
ATTACKER_IP = "203.0.113.44"
ADMIN_IP = "203.0.113.90"


def _event(event_id, kind, attributes, count=1, observed="2026-08-18T05:10:00Z",
           severity="warning"):
    return {
        "id": event_id,
        "kind": kind,
        "observed_at": observed,
        "first_seen": observed,
        "last_seen": observed,
        "severity": severity,
        "summary": "bounded test evidence",
        "count": count,
        "attributes": attributes,
        "recommendation": "review",
    }


def _ssh_failure(event_id, ip=ATTACKER_IP, user="root", count=1,
                 observed="2026-08-18T05:10:00Z"):
    return _event(event_id, "auth.ssh_failure", {
        "source_ip": ip, "user": user, "auth_method": "password",
    }, count=count, observed=observed)


def _ssh_login(event_id, ip=ATTACKER_IP, user="root",
               observed="2026-08-18T05:20:00Z"):
    return _event(event_id, "auth.ssh_login", {
        "source_ip": ip, "user": user, "auth_method": "publickey",
    }, observed=observed, severity="high")


def _service_error(event_id, unit="api.service", failure_kind="exit-code",
                   count=1, observed="2026-08-18T05:10:00Z"):
    return _event(event_id, "system.service_error", {
        "unit": unit, "priority": 3, "failure_kind": failure_kind,
        "message": "unit failed",
    }, count=count, observed=observed, severity="high")


def _batch(events, sensor_id=SENSOR_A):
    return {
        "schema": "mojosec.batch",
        "version": 1,
        "sensor_id": sensor_id,
        "sent_at": "2026-08-18T05:30:00Z",
        "policy_revision": "promo-policy-1",
        "events": events,
    }


def _settings(opts, mode="authoritative", include_host=True,
              require_registered=False):
    from mojo.apps.incident.services import mojosec_correlation

    targets = [
        {
            "installation_key_id": opts.promo_key_a.pk,
            "vhost_ids": [],
            "include_fim": True,
            "include_host": include_host,
            "require_registered_deployments": require_registered,
            "mode": mode,
        },
        {
            "installation_key_id": opts.promo_key_b.pk,
            "vhost_ids": [],
            "include_fim": True,
            "include_host": include_host,
            "require_registered_deployments": require_registered,
            "mode": mode,
        },
    ]
    original = mojosec_correlation.settings.get_static

    def get_static(name, default=None, **kwargs):
        if name == "MOJOSEC_CASE_SHADOW_TARGETS":
            return targets
        return original(name, default, **kwargs)
    return get_static


def _receipt(opts, event, key=None, sensor_id=SENSOR_A):
    from mojo.apps.incident.models import MojoSecReceipt
    from mojo.helpers import dates

    digest = hashlib.sha256(json.dumps(
        event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return MojoSecReceipt.objects.create(
        api_key=key or opts.promo_key_a, sensor_id=sensor_id,
        wire_event_id=event["id"], payload_digest=digest,
        publish_state="published", published_at=dates.utcnow(),
        replay_features={"feature_schema": "replay_features_v1", "event": event})


def _cases(sensor_id=None, **filters):
    from mojo.apps.incident.models import MojoSecCase

    queryset = MojoSecCase.objects.filter(
        sensor_id__startswith="mojosec_promo_test_")
    if sensor_id:
        queryset = queryset.filter(sensor_id=sensor_id)
    return queryset.filter(**filters)


def _promoted_events():
    from mojo.apps.incident.models import Event

    return Event.objects.filter(category="mojosec.case.promoted")


def _per_receipt_events(sensor_id=SENSOR_A):
    from mojo.apps.incident.models import Event

    return Event.objects.filter(metadata__mojosec__sensor_id=sensor_id)


@th.django_unit_setup()
def setup_mojosec_promotion(opts):
    from mojo.apps.account.models import ApiKey, Group
    from mojo.apps.incident.models import (
        Event, Incident, MojoSecCase, MojoSecCaseTransition, MojoSecDeployment,
        MojoSecReceipt)

    stem = "mojosec_promo_test_"
    MojoSecCaseTransition.maintenance_objects.filter(
        case__sensor_id__startswith=stem).delete()
    MojoSecReceipt.objects.filter(sensor_id__startswith=stem).delete()
    for case in MojoSecCase.objects.filter(sensor_id__startswith=stem):
        case.members.update(campaign=None)
    MojoSecCase.objects.filter(sensor_id__startswith=stem).delete()
    MojoSecDeployment.objects.filter(
        deployment_id__startswith="promo-deploy-").delete()
    for sensor in (SENSOR_A, SENSOR_B):
        Event.objects.filter(metadata__mojosec__sensor_id=sensor).delete()
    Incident.objects.filter(category="mojosec.case.promoted").delete()
    Event.objects.filter(category="mojosec.case.promoted").delete()
    ApiKey.objects.filter(name__startswith=stem).delete()
    Group.objects.filter(name__startswith=stem).delete()

    group = Group.objects.create(name=PREFIX, kind="organization")
    for suffix, sensor in (("a", SENSOR_A), ("b", SENSOR_B)):
        key, _token = ApiKey.create_for_group(
            group, f"{stem}key_{suffix}_{uuid.uuid4().hex[:6]}",
            permissions={"mojosec_ingest": True})
        key.metadata = {
            "protected": {
                "mojosec": {
                    "enabled": True,
                    "sensor_id": sensor,
                    "allowed_versions": [1],
                },
            },
        }
        key.save(update_fields=["metadata"])
        setattr(opts, f"promo_key_{suffix}", key)
    opts.promo_group = group


@th.django_unit_test()
def test_ssh_progression_promotes_once_and_suppresses_receipt_events(opts):
    from mojo.apps.incident.services import mojosec, mojosec_correlation

    failures = [
        _ssh_failure(f"aa{index:062x}", count=19, observed="2026-08-18T05:10:00Z")
        for index in range(18)
    ]
    failures.append(_ssh_failure(f"aa{18:062x}", count=13))
    login = _ssh_login(f"ab{0:062x}")
    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        acks = mojosec.ingest_batch(opts.promo_key_a, _batch(failures))
        th.assert_eq({row["status"] for row in acks["results"]}, {"accepted"},
                     "routed ssh failures must ack accepted")
        th.assert_eq(_per_receipt_events().count(), 0,
                     "routed ssh failures must project no per-receipt Events")
        login_ack = mojosec.ingest_batch(opts.promo_key_a, _batch([login]))
        th.assert_eq(login_ack["results"][0]["status"], "accepted",
                     "the promoting login must ack accepted")
        case = _cases(sensor_id=SENSOR_A, family="ssh").get()
        th.assert_eq(case.occurrence_count, 19 * 18 + 13 + 1,
                     "volume must be preserved across aggregates and login")
        th.assert_eq((case.urgency, case.urgency_reason),
                     ("critical", "ssh_failure_then_success"),
                     "failure burst followed by success must be critical")
        th.assert_eq(case.state, "elevated",
                     "the promoted ssh case must be elevated")
        th.assert_eq(case.observed_sources, [ATTACKER_IP],
                     "the case must retain the exact offending source")
        promoted = _promoted_events().filter(
            metadata__mojosec_case__case_id=case.pk)
        th.assert_eq(promoted.count(), 1,
                     "exactly one case-level Event per urgency step")
        # Redelivery: every aggregate and the login again — nothing moves.
        redelivered = mojosec.ingest_batch(
            opts.promo_key_a, _batch(failures + [login]))
        th.assert_eq({row["status"] for row in redelivered["results"]},
                     {"duplicate"}, "redelivery must ack duplicate")
        case.refresh_from_db()
        th.assert_eq(case.occurrence_count, 19 * 18 + 13 + 1,
                     "redelivery must not double-count occurrences")
        th.assert_eq(promoted.count(), 1,
                     "redelivery must not re-project the promotion")


@th.django_unit_test()
def test_ssh_promotion_spans_one_closed_window_only(opts):
    from mojo.apps.incident.services import mojosec, mojosec_correlation

    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        # Failures land in the 05:00 window for a dedicated account.
        acks = mojosec.ingest_batch(opts.promo_key_a, _batch([
            _ssh_failure(f"ba{0:062x}", user="deploy", count=9,
                         observed="2026-08-18T05:55:00Z")]))
        th.assert_eq(acks["results"][0]["status"], "accepted",
                     "failure aggregate must ack accepted")
        # Success in the NEXT hourly window still promotes via the twin.
        mojosec.ingest_batch(opts.promo_key_a, _batch([
            _ssh_login(f"bb{0:062x}", user="deploy",
                       observed="2026-08-18T06:05:00Z")]))
        promoted = _cases(sensor_id=SENSOR_A, family="ssh",
                          resource_id="user:deploy", urgency="critical")
        th.assert_eq(promoted.count(), 1,
                     "success one window after the burst must promote")
        # Two windows later (a fresh account) must NOT promote.
        mojosec.ingest_batch(opts.promo_key_a, _batch([
            _ssh_failure(f"bc{0:062x}", user="ops", count=9,
                         observed="2026-08-18T05:55:00Z")]))
        mojosec.ingest_batch(opts.promo_key_a, _batch([
            _ssh_login(f"bd{0:062x}", user="ops",
                       observed="2026-08-18T07:05:00Z")]))
        stale = _cases(sensor_id=SENSOR_A, family="ssh", resource_id="user:ops",
                       urgency="critical")
        th.assert_eq(stale.count(), 0,
                     "a success two windows later must not promote")


@th.django_unit_test()
def test_same_subnet_admin_never_shares_the_attacker_case(opts):
    from mojo.apps.incident.services import mojosec, mojosec_correlation

    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        mojosec.ingest_batch(opts.promo_key_a, _batch([
            _ssh_failure(f"ca{0:062x}", ip=ATTACKER_IP, user="admin",
                         count=40)]))
        mojosec.ingest_batch(opts.promo_key_a, _batch([
            _ssh_login(f"cb{0:062x}", ip=ADMIN_IP, user="admin")]))
        attacker = _cases(sensor_id=SENSOR_A, family="ssh",
                          resource_id="user:admin",
                          observed_sources__contains=[ATTACKER_IP]).get()
        admin = _cases(sensor_id=SENSOR_A, family="ssh",
                       resource_id="user:admin",
                       observed_sources__contains=[ADMIN_IP]).get()
        th.assert_true(attacker.pk != admin.pk,
                       "same-/24 admin and attacker must never share a case")
        th.assert_eq(attacker.urgency, "info",
                     "failures alone below burst threshold stay info")
        th.assert_eq((admin.urgency, admin.urgency_reason),
                     ("high", "ssh_login_during_failure_burst"),
                     "account-level progression pages high, never critical")
        critical = _cases(sensor_id=SENSOR_A, family="ssh",
                          resource_id="user:admin", urgency="critical")
        th.assert_eq(critical.count(), 0,
                     "no critical promotion without exact-IP progression")


@th.django_unit_test()
def test_ssh_low_volume_and_cross_node_isolation(opts):
    from mojo.apps.incident.services import mojosec, mojosec_correlation

    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        mojosec.ingest_batch(opts.promo_key_a, _batch([
            _ssh_failure(f"da{0:062x}", user="lowvol", count=3)]))
        mojosec.ingest_batch(opts.promo_key_a, _batch([
            _ssh_login(f"db{0:062x}", user="lowvol")]))
        case = _cases(sensor_id=SENSOR_A, resource_id="user:lowvol").get()
        th.assert_eq(case.urgency, "info",
                     "below-threshold failures plus success stays info")
        # The same failures on node B stay a separate case.
        mojosec.ingest_batch(opts.promo_key_b, _batch([
            _ssh_failure(f"dc{0:062x}", user="lowvol", count=3)],
            sensor_id=SENSOR_B))
        th.assert_eq(_cases(resource_id="user:lowvol").count(), 2,
                     "auth cases must never merge across sensors")


@th.django_unit_test()
def test_service_windows_burst_and_oom_stays_immediate(opts):
    from mojo.apps.incident.services import mojosec, mojosec_correlation

    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        # 09:xx keeps this hour clear of the ssh tests' 05:00-07:00 cases —
        # otherwise the burst's corroboration sweep would find them and
        # promote past the assertion below.
        acks = mojosec.ingest_batch(opts.promo_key_a, _batch([
            _service_error(f"ea{index:062x}", count=4,
                           observed="2026-08-18T09:10:00Z")
            for index in range(3)
        ]))
        th.assert_eq({row["status"] for row in acks["results"]}, {"accepted"},
                     "routed service failures must ack accepted")
        case = _cases(sensor_id=SENSOR_A, family="service").get()
        th.assert_eq(case.occurrence_count, 12,
                     "service repeat counts must be preserved")
        th.assert_eq((case.urgency, case.urgency_reason),
                     ("high", "service_failure_burst"),
                     "recurring unit failures must elevate to a burst")
        th.assert_eq(_per_receipt_events().filter(
            category="mojosec.system.service_error").count(), 0,
            "routed service errors project no per-receipt Events")
        # A different failure window is a different case.
        mojosec.ingest_batch(opts.promo_key_a, _batch([
            _service_error(f"eb{0:062x}", observed="2026-08-18T10:10:00Z")]))
        th.assert_eq(_cases(sensor_id=SENSOR_A, family="service").count(), 2,
                     "each hourly impact window is its own case")
        # OOM keeps its immediate critical Event AND contributes.
        oom = _event(f"ec{0:062x}", "system.oom",
                     {"unit": "kernel", "message": "Out of memory: Killed"},
                     observed="2026-08-18T09:15:00Z", severity="critical")
        oom_ack = mojosec.ingest_batch(opts.promo_key_a, _batch([oom]))
        th.assert_eq(oom_ack["results"][0]["status"], "accepted",
                     "oom must ack accepted")
        th.assert_eq(_per_receipt_events().filter(
            category="mojosec.system.oom").count(), 1,
            "oom keeps its immediate per-receipt Event")
        th.assert_eq(_cases(sensor_id=SENSOR_A, family="oom").count(), 1,
                     "oom must also contribute a host case")


@th.django_unit_test()
def test_sudo_case_samples_never_carry_command_text(opts):
    from mojo.apps.incident.services import mojosec, mojosec_correlation

    sudo = _event(f"fa{0:062x}", "auth.sudo_command", {
        "actor": "deployer", "target_user": "root",
        "command": "/usr/bin/systemctl restart api --secret=hunter2",
        "command_path": "/usr/bin/systemctl",
        "command_sha256": hashlib.sha256(b"cmd").hexdigest(),
    }, severity="high")
    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        ack = mojosec.ingest_batch(opts.promo_key_a, _batch([sudo]))
        th.assert_eq(ack["results"][0]["status"], "accepted",
                     "routed sudo must ack accepted")
        case = _cases(sensor_id=SENSOR_A, family="sudo").get()
        th.assert_eq(case.resource_id, "user:deployer",
                     "sudo cases key by actor")
        rendered = json.dumps(case.samples)
        th.assert_true("hunter2" not in rendered and "systemctl restart" not in rendered,
                       "case samples must never carry exact command text")
        th.assert_true("/usr/bin/systemctl" in rendered,
                       "bounded command_path belongs in the sample")
        th.assert_eq(_per_receipt_events().filter(
            category="mojosec.auth.sudo_command").count(), 0,
            "routed sudo projects no per-receipt Event")


@th.django_unit_test()
def test_shadow_include_host_keeps_events_and_sudo_failure_immediate(opts):
    from mojo.apps.incident.services import mojosec, mojosec_correlation

    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts, mode="shadow")), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        ack = mojosec.ingest_batch(opts.promo_key_a, _batch([
            _ssh_failure(f"ga{0:062x}", user="shadowed", count=7)]))
        th.assert_eq(ack["results"][0]["status"], "accepted",
                     "shadow ingest must ack accepted")
        th.assert_eq(_per_receipt_events().filter(
            category="mojosec.auth.ssh_failure").count(), 1,
            "shadow mode keeps the per-receipt Event")
        th.assert_eq(_cases(sensor_id=SENSOR_A, resource_id="user:shadowed").count(),
                     1, "shadow mode still contributes the case")
    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        # sudo_failure is never digest tier, even authoritative.
        ack = mojosec.ingest_batch(opts.promo_key_a, _batch([
            _event(f"gb{0:062x}", "auth.sudo_failure",
                   {"boot_id": "a" * 32}, severity="high")]))
        th.assert_eq(ack["results"][0]["status"], "accepted",
                     "sudo_failure must ack accepted")
        th.assert_eq(_per_receipt_events().filter(
            category="mojosec.auth.sudo_failure").count(), 1,
            "sudo_failure keeps its immediate per-receipt Event")


@th.django_unit_test()
def test_corroboration_promotes_the_high_trigger_once(opts):
    from mojo.apps.incident.models import MojoSecCase
    from mojo.apps.incident.services import mojosec, mojosec_correlation
    from mojo.helpers import dates

    now = dates.utcnow()

    def neighbor(sensor_kind, family, urgency, key_suffix):
        return MojoSecCase.objects.create(
            group=opts.promo_group, installation_key=opts.promo_key_a,
            sensor_id=SENSOR_A, sensor_kind=sensor_kind, family=family,
            resource_id="installation:0", network="",
            correlation_key=f"{PREFIX}-{key_suffix}",
            window_key=f"{PREFIX}-{key_suffix}-w",
            window_start=now, window_end=now, first_seen=now, last_seen=now,
            policy_version=1, evaluator_version=3,
            urgency=urgency, urgency_reason="test_neighbor",
            state="elevated" if urgency in ("high", "critical") else "observing")

    neighbor("auth", "ssh", "critical", "auth-neighbor")
    neighbor("web", "wordpress", "high", "web-neighbor")
    # Warning-level background never corroborates.
    neighbor("web", "php_runtime", "warning", "web-warning-neighbor")
    # Different sensor: must never corroborate node A's trigger.
    MojoSecCase.objects.create(
        group=opts.promo_group, installation_key=opts.promo_key_a,
        sensor_id=SENSOR_B, sensor_kind="host", family="service",
        resource_id="installation:0", network="",
        correlation_key=f"{PREFIX}-other-node",
        window_key=f"{PREFIX}-other-node-w",
        window_start=now, window_end=now, first_seen=now, last_seen=now,
        policy_version=1, evaluator_version=3,
        urgency="high", urgency_reason="test_neighbor", state="elevated")

    unannotated = _event(f"ha{0:062x}", "fim.change", {
        "path": "/etc/nginx/nginx.conf", "change": "modified",
    }, observed=now.isoformat().replace("+00:00", "Z"), severity="high")
    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts, mode="shadow")), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        receipt = _receipt(opts, unannotated)
        case, contributed = mojosec_correlation.contribute(receipt, unannotated)
        th.assert_true(contributed, "unannotated fim must contribute")
    case.refresh_from_db()
    th.assert_eq((case.urgency, case.urgency_reason),
                 ("critical", "corroborated_compromise"),
                 "multi-kind evidence on one node must promote the trigger")
    corroborated = case.breakdown.get("corroborated_with", [])
    th.assert_eq(len(corroborated), 2,
                 "only same-node high/critical neighbors corroborate")
    warning_row = _cases(sensor_id=SENSOR_A, family="php_runtime").get()
    th.assert_true(warning_row.pk not in corroborated,
                   "warning-level background must never corroborate")
    other_node = _cases(sensor_id=SENSOR_B, family="service").get()
    th.assert_true(other_node.pk not in corroborated,
                   "evidence on another sensor must not corroborate")


@th.django_unit_test()
def test_deployment_registration_gate_flips_trust(opts):
    from mojo.apps.incident.models import MojoSecDeployment
    from mojo.apps.incident.services import mojosec_correlation
    from mojo.helpers import dates

    observed = "2026-08-18T05:10:00Z"
    observed_at = datetime.datetime.fromisoformat(
        observed.replace("Z", "+00:00"))
    expires = (observed_at + datetime.timedelta(minutes=10)).isoformat().replace(
        "+00:00", "Z")

    def trusted(event_id):
        return _event(event_id, "fim.change", {
            "path": "/usr/lib/python3.11/site-packages/pkg/mod.py",
            "change": "modified",
            "expected_change": {
                "deployment_id": "promo-deploy-1",
                "operation_id": "op-1",
                "operation_kind": "system-python-packages",
                "completed_at": observed,
                "expires_at": expires,
            },
        }, observed=observed, severity="high")

    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts, mode="shadow", require_registered=True)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        receipt = _receipt(opts, trusted(f"ia{0:062x}"))
        case, contributed = mojosec_correlation.contribute(
            receipt, trusted(f"ia{0:062x}"))
        th.assert_true(contributed, "unregistered trusted fim must contribute")
        th.assert_true(case.family != "deployment",
                       "unregistered deployment ids are untrusted evidence")
        th.assert_eq(case.urgency, "high",
                     "unregistered deployment change is immediate evidence")
        MojoSecDeployment.objects.create(
            installation_key=opts.promo_key_a, deployment_id="promo-deploy-1",
            expires_at=observed_at + datetime.timedelta(hours=1))
        receipt2 = _receipt(opts, trusted(f"ib{0:062x}"))
        case2, contributed2 = mojosec_correlation.contribute(
            receipt2, trusted(f"ib{0:062x}"))
        th.assert_true(contributed2, "registered trusted fim must contribute")
        th.assert_eq(case2.family, "deployment",
                     "a registered deployment id restores the digest path")
