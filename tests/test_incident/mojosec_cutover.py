import copy
import hashlib
import json
import uuid
from unittest import mock

from testit import helpers as th
from tests.test_edge._helpers import (
    declare_pools, make_certificate, make_domain, make_vhost,
)


PREFIX = f"mojosec_cutover_test_{uuid.uuid4().hex[:10]}"
SENSOR_ID = f"{PREFIX}-node-a"
DOMAIN_NAME = f"{PREFIX.replace('_', '-')}.example.test"
DEPLOY_ID = "wmwx-release-2026-08-21.1"


def _fim_event(event_id, path, expected=True, observed="2026-08-18T02:12:00Z",
               operation_kind="system-python-packages", operation_id="op-pip-1",
               deployment_id=DEPLOY_ID, count=1):
    import datetime as _dt

    attributes = {"path": path, "change": "modified"}
    if expected:
        observed_at = _dt.datetime.fromisoformat(observed.replace("Z", "+00:00"))
        expires = (observed_at + _dt.timedelta(hours=1)).isoformat().replace(
            "+00:00", "Z")
        attributes["expected_change"] = {
            "deployment_id": deployment_id,
            "operation_id": operation_id,
            "operation_kind": operation_kind,
            "completed_at": observed,
            "expires_at": expires,
        }
    return {
        "id": event_id,
        "kind": "fim.change",
        "observed_at": observed,
        "first_seen": observed,
        "last_seen": observed,
        "severity": "high",
        "summary": "bounded test evidence",
        "count": count,
        "attributes": attributes,
        "recommendation": "review",
    }


def _web_event(event_id, vhost_id, count=1, observed="2026-08-18T02:12:00Z"):
    return {
        "id": event_id,
        "kind": "web.probe",
        "observed_at": observed,
        "first_seen": observed,
        "last_seen": observed,
        "severity": "high",
        "summary": "bounded test evidence",
        "count": count,
        "attributes": {
            "source_ip": "198.51.100.77",
            "method": "GET",
            "status": 404,
            "request_uri": "/wp-login.php",
            "response_class": "impossible_path",
            "resource_id": f"vhost:{vhost_id}",
            "edge_policy_version": 5,
            "scheme": "https",
        },
        "recommendation": "review",
    }


def _batch(events):
    return {
        "schema": "mojosec.batch",
        "version": 1,
        "sensor_id": SENSOR_ID,
        "policy_revision": "cutover-1",
        "events": events,
    }


def _settings(opts, mode="authoritative", targets=None, quiet=60):
    from mojo.apps.incident.services import mojosec_correlation

    if targets is None:
        targets = [{
            "installation_key_id": opts.cutover_api_key.pk,
            "vhost_ids": [opts.cutover_vhost_id],
            "include_fim": True,
            "mode": mode,
        }]
    # Captured before mock.patch replaces it. Unknown names MUST fall through
    # to the real helper: SettingsHelper.__getattr__ routes attribute reads
    # (USE_TZ included) through get_static, and starving those makes
    # dates.utcnow() silently naive.
    original = mojosec_correlation.settings.get_static

    def get_static(name, default=None, **kwargs):
        if name == "MOJOSEC_CASE_SHADOW_TARGETS":
            return targets
        if name == "MOJOSEC_CASE_FUTURE_SKEW_SECONDS":
            return 300
        if name == "MOJOSEC_DEPLOY_QUIET_SECONDS":
            return quiet
        return original(name, default, **kwargs)
    return get_static


def _sensor_events(opts):
    from mojo.apps.incident.models import Event

    return Event.objects.filter(metadata__mojosec__sensor_id=SENSOR_ID)


