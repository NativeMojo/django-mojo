"""Provider-facing paths of the assistant `webapp` domain.

Opt-in and serial because every test here patches a shared provider surface
(`dnsman.services.dns`, `helpers.dns.probe`, `dnsman.services.certs`,
`edge.services.public_probe`, `edge.services.webapp_deploy`) around an
in-process service call, and because the edge fixtures declare `EDGE_*`
settings. The default-core half of this domain lives in
`tests/test_edge/31_*` and `tests/test_edge/32_*`, which need none of that.

What is being proven throughout: the assistant never infers an outcome a
provider has not confirmed, never retries a no-retry contract, and never hands
the model a raw provider error.
"""

import uuid
from unittest import mock

from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools, declare_release_buckets, make_certificate, make_domain,
    make_group, make_group_member, make_release, make_vhost, make_webapp,
    with_setting,
)


TARGET = "edge-assistant-target.example.net"


def _call(name, params, user):
    from mojo.apps.assistant import get_registry

    return get_registry()[name]["handler"](params, user)


def _address_operation(opts, domain, label="app"):
    from mojo.apps.edge.models import WebAppOnboardingOperation
    from mojo.apps.edge.services import webapp_onboarding

    web_app = make_webapp(opts.group, slug=f"pp{uuid.uuid4().hex[:6]}")
    return WebAppOnboardingOperation.objects.create(
        group=opts.group, actor=opts.actor, web_app=web_app,
        origin=webapp_onboarding.ASSISTANT_ORIGIN,
        replay_fingerprint=uuid.uuid4().hex, cursor="address",
        state={"profile": {"slug": web_app.slug}, "choices": {
            "address": {"label": label, "domain": domain.pk}}})


@th.django_unit_setup()
def setup_webapp_provider_paths(opts):
    declare_pools()
    declare_release_buckets()
    opts.group = make_group("edge-asprov")
    opts.actor, _, _, _ = make_group_member(
        ["manage_webapp", "manage_dns"], group=opts.group)
    opts.actor.add_permission(["view_admin"])
    opts.actor.save()

    opts.viewer, _, _, _ = make_group_member(["view_dns"], group=opts.group)
    opts.viewer_home = make_group("edge-asprovhome")
    from mojo.apps.account.models import GroupMember
    member, _ = GroupMember.objects.get_or_create(
        user=opts.viewer, group=opts.viewer_home)
    member.permissions = {"manage_webapp": True, "manage_dns": True}
    member.save()
    opts.viewer.add_permission(["view_admin"])
    opts.viewer.save()


@th.django_unit_test("an external address parks at WAIT_FOR_USER and the tool shows the records")
def test_external_address_surfaces_its_records(opts):
    from mojo.apps.edge.services import webapp_onboarding

    domain = make_domain(group=opts.group, provider="mojo")
    operation = _address_operation(opts, domain)

    def run():
        with mock.patch("mojo.helpers.dns.probe.query_cname",
                        return_value=mock.Mock(targets=[])), \
                mock.patch("mojo.apps.dnsman.services.dns.upsert_record") as upsert:
            return webapp_onboarding._advance_address(operation), upsert

    outcome, upsert = with_setting("EDGE_WEBAPP_CNAME_TARGET", TARGET, run)
    assert outcome == webapp_onboarding.WAIT_FOR_USER, \
        f"an unpublished external CNAME did not wait for the operator: {outcome}"
    upsert.assert_not_called()
    operation.save()

    status = _call("get_webapp_setup_status",
                   {"operation_id": str(operation.operation_id)}, opts.actor)
    records = (status.get("evidence") or {}).get("address", {}).get("records") or []
    assert len(records) == 1, \
        f"the setup status did not surface exactly the app CNAME: {records}"
    assert (records[0]["name"] == f"app.{domain.name}"
            and records[0]["value"] == TARGET), \
        f"the record the operator must publish was not reported verbatim: {records}"
    assert status["cursor"] == "address" and status["status"] != "succeeded", \
        f"a setup waiting on the operator was reported as finished: {status}"


