"""WebApp onboarding durability, tenancy, and secret-boundary regressions."""

import socket
from unittest import mock

from testit import helpers as th

from tests.test_edge._helpers import (
    declare_release_buckets, login, make_group, make_group_member, make_webapp,
)


@th.django_unit_setup()
def setup_webapp_onboarding(opts):
    from mojo.apps.edge.models import WebAppOnboardingOperation

    WebAppOnboardingOperation.objects.all().delete()
    declare_release_buckets()
    opts.group = make_group("edge-onboard")
    opts.actor, opts.actor_email, opts.actor_password, _ = make_group_member(
        ["manage_webapp", "manage_dns"], group=opts.group)


@th.django_unit_test("onboarding state recursively redacts and bounds provider evidence")
def test_recursive_secret_redaction(opts):
    from mojo.apps.edge.services import webapp_onboarding

    payload = {"safe": {"token": "raw", "nested": [{"api_secret": "raw"}]},
               "authorization": "Bearer raw", "status": "verified"}
    cleaned = webapp_onboarding._safe(payload)

    assert cleaned["safe"]["token"] == "[redacted]", \
        "a nested token survived onboarding evidence sanitization"
    assert cleaned["safe"]["nested"][0]["api_secret"] == "[redacted]", \
        "a secret inside a list survived recursive sanitization"
    assert cleaned["authorization"] == "[redacted]", \
        "authorization material survived sanitization"
    assert "raw" not in str(cleaned), "raw secret text remained in safe evidence"


@th.django_unit_test("profile validation refuses workflow and path injection")
def test_profile_input_validation(opts):
    from mojo import errors as me
    from mojo.apps.edge import validators

    cases = [
        (validators.validate_github_repository, "owner/repo\nrun: curl evil"),
        (validators.validate_deployment_ref, "main\n${{ secrets.ALL }}"),
        (validators.validate_deployment_ref, "../main"),
        (validators.validate_build_output, "../../private"),
        (validators.validate_build_output, "dist; curl evil"),
    ]
    for validator, value in cases:
        try:
            validator(value)
            error = None
        except me.ValueException as exc:
            error = exc
        assert error is not None, f"{validator.__name__} accepted {value!r}"


@th.django_unit_test("address onboarding refuses apex and wildcard serving names")
def test_address_requires_one_concrete_label(opts):
    from mojo import errors as me
    from mojo.apps.edge.models import WebAppOnboardingOperation
    from mojo.apps.edge.services import webapp_onboarding

    for index, label in enumerate(("", "*")):
        operation = WebAppOnboardingOperation.objects.create(
            group=opts.group, actor=opts.actor, origin="http://testserver",
            replay_fingerprint=str(index + 7) * 64,
            state={"choices": {"address": {"label": label, "domain": 0}}})
        try:
            webapp_onboarding._advance_address(operation)
            error = None
        except me.ValueException as exc:
            error = exc
        assert error is not None, f"address onboarding accepted label {label!r}"


@th.django_unit_test("generated workflow is secret-free and quotes shell inputs")
def test_secret_free_workflow(opts):
    from mojo.apps.edge.services import webapp_onboarding

    web_app = make_webapp(opts.group, slug="appworkflow")
    web_app.github_repository = "NativeMojo/customer-portal"
    web_app.deployment_ref = "release/2026-08"
    web_app.build_output = "packages/web/dist"
    web_app.save()
    result = webapp_onboarding.workflow(web_app)

    assert result["schema_version"] == 1, "workflow contract is not versioned"
    assert "MOJO_DEPLOY_KEY: ${{ secrets.MOJO_DEPLOY_KEY }}" in result["yaml"], \
        "workflow does not consume the named GitHub secret"
    assert '"${BUILD_OUTPUT}"' in result["yaml"], \
        "validated build output is not quoted at its shell boundary"
    assert "Bearer " not in result["yaml"] and "preview-token" not in result["yaml"], \
        "generated workflow embedded credential material"


@th.django_unit_test("public verification rejects mixed private DNS before connecting")
def test_public_probe_rejects_mixed_dns(opts):
    from mojo.apps.edge.services import public_probe

    answers = [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443)),
    ]
    with mock.patch("socket.getaddrinfo", return_value=answers):
        try:
            public_probe.public_addresses("example.com")
            error = None
        except public_probe.UnsafePublicProbe as exc:
            error = exc
    assert error is not None, \
        "one public answer masked a private rebinding answer"