def _receipt(opts, event, sensor_id=SENSOR_ID):
    from mojo.apps.incident.models import MojoSecReceipt
    from mojo.helpers import dates

    digest = hashlib.sha256(json.dumps(
        event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return MojoSecReceipt.objects.create(
        api_key=opts.cutover_api_key, sensor_id=sensor_id,
        wire_event_id=event["id"], payload_digest=digest,
        publish_state="published", published_at=dates.utcnow(),
        replay_features={"feature_schema": "replay_features_v1", "event": event})


@th.django_unit_setup()
def setup_mojosec_cutover(opts):
    from mojo.apps.account.models import ApiKey, Group
    from mojo.apps.dnsman.models import Certificate, Domain
    from mojo.apps.edge.models import Vhost
    from mojo.apps.incident.models import (
        Event, Incident, MojoSecCase, MojoSecCaseTransition, MojoSecReceipt,
        RuleSet)

    # The stem (not the per-run uuid prefix) so rows from EVERY earlier run
    # are swept — the databases are long-lived and canonical wire ids
    # otherwise accumulate into other files' globally-scoped assertions.
    stem = "mojosec_cutover_test_"
    MojoSecCaseTransition.maintenance_objects.filter(
        case__sensor_id__startswith=stem).delete()
    MojoSecReceipt.objects.filter(sensor_id__startswith=stem).delete()
    MojoSecCase.objects.filter(sensor_id__startswith=stem).delete()
    Event.objects.filter(metadata__mojosec__sensor_id=SENSOR_ID).delete()
    # Only authoritative-mode tests (this file) create promoted case Events;
    # incidents cascade-delete their linked events, then orphans go directly.
    Incident.objects.filter(category="mojosec.case.promoted").delete()
    Event.objects.filter(category="mojosec.case.promoted").delete()
    RuleSet.objects.filter(name__startswith=PREFIX).delete()
    ApiKey.objects.filter(name__startswith=PREFIX).delete()
    Vhost.objects.filter(domain__name=DOMAIN_NAME).delete()
    Certificate.objects.filter(domain__name=DOMAIN_NAME).delete()
    Domain.objects.filter(name=DOMAIN_NAME).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()

    declare_pools()
    group = Group.objects.create(name=PREFIX, kind="organization")
    key, _token = ApiKey.create_for_group(
        group, f"{PREFIX}_key", permissions={"mojosec_ingest": True})
    key.metadata = {
        "protected": {
            "mojosec": {
                "enabled": True,
                "sensor_id": SENSOR_ID,
                "allowed_versions": [1],
            },
        },
    }
    key.save(update_fields=["metadata"])

    policy = {
        "version": 5,
        "impossible_path_families": [
            "admin_tools", "php_runtime", "secret_files", "wordpress"],
        "response_class": "spa_fallback",
    }
    domain = make_domain(name=DOMAIN_NAME, group=group)
    certificate = make_certificate(domain)
    vhost = make_vhost(
        domain, certificate, kind="site", spa=True, mojosec_policy=policy)

    opts.cutover_api_key = key
    opts.cutover_vhost_id = vhost.pk


@th.django_unit_test()
def test_deploy_flood_coalesces_to_one_quiet_case_and_zero_events(opts):
    """The WMWX fixture: expected deploy churn projects nothing and settles."""
    from mojo.apps.incident.models import (
        MojoSecCase, MojoSecCaseTransition, MojoSecReceipt)
    from mojo.apps.incident.services import mojosec, mojosec_correlation
    from mojo.helpers import dates

    events = [
        _fim_event(
            f"cf{index:062x}",
            f"/usr/lib/python3.11/site-packages/pkg_{index % 40}/mod_{index}.py")
        for index in range(2335)
    ]
    events += [
        _fim_event(
            f"aa{index:062x}", path, operation_kind="rendered-node-config",
            operation_id="op-node-1")
        for index, path in enumerate((
            "/etc/cron.d/3_mojo_jobs", "/etc/cron.d",
            "/etc/logrotate.d/mojosec", "/etc/logrotate.d",
            "/etc/nginx/conf.d/00_mojosec.conf", "/etc/nginx/conf.d",
        ))
    ]
    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        acks = mojosec.ingest_batch(opts.cutover_api_key, _batch(events))
        statuses = {row["status"] for row in acks["results"]}
        th.assert_eq(statuses, {"accepted"},
                     f"every trusted deploy change must ack accepted: {statuses}")
        redelivered = mojosec.ingest_batch(
            opts.cutover_api_key, _batch(events[:50]))
        th.assert_eq({row["status"] for row in redelivered["results"]},
                     {"duplicate"},
                     "redelivery of contributed receipts must ack duplicate")

        th.assert_eq(_sensor_events(opts).count(), 0,
                     "expected deployment churn must project zero per-receipt Events")
        cases = list(MojoSecCase.objects.filter(sensor_id=SENSOR_ID))
        th.assert_eq(len(cases), 1,
                     f"2,341 deploy changes must coalesce into ONE case, got {len(cases)}")
        case = cases[0]
        th.assert_eq(
            (case.family, case.deployment_id, case.urgency, case.state),
            ("deployment", DEPLOY_ID, "info", "observing"),
            "the deployment case must carry its identity at digest urgency")
        th.assert_eq((case.occurrence_count, case.receipt_count), (2341, 2341),
                     "occurrence and receipt totals must be exact, not chunked")
        th.assert_eq(case.projected_event_count, 0,
                     "a case-routed deployment case projects no Events")
        th.assert_eq(case.breakdown["operations"],
                     {"system-python-packages": 2335, "rendered-node-config": 6},
                     "the bounded operation breakdown must carry exact counts")
        th.assert_eq(case.breakdown["tiers"],
                     {"system_binary": 2335, "system_config": 6},
                     "the bounded tier breakdown must carry exact counts")
        routed = MojoSecReceipt.objects.filter(
            sensor_id=SENSOR_ID, case_routed=True,
            publish_state="published").count()
        th.assert_eq(routed, 2341,
                     "every routed receipt must reach the published prunable state")

        result = mojosec_correlation.settle_sweep(now=dates.utcnow())
        th.assert_true(result["settled"] >= 1,
                       f"the quiet deployment case must settle: {result}")
        case.refresh_from_db()
        th.assert_eq((case.state, case.state_reason),
                     ("settled", "deployment_quiet_window"),
                     "the quiet window must settle the deployment case")
        th.assert_true(case.settled_at is not None,
                       "settling must stamp settled_at")
        th.assert_eq(_sensor_events(opts).count(), 0,
                     "settling must not project an Event — deploys stay invisible")
        settled_transition = MojoSecCaseTransition.objects.filter(
            case=case, transition="settled").count()
        th.assert_eq(settled_transition, 1,
                     "settling must append exactly one system transition")

        late = _fim_event(
            "bb" + "0" * 62,
            "/usr/lib/python3.11/site-packages/pkg_late/late.py",
            observed="2026-08-18T02:20:00Z")
        late_ack = mojosec.ingest_batch(opts.cutover_api_key, _batch([late]))
        th.assert_eq(late_ack["results"][0]["status"], "accepted",
                     "a late trusted change must still be accepted")
        case.refresh_from_db()
        th.assert_eq((case.state, case.state_reason),
                     ("observing", "deployment_reopened"),
                     "a genuinely new late receipt must reopen the settled case")
        th.assert_eq(case.occurrence_count, 2342,
                     "the reopened case must keep exact totals")
        reopened = MojoSecCaseTransition.objects.filter(
            case=case, transition="reopened").count()
        th.assert_eq(reopened, 1, "reopening must append one transition")


@th.django_unit_test()
def test_deployment_cases_split_per_sensor_and_utc_day(opts):
    from mojo.apps.incident.models import MojoSecCase
    from mojo.apps.incident.services import mojosec_correlation

    events = (
        (_fim_event("c1" + "0" * 62, "/etc/one.conf",
                    observed="2026-08-18T23:59:00Z"), f"{PREFIX}-node-a2"),
        (_fim_event("c2" + "0" * 62, "/etc/two.conf",
                    observed="2026-08-18T23:59:30Z"), f"{PREFIX}-node-b2"),
        (_fim_event("c3" + "0" * 62, "/etc/three.conf",
                    observed="2026-08-19T00:01:00Z"), f"{PREFIX}-node-a2"),
    )
    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        for event, sensor in events:
            receipt = _receipt(opts, event, sensor_id=sensor)
            case, contributed = mojosec_correlation.contribute(receipt, event)
            th.assert_true(contributed, f"receipt {event['id'][:4]} must contribute")
    cases = MojoSecCase.objects.filter(
        sensor_id__startswith=f"{PREFIX}-node-", family="deployment",
        deployment_id=DEPLOY_ID).exclude(sensor_id=SENSOR_ID)
    th.assert_eq(cases.count(), 3,
                 "two sensors plus a UTC-day rollover must produce three cases: "
                 "per-sensor separation and the day bucket both key the case")


@th.django_unit_test()
def test_unannotated_changes_stay_immediate_and_promote_the_case(opts):
    from mojo.apps.incident.models import Event, MojoSecCase, MojoSecReceipt
    from mojo.apps.incident.services import mojosec, mojosec_correlation

    events = [
        _fim_event("d1" + "0" * 62,
                   "/usr/lib/python3.11/site-packages/pkg/good.py"),
        _fim_event("d2" + "0" * 62, "/etc/nginx/nginx.conf", expected=False),
    ]
    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        acks = mojosec.ingest_batch(opts.cutover_api_key, _batch(events))
    th.assert_eq({row["status"] for row in acks["results"]}, {"accepted"},
                 f"both changes must be accepted: {acks}")
    immediate = _sensor_events(opts).filter(category="mojosec.fim.change")
    th.assert_eq(immediate.count(), 1,
                 "exactly the unannotated change projects an immediate Event")
    th.assert_eq(immediate.first().level, 8,
                 "the unannotated protected change keeps its high severity")
    unannotated = MojoSecReceipt.objects.get(wire_event_id="d2" + "0" * 62)
    th.assert_true(not unannotated.case_routed,
                   "an unannotated change must keep the legacy per-receipt path")
    untrusted_case = MojoSecCase.objects.get(
        sensor_id=SENSOR_ID, family="system_config")
    th.assert_eq((untrusted_case.urgency, untrusted_case.state),
                 ("high", "elevated"),
                 "the unannotated change must elevate its untrusted case")
    promoted = Event.objects.filter(
        category="mojosec.case.promoted",
        metadata__mojosec_case__case_id=untrusted_case.pk)
    th.assert_eq(promoted.count(), 1,
                 "the promotion must project exactly one deliberate case Event")
    body = json.dumps(promoted.first().metadata)
    th.assert_true("nginx.conf" not in body,
                   "raw paths must never enter the projected case Event")
    untrusted_case.refresh_from_db()
    th.assert_eq(untrusted_case.projected_urgency, "high",
                 "the projection ratchet must record the projected urgency")


@th.django_unit_test()
def test_web_probes_route_and_promotion_projects_once(opts):
    from mojo.apps.incident.models import (
        Event, MojoSecCase, MojoSecCaseTransition, MojoSecReceipt, RuleSet)
    from mojo.apps.incident.services import mojosec, mojosec_correlation

    rule_set = RuleSet.objects.create(
        name=f"{PREFIX} promoted", category="mojosec.case.promoted",
        priority=10, handler="notify://security@example.test")
    first = _web_event("e1" + "0" * 62, opts.cutover_vhost_id, count=600)
    second = _web_event("e2" + "0" * 62, opts.cutover_vhost_id, count=500)
    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        first_ack = mojosec.ingest_batch(opts.cutover_api_key, _batch([first]))
        th.assert_eq(first_ack["results"][0]["status"], "accepted",
                     f"the first probe burst must be accepted: {first_ack}")
        th.assert_eq(
            _sensor_events(opts).filter(category="mojosec.web.probe").count(),
            0, "routed web probes must project no per-receipt Events")
        second_ack = mojosec.ingest_batch(opts.cutover_api_key, _batch([second]))
        th.assert_eq(second_ack["results"][0]["status"], "accepted",
                     f"the promoting burst must be accepted: {second_ack}")
        redelivered = mojosec.ingest_batch(
            opts.cutover_api_key, _batch([second]))
        th.assert_eq(redelivered["results"][0]["status"], "duplicate",
                     "redelivery after promotion must ack duplicate")

    case = MojoSecCase.objects.get(sensor_id=SENSOR_ID, sensor_kind="web")
    th.assert_eq((case.urgency, case.state, case.occurrence_count),
                 ("high", "elevated", 1100),
                 "sustained trusted impossible paths must promote the case")
    promoted = Event.objects.filter(
        category="mojosec.case.promoted",
        metadata__mojosec_case__case_id=case.pk)
    th.assert_eq(promoted.count(), 1,
                 "one urgency step must project exactly one Event, ratcheted")
    projection = MojoSecCaseTransition.objects.filter(
        case=case, transition="projection")
    th.assert_eq(projection.count(), 1,
                 "the projection must append one receipt-less system transition")
    th.assert_eq(projection.first().projected_event_id, promoted.first().pk,
                 "the projection transition must link the projected Event")
    case.refresh_from_db()
    th.assert_true(case.projection_dispatched_at is not None,
                   "the promoted RuleSet dispatch must be durably recorded")
    th.assert_eq(MojoSecReceipt.objects.filter(
        wire_event_id__in=(first["id"], second["id"]),
        case_routed=True, publish_state="published").count(), 2,
        "routed web receipts must reach the published state")
    rule_set.delete()


@th.django_unit_test()
def test_ack_retry_then_sticky_terminal_conversion(opts):
    from mojo.apps.incident.models import MojoSecReceipt
    from mojo.apps.incident.services import mojosec, mojosec_correlation

    event = _fim_event("f1" + "0" * 62,
                       "/usr/lib/python3.11/site-packages/pkg/one.py")
    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"), \
            mock.patch.object(mojosec_correlation, "contribute",
                              side_effect=RuntimeError("db unavailable")):
        ack = mojosec.ingest_batch(opts.cutover_api_key, _batch([event]))
    th.assert_eq(ack["results"][0]["status"], "retry",
                 f"a failed contribution must ack retry: {ack}")
    receipt = MojoSecReceipt.objects.get(wire_event_id=event["id"])
    th.assert_eq((receipt.case_routed, receipt.publish_state),
                 (True, "pending"),
                 "the routed receipt must stay pending for redelivery")

    # The binding disappears entirely (for example a malformed target list);
    # stickiness must convert to a visible Event, never dead-letter/reject.
    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts, targets=[])), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        converted = mojosec.ingest_batch(opts.cutover_api_key, _batch([event]))
    th.assert_eq(converted["results"][0]["status"], "accepted",
                 f"the terminal conversion must accept the evidence: {converted}")
    receipt.refresh_from_db()
    th.assert_eq(
        (receipt.case_routed, receipt.publish_state, receipt.event is None),
        (False, "published", False),
        "conversion must surface the ordinary per-receipt Event")
    th.assert_eq(receipt.event.category, "mojosec.fim.change",
                 "the converted Event must be the normal projection")