@th.django_unit_test("a foreign CNAME is refused and never overwritten")
def test_foreign_cname_is_refused(opts):
    from mojo.apps.edge.services import webapp_onboarding

    domain = make_domain(group=opts.group, provider="route53")
    operation = _address_operation(opts, domain)
    foreign = [mock.Mock(name_="x", type="CNAME")]
    foreign[0].name = f"app.{domain.name}"
    foreign[0].type = "CNAME"
    foreign[0].record_values = ["somewhere-else.example.net"]

    def run():
        with mock.patch("mojo.apps.dnsman.services.dns.list_records",
                        return_value=foreign), \
                mock.patch("mojo.apps.dnsman.services.dns.upsert_record") as upsert:
            error = None
            try:
                webapp_onboarding._advance_address(operation)
            except Exception as exc:
                error = exc
            return error, upsert

    error, upsert = with_setting("EDGE_WEBAPP_CNAME_TARGET", TARGET, run)
    assert error is not None and "foreign CNAME" in str(error), \
        f"an address pointing somewhere else was taken over: {error}"
    upsert.assert_not_called()


@th.django_unit_test("an ambiguous provider outcome is surfaced verbatim and never retried")
def test_ambiguous_provider_outcome_is_not_retried(opts):
    from mojo.apps.edge.services import webapp_onboarding

    domain = make_domain(group=opts.group, provider="route53")
    operation = _address_operation(opts, domain)

    def run():
        with mock.patch("mojo.apps.dnsman.services.dns.list_records",
                        return_value=[]), \
                mock.patch("mojo.apps.dnsman.services.dns.upsert_record",
                           side_effect=RuntimeError("provider token=do-not-log")) as upsert, \
                mock.patch("mojo.apps.edge.services.webapp_onboarding.publish"):
            outcome = webapp_onboarding.advance(operation.pk)
            return outcome, upsert

    outcome, upsert = with_setting("EDGE_WEBAPP_CNAME_TARGET", TARGET, run)
    assert upsert.call_count == 1, (
        "the ambiguous provider write was retried inside one pass; the "
        f"no-automatic-retry contract belongs to the service ({upsert.call_count})")
    operation.refresh_from_db()
    assert "ambiguous" in operation.last_error, \
        f"the ambiguous outcome was not recorded: {operation.last_error} / {outcome}"

    status = _call("get_webapp_setup_status",
                   {"operation_id": str(operation.operation_id)}, opts.actor)
    assert status["last_error"] == operation.last_error, \
        f"the tool did not surface the service's own words: {status['last_error']}"
    assert "do-not-log" not in str(status), \
        f"a raw provider error reached the model: {status}"


@th.django_unit_test("a certificate still issuing is reported as pending, never as attached")
def test_pending_certificate_is_never_reported_as_issued(opts):
    from mojo.apps.dnsman.models import Certificate

    domain = make_domain(group=opts.group, provider="mojo")
    certificate = make_certificate(domain)
    primary = make_vhost(domain, certificate, label="www", kind="site_api")
    web_app = make_webapp(opts.group, slug=f"cp{uuid.uuid4().hex[:6]}",
                          vhost=primary)
    alias_domain = make_domain(group=opts.group, provider="mojo")
    Certificate.objects.create(
        domain=alias_domain, common_name=alias_domain.name,
        sans=[alias_domain.name, f"*.{alias_domain.name}"], status="pending")

    def run():
        with mock.patch("mojo.helpers.dns.probe.query_cname",
                        return_value=mock.Mock(targets=[TARGET])), \
                mock.patch("mojo.apps.dnsman.services.certs.request_certificate") as request:
            result = _call("attach_webapp_address",
                           {"webapp": web_app.pk,
                            "hostname": f"shop.{alias_domain.name}"}, opts.actor)
            return result, request

    result, request = with_setting("EDGE_WEBAPP_CNAME_TARGET", TARGET, run)
    assert result.get("status") == "certificate_pending", (
        "an address whose certificate is still issuing was reported as "
        f"something other than pending: {result}")
    assert result.get("status") != "attached", \
        "a pending certificate was reported as a live address"
    request.assert_not_called()  # an in-flight order is never duplicated