@th.django_unit_test("public verification uses root SNI Host and never follows redirects")
def test_public_probe_root_contract(opts):
    from mojo.apps.edge.services import public_probe

    response = mock.Mock(status=302)
    response.read.return_value = b""
    connection = mock.Mock()
    connection.getresponse.return_value = response
    with mock.patch.object(public_probe, "public_addresses", return_value=["93.184.216.34"]), \
            mock.patch.object(public_probe, "_PinnedHTTPSConnection", return_value=connection):
        result = public_probe.probe_https_root("https://example.com")

    connection.request.assert_called_once_with(
        "GET", "/", headers={"Host": "example.com",
                              "Accept": "text/html,*/*;q=0.1",
                              "Connection": "close"})
    assert result["ok"] is False and result["redirected"] is True, \
        "verification treated a redirect as proof or followed it"


@th.django_unit_test("GitHub evidence uses only an exact group installation")
def test_github_evidence_is_tenant_scoped(opts):
    from mojo.apps.github.models import GitHubInstall
    from mojo.apps.edge.models import WebAppOnboardingOperation
    from mojo.apps.edge.services import webapp_onboarding

    foreign = make_group("edge-onboard-foreign")
    GitHubInstall.objects.create(
        group=foreign, installation_id=91816001, account_name="foreign")
    operation = WebAppOnboardingOperation.objects.create(
        group=opts.group, actor=opts.actor, origin="https://example.com",
        replay_fingerprint="a" * 64)
    with mock.patch(
            "mojo.apps.github.services.github_app.get_install_token") as token:
        evidence = webapp_onboarding._github_evidence(
            operation, "NativeMojo/portal", "main", "dist")

    assert evidence["status"] == "unavailable", \
        "a foreign group's GitHub installation supplied repository evidence"
    token.assert_not_called()


@th.django_unit_test("summary v1 exports readiness without operation state or tokens")
def test_summary_is_frozen_and_secret_free(opts):
    from mojo.apps.edge.models import WebAppOnboardingOperation
    from mojo.apps.edge.services import webapp_onboarding

    web_app = make_webapp(opts.group, slug="appsummary")
    WebAppOnboardingOperation.objects.create(
        group=opts.group, actor=opts.actor, web_app=web_app,
        origin="https://example.com", replay_fingerprint="b" * 64,
        state={"confirm_token": "must-never-export"},
        evidence={"provider": {"token": "must-never-export", "status": "verified"}})
    result = webapp_onboarding.summary_for(web_app)

    assert result["schema_version"] == 1, "1818 summary dependency is not frozen at v1"
    assert result["webapp"]["id"] == web_app.pk, "summary changed WebApp identity"
    assert "state" not in result["onboarding"], "internal reconciliation state escaped"
    assert "must-never-export" not in str(result), "summary exported secret material"


@th.django_unit_test("operation detail authorizes a group member without a global grant")
def test_detail_uses_operation_group_scope(opts):
    from mojo.apps.edge.models import WebAppOnboardingOperation

    operation = WebAppOnboardingOperation.objects.create(
        group=opts.group, actor=opts.actor, origin="http://testserver",
        replay_fingerprint="c" * 64)
    login(opts, opts.actor_email, opts.actor_password)
    response = opts.client.get(
        f"/api/edge/webapp/onboarding/detail?operation={operation.operation_id}")

    assert response.status_code == 200, (
        "a group-scoped WebApp manager was blocked by an accidental global "
        f"permission gate ({response.status_code}: {response.body})")


@th.django_unit_test("stale worker release cannot resurrect cancellation")
def test_stale_lease_cannot_overwrite_cancel(opts):
    from mojo.apps.edge.models import WebAppOnboardingOperation
    from mojo.apps.edge.services import webapp_onboarding

    operation = WebAppOnboardingOperation.objects.create(
        group=opts.group, actor=opts.actor, origin="http://testserver",
        replay_fingerprint="d" * 64, lease_owner="worker-1")
    stale = WebAppOnboardingOperation.objects.get(pk=operation.pk)
    WebAppOnboardingOperation.objects.filter(pk=operation.pk).update(
        status="cancelled", lease_owner="", revision=1)

    released = webapp_onboarding._release(stale, "worker-1", wait=True)
    operation.refresh_from_db()
    assert released is False, "a stale worker retained authority after cancellation"
    assert operation.status == "cancelled", \
        "stale worker release resurrected a cancelled onboarding operation"


@th.django_unit_test("migration 0009 is the frozen onboarding edge")
def test_onboarding_migration_contract(opts):
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    migration = root / "mojo/apps/edge/migrations/0009_webapp_onboarding.py"
    text = migration.read_text()
    assert "WebAppOnboardingOperation" in text, \
        "edge 0009 does not create the onboarding ledger"
    assert "edge_webapp_onboarding_live_replay_uniq" in text, \
        "edge 0009 omitted live replay serialization"
    assert "dnsman', '0004_dnsrecordreservation" in text, \
        "edge 0009 lost its DNS reconciliation dependency"