@th.django_unit_test()
def test_stranded_receipts_survive_sweeps_and_retry(opts):
    from mojo.apps.incident.models import MojoSecCase, MojoSecReceipt
    from mojo.apps.incident.services import mojosec, mojosec_correlation
    from mojo.helpers import dates

    event = _fim_event("f2" + "0" * 62,
                       "/usr/lib/python3.11/site-packages/pkg/two.py")
    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"), \
            mock.patch.object(mojosec_correlation, "contribute",
                              side_effect=RuntimeError("db unavailable")):
        mojosec.ingest_batch(opts.cutover_api_key, _batch([event]))
    receipt = MojoSecReceipt.objects.get(wire_event_id=event["id"])
    MojoSecReceipt.objects.filter(pk=receipt.pk).update(
        created=dates.utcnow() - dates.timedelta(days=2),
        modified=dates.utcnow() - dates.timedelta(days=2))

    # The 1-day dead-letter sweep must skip case-routed receipts outright.
    mojosec.replay_handler_outbox()
    receipt.refresh_from_db()
    th.assert_eq((receipt.case_routed, receipt.publish_state),
                 (True, "pending"),
                 "the dead-letter sweep must never touch a case-routed receipt")

    # And _publish_receipt must refuse rather than dead-letter + reject.
    published, did = mojosec._publish_receipt(receipt)
    th.assert_true(did is False and published.publish_state == "pending",
                   "_publish_receipt must refuse a case-routed receipt unharmed")

    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        result = mojosec_correlation.settle_sweep(now=dates.utcnow())
    th.assert_true(result["retried"] >= 1,
                   f"the sweep must re-drive the stranded receipt: {result}")
    receipt.refresh_from_db()
    th.assert_eq((receipt.case_routed, receipt.publish_state),
                 (True, "published"),
                 "the sweep retry must complete the stranded contribution")
    th.assert_true(MojoSecCase.objects.filter(
        sensor_id=SENSOR_ID, family="deployment",
        receipts__pk=receipt.pk).exists(),
        "the retried receipt must land in its deployment case")


