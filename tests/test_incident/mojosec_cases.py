import copy
import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from testit import helpers as th


PREFIX = f"mojosec_case_test_{uuid.uuid4().hex[:10]}"
PASSWORD = "MojoSecCases##1"


def _event(kind, event_id, count=1, attributes=None, observed="2026-08-18T01:12:00Z"):
    return {
        "id": event_id,
        "kind": kind,
        "observed_at": observed,
        "first_seen": observed,
        "last_seen": observed,
        "severity": "high",
        "summary": "bounded test evidence",
        "count": count,
        "attributes": attributes or {},
        "recommendation": "review",
    }


def _receipt(opts, event):
    from mojo.apps.incident.models import MojoSecReceipt
    from mojo.helpers import dates

    digest = hashlib.sha256(json.dumps(
        event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return MojoSecReceipt.objects.create(
        api_key=opts.case_api_key, sensor_id=PREFIX,
        wire_event_id=event["id"], payload_digest=digest,
        publish_state="published", published_at=dates.utcnow(),
        replay_features={"feature_schema": "replay_features_v1", "event": event})


def _targets(opts):
    return [{
        "installation_key_id": opts.case_api_key.pk,
        "vhost_ids": [opts.case_vhost_id],
        "include_fim": True,
    }]


@th.django_unit_setup()
def setup_mojosec_cases(opts):
    from mojo.apps.account.models import ApiKey, Group, User
    from mojo.apps.incident.models import (
        MojoSecCase, MojoSecCaseTransition, MojoSecReceipt)

    MojoSecCaseTransition.maintenance_objects.filter(
        case__sensor_id=PREFIX).delete()
    MojoSecReceipt.objects.filter(sensor_id=PREFIX).delete()
    MojoSecCase.objects.filter(sensor_id=PREFIX).delete()
    ApiKey.objects.filter(name__startswith=PREFIX).delete()
    Group.objects.filter(name=PREFIX).delete()
    User.objects.filter(username__startswith=PREFIX).delete()

    group = Group.objects.create(name=PREFIX, kind="organization")
    key, _ = ApiKey.create_for_group(
        group, f"{PREFIX}_key", permissions={"mojosec_ingest": True})
    viewer = User.objects.create_user(
        f"{PREFIX}@example.test", PASSWORD, username=f"{PREFIX}_viewer")
    viewer.is_active = True
    viewer.is_email_verified = True
    viewer.requires_mfa = False
    viewer.add_permission("view_security")
    viewer.save()
    nobody = User.objects.create_user(
        f"{PREFIX}_none@example.test", PASSWORD, username=f"{PREFIX}_none")
    nobody.is_active = True
    nobody.is_email_verified = True
    nobody.requires_mfa = False
    nobody.save()

    opts.case_api_key = key
    opts.case_vhost_id = 987654
    opts.case_viewer_email = viewer.email
    opts.case_nobody_email = nobody.email


@th.django_unit_test()
def test_shadow_case_is_idempotent_and_301_twins_preserve_volume(opts):
    from mojo.apps.incident.models import MojoSecCase, MojoSecCaseTransition
    from mojo.apps.incident.services import mojosec_correlation

    base = {
        "source_ip": "198.51.100.19",
        "method": "GET",
        "status": 404,
        "request_uri": "/wp-login.php?token=receipt-only-secret",
        "response_class": "impossible_path",
        "resource_id": f"vhost:{opts.case_vhost_id}",
        "edge_policy_version": 3,
        "scheme": "https",
    }
    secure = _event("web.probe", "1" * 64, count=10000, attributes=base)
    redirect = copy.deepcopy(secure)
    redirect.update({"id": "2" * 64, "count": 1})
    redirect["attributes"].update({
        "scheme": "http", "status": 301, "response_class": "redirect"})
    first = _receipt(opts, secure)
    twin = _receipt(opts, redirect)

    with mock.patch.object(
            mojosec_correlation.settings, "get_static", return_value=_targets(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        case, contributed = mojosec_correlation.contribute(first, secure)
        duplicate, duplicate_contributed = mojosec_correlation.contribute(first, secure)
        twin_case, twin_contributed = mojosec_correlation.contribute(twin, redirect)

    case.refresh_from_db()
    th.assert_true(contributed and not duplicate_contributed and twin_contributed,
                   "one receipt must contribute once while its redirect twin remains evidence")
    th.assert_eq((duplicate.pk, twin_case.pk), (case.pk, case.pk),
                 "duplicate and HTTP redirect twin must resolve to the deterministic case")
    th.assert_eq(case.occurrence_count, 10001,
                 "occurrence volume must include the 10,000 probes and redirect twin")
    th.assert_eq(case.receipt_count, 2,
                 "receipt count must remain distinct from occurrence volume")
    th.assert_eq(case.projected_event_count, 1,
                 "the trusted HTTP-to-HTTPS 301 twin must not inflate projected Events")
    th.assert_eq(case.sample_count, 1,
                 "the same family/network/resource must occupy one bounded sample")
    th.assert_eq(MojoSecCase.objects.filter(sensor_id=PREFIX).count(), 1,
                 "10,000 occurrences must compress into one bounded web case")
    th.assert_eq(MojoSecCaseTransition.objects.filter(case=case).count(), 2,
                 "every contributing receipt must append exactly one transition")
    th.assert_true("receipt-only-secret" not in json.dumps(case.samples),
                   "query strings and receipt-only secrets must never enter case samples")


@th.django_unit_test()
def test_concurrent_delivery_and_fim_load_are_bounded(opts):
    from django.db import close_old_connections
    from mojo.apps.incident.models import MojoSecCase
    from mojo.apps.incident.services import mojosec_correlation

    web = _event("web.probe", "3" * 64, attributes={
        "source_ip": "2001:db8::12", "method": "GET", "status": 404,
        "request_uri": "/.env.production?password=never-case-data",
        "response_class": "impossible_path",
        "resource_id": f"vhost:{opts.case_vhost_id}",
        "edge_policy_version": 4, "scheme": "https",
    }, observed="2026-08-18T02:12:00Z")
    receipt = _receipt(opts, web)

    def contribute_once():
        close_old_connections()
        try:
            return mojosec_correlation.contribute(receipt, web)[1]
        finally:
            close_old_connections()

    with mock.patch.object(
            mojosec_correlation.settings, "get_static", return_value=_targets(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda unused: contribute_once(), range(2)))

        fim = _event("fim.change", "4" * 64, count=6636, attributes={
            "path": "/etc/ssh/sshd_config", "change": "modified",
            "expected_change": {
                "deployment_id": "deploy-2026-08-18",
                "operation_id": "release-42", "operation_kind": "release",
                "completed_at": "2026-08-18T02:12:00Z",
                "expires_at": "2026-08-18T03:00:00Z",
            },
        }, observed="2026-08-18T02:13:00Z")
        fim_receipt = _receipt(opts, fim)
        fim_case, fim_contributed = mojosec_correlation.contribute(fim_receipt, fim)

    th.assert_eq(sum(1 for outcome in outcomes if outcome), 1,
                 "two concurrent deliveries must produce one receipt contribution")
    web_case = MojoSecCase.objects.get(receipts=receipt)
    th.assert_eq((web_case.receipt_count, web_case.occurrence_count), (1, 1),
                 "concurrent delivery must increment neither receipt nor occurrence twice")
    fim_case.refresh_from_db()
    th.assert_true(fim_contributed, "the rollout-approved FIM receipt must contribute")
    th.assert_eq((fim_case.occurrence_count, fim_case.receipt_count), (6636, 1),
                 "6,636 expected FIM changes must preserve volume separately from receipts")
    th.assert_eq(fim_case.urgency, "info",
                 "trusted deployment grouping must not promote expected FIM changes")
    th.assert_true("sshd_config" not in json.dumps(fim_case.samples),
                   "raw FIM paths must not enter bounded case samples")


@th.django_unit_test()
def test_family_and_overflow_normalization_caps_varied_load(opts):
    from mojo.apps.incident.services import mojosec_correlation

    web_families = {
        mojosec_correlation._web_family(
            f"/wp-admin/plugin-{index}/../../token-{index}.php")
        for index in range(10000)
    }
    fim_tiers = {
        mojosec_correlation._fim_tier(f"/etc/mojosec-fixture/{index}")
        for index in range(6636)
    }
    th.assert_eq(web_families, {"wordpress"},
                 "10,000 varied WordPress probes must normalize to one family")
    th.assert_eq(fim_tiers, {"system_config"},
                 "6,636 protected FIM paths must normalize to one protected tier")
    th.assert_eq(mojosec_correlation._web_family("/unregistered/product/path"),
                 "other_probe", "unknown probe paths need an explicit overflow bucket")


@th.django_unit_test()
def test_samples_overflow_and_late_windows_are_explicit(opts):
    from mojo.apps.incident.services import mojosec_correlation

    cases = []
    with mock.patch.object(
            mojosec_correlation.settings, "get_static", return_value=_targets(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        for index in range(9):
            event = _event("web.probe", f"{index + 10:064x}", attributes={
                "source_ip": "198.51.100.88", "method": "GET", "status": 404,
                "request_uri": f"/wp-content/plugin-{index}/readme.txt?secret=drop-me",
                "response_class": "impossible_path",
                "resource_id": f"vhost:{opts.case_vhost_id}",
                "edge_policy_version": 5, "scheme": "https",
            }, observed="2026-08-18T04:05:00Z")
            receipt = _receipt(opts, event)
            cases.append(mojosec_correlation.contribute(receipt, event)[0])
        late = _event("web.probe", "f" * 64, attributes={
            "source_ip": "198.51.100.88", "method": "GET", "status": 404,
            "request_uri": "/wp-content/late/readme.txt",
            "response_class": "impossible_path",
            "resource_id": f"vhost:{opts.case_vhost_id}",
            "edge_policy_version": 5, "scheme": "https",
        }, observed="2026-08-18T03:59:59Z")
        late_case = mojosec_correlation.contribute(_receipt(opts, late), late)[0]

    case = cases[0]
    case.refresh_from_db()
    th.assert_true(all(item.pk == case.pk for item in cases),
                   "varied paths in one family/network/window must share one case")
    th.assert_eq((case.distinct_count, case.sample_count, case.overflow_count), (9, 8, 1),
                 "sample storage must cap at eight and count explicit overflow")
    th.assert_true("drop-me" not in json.dumps(case.samples),
                   "query strings must be absent from every bounded path shape")
    th.assert_true(late_case.pk != case.pk,
                   "late evidence must enter its deterministic observed-time window")
    th.assert_eq((late_case.window_start.hour, case.window_start.hour), (3, 4),
                 "hour rollover must remain explicit instead of mutating the active window")


@th.django_unit_test()
def test_unknown_response_identity_never_promotes_from_status_or_length(opts):
    from mojo.apps.incident.services import mojosec_correlation

    event = _event("web.probe", "e" * 64, count=2500, attributes={
        "source_ip": "198.51.100.121", "method": "GET", "status": 200,
        "response_bytes": 12345, "request_uri": "/wp-login.php",
        "resource_id": f"vhost:{opts.case_vhost_id}", "scheme": "https",
    }, observed="2026-08-18T05:05:00Z")
    with mock.patch.object(
            mojosec_correlation.settings, "get_static", return_value=_targets(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        case, contributed = mojosec_correlation.contribute(_receipt(opts, event), event)
    th.assert_true(contributed, "unknown evidence should remain visible as a shadow case")
    th.assert_eq((case.state, case.urgency, case.urgency_reason),
                 ("observing", "info", "unknown_edge_evidence"),
                 "status, length, and probe filename alone must never promote compromise")


@th.django_unit_test()
def test_case_serialization_has_constant_query_count(opts):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from mojo.apps.incident.models import MojoSecCase
    from mojo.apps.incident.rest.mojosec import _case_row

    with CaptureQueriesContext(connection) as captured:
        rows = list(MojoSecCase.objects.filter(sensor_id=PREFIX).order_by("-id")[:100])
        serialized = [_case_row(case) for case in rows]
    th.assert_true(len(captured.captured_queries) <= 1,
                   f"case list serialization must stay one query: {captured.captured_queries}")
    th.assert_eq(len(serialized), len(rows),
                 "query-count coverage must serialize every selected case")
    if rows:
        with CaptureQueriesContext(connection) as detail_queries:
            detail = _case_row(rows[0], detail=True)
        th.assert_true(len(detail_queries.captured_queries) <= 1,
                       f"case detail may add only its bounded transition query: "
                       f"{detail_queries.captured_queries}")
        th.assert_true(len(detail["transitions"]) <= 50,
                       "case detail transitions must remain bounded")


@th.django_unit_test()
def test_case_read_contract_requires_security_and_stays_bounded(opts):
    from mojo.decorators.limits import clear_rate_limits

    opts.client.logout()
    anonymous = opts.client.get("/api/incident/mojosec/case")
    th.assert_true(anonymous.status_code in (401, 403),
                   "anonymous callers must not reach shadow cases")

    clear_rate_limits(ip="127.0.0.1", key="login")
    th.assert_true(opts.client.login(opts.case_nobody_email, PASSWORD),
                   "the no-permission fixture must log in")
    denied = opts.client.get("/api/incident/mojosec/case")
    th.assert_true(denied.status_code in (401, 403),
                   "a user without global security permission must be denied")

    opts.client.logout()
    clear_rate_limits(ip="127.0.0.1", key="login")
    th.assert_true(opts.client.login(opts.case_viewer_email, PASSWORD),
                   "the security viewer fixture must log in")
    listed = opts.client.get("/api/incident/mojosec/case?page_size=1")
    th.assert_eq(listed.status_code, 200,
                 f"a global security viewer must read cases: {listed.response}")
    th.assert_true(len(listed.response.data) <= 1,
                   "the read contract must enforce its requested bounded page")
    metrics = opts.client.get("/api/incident/mojosec/case-metrics?days=1")
    th.assert_eq(metrics.status_code, 200,
                 f"the bounded aggregate metrics contract must be readable: {metrics.response}")
    th.assert_true("samples" not in metrics.response.data,
                   "metrics must never expose evidence arrays")