@th.django_unit_test("health maps an unreachable origin to unhealthy with no raw error")
def test_health_never_leaks_a_raw_probe_error(opts):
    from mojo.apps.edge.services.public_probe import UnsafePublicProbe

    domain = make_domain(group=opts.group)
    certificate = make_certificate(domain)
    vhost = make_vhost(domain, certificate, label="hp", kind="site_api")
    web_app = make_webapp(opts.group, slug=f"hp{uuid.uuid4().hex[:6]}",
                          vhost=vhost)

    with mock.patch("mojo.apps.edge.services.public_probe.probe_https_root",
                    side_effect=UnsafePublicProbe("refusing to probe a private address")):
        unsafe = _call("check_webapp_health", {"webapp": web_app.pk}, opts.actor)
    assert unsafe["status"] == "unhealthy", \
        f"an unsafe probe target was not reported unhealthy: {unsafe}"

    with mock.patch("mojo.apps.edge.services.public_probe.probe_https_root",
                    side_effect=RuntimeError("connect failed to 10.1.2.3 token=abc")):
        crashed = _call("check_webapp_health", {"webapp": web_app.pk}, opts.actor)
    assert crashed["status"] == "unhealthy", \
        f"a crashing probe was not reported unhealthy: {crashed}"
    assert "10.1.2.3" not in str(crashed) and "token=abc" not in str(crashed), (
        "a raw probe exception reached the model, address internals and all: "
        f"{crashed}")

    with mock.patch("mojo.apps.edge.services.public_probe.probe_https_root",
                    return_value={"ok": False, "status": 502}):
        down = _call("check_webapp_health", {"webapp": web_app.pk}, opts.actor)
    assert down["status"] == "unhealthy" and down["detail"] == "HTTP 502", \
        f"a failing origin did not report its status line: {down}"


@th.django_unit_test("a failed fleet deployment reports counts to a viewer and bounded errors to a writer")
def test_failed_deployment_evidence_is_partitioned(opts):
    from mojo.apps.edge.models import WebAppDeployment

    domain = make_domain(group=opts.group)
    certificate = make_certificate(domain)
    vhost = make_vhost(domain, certificate, label="fd", kind="site_api")
    web_app = make_webapp(opts.group, slug=f"fd{uuid.uuid4().hex[:6]}",
                          vhost=vhost)
    release = make_release(web_app, "fdv1", status="uploaded")
    deployment = WebAppDeployment.objects.create(
        webapp=web_app, release=release, status="failed",
        detail="fleet deployment failed",
        targets=[{"runner": f"edge-secret-runner-{index:02d}", "job": index}
                 for index in range(8)])

    rows = [{"runner": f"edge-secret-runner-{index:02d}", "job": index,
             "status": "failed" if index else "completed",
             "error": f"node {index} exploded " + ("x" * 900),
             "result": {"status": "installed", "generation": 41 + index,
                        "changed": True}}
            for index in range(8)]

    def read(user):
        with mock.patch(
                "mojo.apps.edge.services.webapp_deploy.target_status",
                return_value=rows):
            return _call("get_webapp_deployment",
                         {"deployment": deployment.pk}, user)

    writer = read(opts.actor)
    viewer = read(opts.viewer)

    assert writer["nodes"]["expected"] == 8 and writer["nodes"]["failed"] == 7, \
        f"the failed-node count was wrong for a writer: {writer['nodes']}"
    assert viewer["nodes"]["expected"] == 8 and viewer["nodes"]["failed"] == 7, \
        f"the failed-node count was wrong for a viewer: {viewer['nodes']}"
    assert "errors" not in viewer["nodes"] and "detail" not in viewer["nodes"], \
        f"a viewer was handed node error text: {viewer['nodes']}"
    assert len(writer["nodes"]["errors"]) == 5, (
        "a writer was handed more than five node errors: "
        f"{len(writer['nodes']['errors'])}")
    for text in writer["nodes"]["errors"]:
        assert len(text) <= 200, f"a node error was not truncated: {len(text)}"
    assert "edge-secret-runner" not in str(writer) + str(viewer), \
        "a runner id reached the model on the failure path"
    assert writer["success"] is False and writer["terminal"] is True, \
        f"a failed deployment was not reported as a terminal failure: {writer}"