@th.django_unit_test()
def test_shadow_mode_and_unenrolled_behavior_unchanged(opts):
    from mojo.apps.incident.models import Event, MojoSecCase, MojoSecReceipt
    from mojo.apps.incident.services import mojosec, mojosec_correlation

    trusted = _fim_event("f3" + "0" * 62,
                         "/usr/lib/python3.11/site-packages/pkg/three.py",
                         observed="2026-08-18T06:12:00Z")
    unannotated = _fim_event("f4" + "0" * 62, "/etc/hosts", expected=False,
                             observed="2026-08-18T06:12:00Z")
    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts, mode="shadow")), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        acks = mojosec.ingest_batch(
            opts.cutover_api_key, _batch([trusted, unannotated]))
    th.assert_eq({row["status"] for row in acks["results"]}, {"accepted"},
                 f"shadow-mode ingestion must be unchanged: {acks}")
    th.assert_eq(
        _sensor_events(opts).filter(
            category="mojosec.fim.change",
            metadata__mojosec__event_id__in=(
                trusted["id"], unannotated["id"])).count(), 2,
        "shadow mode must keep one per-receipt Event per change")
    th.assert_eq(MojoSecReceipt.objects.filter(
        wire_event_id__in=(trusted["id"], unannotated["id"]),
        case_routed=True).count(), 0,
        "shadow mode must never route receipts away from Events")
    th.assert_true(MojoSecCase.objects.filter(
        sensor_id=SENSOR_ID, family="deployment").exists(),
        "shadow mode still records the deployment case beside the Events")
    elevated = MojoSecCase.objects.get(
        sensor_id=SENSOR_ID, family="system_config",
        receipts__wire_event_id=unannotated["id"])
    th.assert_eq(elevated.projected_urgency, "",
                 "shadow mode must never project a case Event")
    th.assert_eq(Event.objects.filter(
        category="mojosec.case.promoted",
        metadata__mojosec_case__case_id=elevated.pk).count(), 0,
        "no promotion Event may exist for a shadow-mode case")


