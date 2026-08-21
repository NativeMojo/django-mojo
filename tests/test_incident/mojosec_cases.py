import copy
import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from testit import helpers as th
from tests.test_edge._helpers import (
    declare_pools, make_certificate, make_domain, make_vhost,
)


PREFIX = f"mojosec_case_test_{uuid.uuid4().hex[:10]}"
PASSWORD = "MojoSecCases##1"
DOMAIN_NAME = f"{PREFIX.replace('_', '-')}.example.test"
OTHER_DOMAIN_NAME = f"other-{PREFIX.replace('_', '-')}.example.test"


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


def _targets(opts, vhost_ids=None):
    return [{
        "installation_key_id": opts.case_api_key.pk,
        "vhost_ids": list(vhost_ids or [opts.case_vhost_id]),
        "include_fim": True,
    }]


def _settings(opts, vhost_ids=None, future_skew=300):
    def get_static(name, default=None, **kwargs):
        if name == "MOJOSEC_CASE_SHADOW_TARGETS":
            return _targets(opts, vhost_ids=vhost_ids)
        if name == "MOJOSEC_CASE_FUTURE_SKEW_SECONDS":
            return future_skew
        return default
    return get_static


@th.django_unit_setup()
def setup_mojosec_cases(opts):
    from mojo.apps.account.models import ApiKey, Group, User
    from mojo.apps.dnsman.models import Certificate, Domain
    from mojo.apps.edge.models import Vhost
    from mojo.apps.incident.models import (
        MojoSecCase, MojoSecCaseTransition, MojoSecReceipt)

    # Sweep by the constant stem: per-run uuid prefixes leak rows into the
    # long-lived database forever, and their canonical wire ids collide with
    # other files' globally-scoped lookups.
    stem = "mojosec_case_test_"
    MojoSecCaseTransition.maintenance_objects.filter(
        case__sensor_id__startswith=stem).delete()
    MojoSecReceipt.objects.filter(sensor_id__startswith=stem).delete()
    MojoSecCase.objects.filter(sensor_id__startswith=stem).delete()
    ApiKey.objects.filter(name__startswith=PREFIX).delete()
    Vhost.objects.filter(domain__name__in=(DOMAIN_NAME, OTHER_DOMAIN_NAME)).delete()
    Certificate.objects.filter(
        domain__name__in=(DOMAIN_NAME, OTHER_DOMAIN_NAME)).delete()
    Domain.objects.filter(name__in=(DOMAIN_NAME, OTHER_DOMAIN_NAME)).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(username__startswith=PREFIX).delete()

    declare_pools()
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
    other_group = Group.objects.create(
        name=f"{PREFIX}_other", kind="organization")
    other_domain = make_domain(name=OTHER_DOMAIN_NAME, group=other_group)
    other_vhost = make_vhost(
        other_domain, make_certificate(other_domain), kind="site", spa=True,
        mojosec_policy=policy)

    opts.case_api_key = key
    opts.case_vhost_id = vhost.pk
    opts.case_other_vhost_id = other_vhost.pk
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
        "edge_policy_version": 5,
        "scheme": "https",
    }
    secure = _event("web.probe", "1" * 64, count=10000, attributes=base)
    redirect = copy.deepcopy(secure)
    redirect.update({"id": "2" * 64, "count": 1})
    redirect["attributes"].update({
        "scheme": "http", "status": 301, "response_class": "spa_fallback"})
    first = _receipt(opts, secure)
    twin = _receipt(opts, redirect)

    with mock.patch.object(
            mojosec_correlation.settings, "get_static", side_effect=_settings(opts)), \
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
        "edge_policy_version": 5, "scheme": "https",
    }, observed="2026-08-18T02:12:00Z")
    receipt = _receipt(opts, web)

    def contribute_once():
        close_old_connections()
        try:
            return mojosec_correlation.contribute(receipt, web)[1]
        finally:
            close_old_connections()

    with mock.patch.object(
            mojosec_correlation.settings, "get_static", side_effect=_settings(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda unused: contribute_once(), range(2)))

        fim = _event("fim.change", "4" * 64, count=6636, attributes={
            "path": "/etc/ssh/sshd_config", "change": "modified",
            "expected_change": {
                "deployment_id": "deploy-2026-08-18",
                "operation_id": "release-42", "operation_kind": "release",
                "completed_at": "2026-08-18T02:12:00Z",
                "expires_at": "2026-08-18T02:27:00Z",
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
            mojosec_correlation.settings, "get_static", side_effect=_settings(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        for index in range(9):
            event = _event("web.probe", f"{index + 10:064x}", attributes={
                "source_ip": "198.51.101.88", "method": "GET", "status": 404,
                "request_uri": f"/wp-content/plugin-{index}/readme.txt?secret=drop-me",
                "response_class": "impossible_path",
                "resource_id": f"vhost:{opts.case_vhost_id}",
                "edge_policy_version": 5, "scheme": "https",
            }, observed="2026-08-18T01:05:00Z")
            receipt = _receipt(opts, event)
            cases.append(mojosec_correlation.contribute(receipt, event)[0])
        late = _event("web.probe", "f" * 64, attributes={
            "source_ip": "198.51.101.88", "method": "GET", "status": 404,
            "request_uri": "/wp-content/late/readme.txt",
            "response_class": "impossible_path",
            "resource_id": f"vhost:{opts.case_vhost_id}",
            "edge_policy_version": 5, "scheme": "https",
        }, observed="2026-08-18T00:59:59Z")
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
    th.assert_eq((late_case.window_start.hour, case.window_start.hour), (0, 1),
                 "hour rollover must remain explicit instead of mutating the active window")


@th.django_unit_test()
def test_unbound_response_identity_is_rejected_from_shadow_contribution(opts):
    from mojo.apps.incident.services import mojosec_correlation

    event = _event("web.probe", "e" * 64, count=2500, attributes={
        "source_ip": "198.51.100.121", "method": "GET", "status": 200,
        "response_bytes": 12345, "request_uri": "/wp-login.php",
        "resource_id": f"vhost:{opts.case_vhost_id}", "scheme": "https",
    }, observed="2026-08-18T05:05:00Z")
    with mock.patch.object(
            mojosec_correlation.settings, "get_static", side_effect=_settings(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        case, contributed = mojosec_correlation.contribute(_receipt(opts, event), event)
    th.assert_true(not contributed and case is None,
                   "wire shape and status must not bypass authoritative VHost binding")


@th.django_unit_test()
def test_web_shadow_requires_authoritative_vhost_tenant_and_policy_binding(opts):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from mojo.apps.incident.services import mojosec_correlation

    def evidence(event_id, vhost_id, version=5, response_class="impossible_path"):
        return _event("web.probe", event_id, attributes={
            "source_ip": "198.51.102.144", "method": "GET", "status": 404,
            "request_uri": "/wp-login.php", "response_class": response_class,
            "resource_id": f"vhost:{vhost_id}", "edge_policy_version": version,
            "scheme": "https",
        })

    rejected = []
    checks = (
        (evidence("a" * 64, 987654), [987654]),
        (evidence("b" * 64, opts.case_other_vhost_id),
         [opts.case_other_vhost_id]),
        (evidence("c" * 64, opts.case_vhost_id, version=4),
         [opts.case_vhost_id]),
        (evidence("d" * 64, opts.case_vhost_id, response_class="reverse_proxy"),
         [opts.case_vhost_id]),
    )
    for event, targets in checks:
        receipt = _receipt(opts, event)
        with mock.patch.object(
                mojosec_correlation.settings, "get_static",
                side_effect=_settings(opts, vhost_ids=targets)), \
                mock.patch.object(mojosec_correlation, "_record_metric"):
            rejected.append(mojosec_correlation.contribute(receipt, event))
        receipt.refresh_from_db()
        th.assert_true(receipt.case_contributed_at is None,
                       "unbound wire evidence must leave the raw receipt untouched")

    valid = evidence("6" * 64, opts.case_vhost_id)
    valid_receipt = _receipt(opts, valid)
    with mock.patch.object(
            mojosec_correlation.settings, "get_static", side_effect=_settings(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        with CaptureQueriesContext(connection) as binding_queries:
            binding = mojosec_correlation._shadow_binding(valid_receipt, valid)
        case, contributed = mojosec_correlation.contribute(valid_receipt, valid)

    th.assert_true(all(item == (None, False) for item in rejected),
                   "nonexistent, cross-group, and stale policy evidence must fail closed")
    th.assert_true(contributed and case.resource_id == f"vhost:{opts.case_vhost_id}",
                   "matching tenant ownership and authoritative policy must contribute")
    th.assert_true(binding is not None and len(binding_queries.captured_queries) <= 1,
                   "bounded target authorization must resolve with one VHost lookup")
    th.assert_eq((case.policy_version, case.urgency_reason),
                 (5, "trusted_impossible_path"),
                 "trusted classification must use the saved VHost policy")


@th.django_unit_test()
def test_future_evidence_uses_receipt_time_outside_static_skew(opts):
    from mojo.apps.incident.services import mojosec_correlation
    from mojo.helpers import dates

    future = dates.utcnow() + dates.timedelta(days=1)
    observed = future.isoformat().replace("+00:00", "Z")
    event = _event("web.probe", "7" * 64, attributes={
        "source_ip": "198.51.100.155", "method": "GET", "status": 404,
        "request_uri": "/wp-login.php", "response_class": "impossible_path",
        "resource_id": f"vhost:{opts.case_vhost_id}", "edge_policy_version": 5,
        "scheme": "https",
    }, observed=observed)
    receipt = _receipt(opts, event)
    with mock.patch.object(
            mojosec_correlation.settings, "get_static", side_effect=_settings(opts)), \
            mock.patch.object(mojosec_correlation, "_record_metric"):
        case, contributed = mojosec_correlation.contribute(receipt, event)
        within = receipt.created + dates.timedelta(seconds=299)
        bounded_within = mojosec_correlation._bounded_observed(within, receipt.created)

    th.assert_true(contributed, "future-skew handling must not alter ingestion authority")
    th.assert_eq(case.last_seen, receipt.created,
                 "far-future sensor time must not control case ordering or windows")
    th.assert_eq(bounded_within, within,
                 "timestamps within the configured skew remain sensor-observed time")
    th.assert_eq(receipt.replay_features["event"]["last_seen"], observed,
                 "receipt replay evidence must retain the raw sensor timestamp")


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
    from mojo.apps.incident.models import MojoSecCase
    from mojo.apps.incident.services import mojosec_correlation
    from mojo.decorators.limits import clear_rate_limits
    from mojo.helpers import dates

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
    page_too_deep = opts.client.get("/api/incident/mojosec/case?page=101")
    th.assert_eq(page_too_deep.status_code, 400,
                 "the list contract must reject offsets beyond page 100")
    page_too_wide = opts.client.get("/api/incident/mojosec/case?page_size=101")
    th.assert_eq(page_too_wide.status_code, 400,
                 "the list contract must reject page sizes beyond 100")
    metrics = opts.client.get("/api/incident/mojosec/case-metrics?days=1")
    th.assert_eq(metrics.status_code, 200,
                 f"the bounded aggregate metrics contract must be readable: {metrics.response}")
    th.assert_true("samples" not in metrics.response.data,
                   "metrics must never expose evidence arrays")

    originals = list(MojoSecCase.objects.filter(
        sensor_id=PREFIX, resource_id=f"vhost:{opts.case_vhost_id}").values_list(
            "pk", "last_seen"))
    th.assert_true(bool(originals),
                   "the metrics upper-bound proof requires a bound shadow case")
    MojoSecCase.objects.filter(pk__in=[row[0] for row in originals]).update(
        last_seen=dates.utcnow() + dates.timedelta(days=1))
    with mock.patch.object(
            mojosec_correlation.settings, "get_static", side_effect=_settings(opts)):
        future_metrics = opts.client.get(
            f"/api/incident/mojosec/case-metrics?days=1&resource_id="
            f"vhost:{opts.case_vhost_id}")
    th.assert_eq(future_metrics.response.data["cases"], 0,
                 "metrics must exclude rows beyond the configured future bound")
    for pk, last_seen in originals:
        MojoSecCase.objects.filter(pk=pk).update(last_seen=last_seen)