@th.django_unit_test()
def test_projection_crash_is_healed_by_the_sweep(opts):
    from mojo.apps.incident.models import Event, MojoSecCase, MojoSecCaseTransition
    from mojo.apps.incident.services import mojosec, mojosec_correlation
    from mojo.helpers import dates

    event = _fim_event("f5" + "0" * 62, "/etc/ssh/sshd_config", expected=False,
                       observed="2026-08-18T09:12:00Z")
    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"), \
            mock.patch.object(mojosec_correlation, "_project_case",
                              return_value=None):
        mojosec.ingest_batch(opts.cutover_api_key, _batch([event]))
    case = MojoSecCase.objects.get(
        sensor_id=SENSOR_ID, family="system_config",
        receipts__wire_event_id=event["id"])
    th.assert_eq((case.urgency, case.projected_urgency), ("high", ""),
                 "the crash simulation must leave the projection lagging")
    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        result = mojosec_correlation.settle_sweep(now=dates.utcnow())
    th.assert_true(result["projected"] >= 1,
                   f"the sweep must heal the missed projection: {result}")
    case.refresh_from_db()
    th.assert_eq(case.projected_urgency, "high",
                 "the healed projection must advance the ratchet")
    th.assert_eq(Event.objects.filter(
        category="mojosec.case.promoted",
        metadata__mojosec_case__case_id=case.pk).count(), 1,
        "the healed projection must create exactly one Event")
    th.assert_eq(MojoSecCaseTransition.objects.filter(
        case=case, transition="projection").count(), 1,
        "the healed projection must append one system transition")
