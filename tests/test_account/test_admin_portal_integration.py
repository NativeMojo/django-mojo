"""Merged Admin navigation, evidence, and production secret boundaries."""

import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from testit import helpers as th


ROOT = Path(__file__).resolve().parents[2]
ADMIN_EMAIL = "admin_portal_integration@test.com"
ADMIN_PASSWORD = "Admin_portal_integration_pw_99"
TARGET_EMAIL = "admin_portal_secret_target@test.com"


def _sentinel(kind):
    return f"ADMIN-SECRET-{kind}-{uuid.uuid4().hex}"


@th.django_unit_setup()
def setup_admin_portal_integration(opts):
    from mojo.apps.account.models import ApiKey, Group, User
    from mojo.apps.dnsman.models import Certificate, DnsCredential, Domain, DomainPurchase
    from mojo.apps.edge.models import WebApp
    from mojo.apps.logit.models import Log

    WebApp.objects.filter(slug="admin-integration-secret-app").delete()
    DomainPurchase.objects.filter(domain_name="admin-integration-secret.example").delete()
    Certificate.objects.filter(common_name="admin-integration-secret.example").delete()
    Domain.objects.filter(name="admin-integration-secret.example").delete()
    DnsCredential.objects.filter(name="admin-integration-secret-dns").delete()
    ApiKey.objects.filter(name__startswith="admin-integration-secret").delete()
    Group.objects.filter(name="admin-integration-secret-group").delete()
    User.objects.filter(email__in=(ADMIN_EMAIL, TARGET_EMAIL)).delete()
    Log.objects.filter(username=ADMIN_EMAIL).delete()

    admin = User.objects.create_user(
        username=ADMIN_EMAIL, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)
    admin.is_active = True
    admin.is_email_verified = True
    admin.requires_mfa = False
    admin.is_superuser = True
    admin.save()
    target = User.objects.create_user(
        username=TARGET_EMAIL, email=TARGET_EMAIL, password="initial-password")
    target.is_active = True
    target.is_email_verified = True
    target.requires_mfa = False
    target.password = _sentinel("PASSWORD-HASH")
    target.auth_key = _sentinel("RESET-STATE")
    target.save(update_fields=["password", "auth_key", "modified"])

    group = Group.objects.create(
        name="admin-integration-secret-group", kind="organization")
    dns_key = _sentinel("DNS-KEY")
    dns_secret = _sentinel("DNS-SECRET")
    credential = DnsCredential.objects.create(
        group=group, name="admin-integration-secret-dns", provider="godaddy",
        is_active=True, verified=True)
    credential.set_credentials(dns_key, dns_secret)
    credential.save(update_fields=["mojo_secrets", "modified"])
    domain = Domain.objects.create(
        group=group, user=admin, name="admin-integration-secret.example",
        provider="godaddy", credential=credential, status="active", verified=True)
    certificate_key = _sentinel("CERTIFICATE-KEY")
    certificate = Certificate.objects.create(
        domain=domain, common_name=domain.name, sans=[domain.name], status="active",
        cert_pem="-----BEGIN CERTIFICATE-----\nPUBLIC\n-----END CERTIFICATE-----")
    certificate.set_private_key_pem(certificate_key)
    certificate.save(update_fields=["mojo_secrets", "modified"])
    quote_token = _sentinel("REGISTRAR-TOKEN")
    purchase = DomainPurchase.objects.create(
        group=group, user=admin, domain_name=domain.name, status="quoted",
        price="14.00", currency="USD", confirm_token=quote_token)
    webapp = WebApp(
        group=group, slug="admin-integration-secret-app",
        display_name="Admin integration secret app", environment="production",
        bucket="admin-integration-bucket", prefix="pending")
    with mock.patch("mojo.apps.edge.validators.validate_web_app"):
        webapp.save(); webapp.prefix = webapp.storage_prefix(); webapp.save()

    opts.integration_admin = admin.pk
    opts.integration_target = target.pk
    opts.integration_group = group.pk
    opts.integration_credential = credential.pk
    opts.integration_certificate = certificate.pk
    opts.integration_purchase = purchase.pk
    opts.integration_webapp = webapp.pk
    opts.integration_seeded_secrets = [
        target.password, target.auth_key, dns_key, dns_secret,
        certificate_key, quote_token,
    ]


def _body(response):
    return json.dumps(response.json, default=str, sort_keys=True)


def _assert_absent(secrets, value, label):
    for secret in secrets:
        th.assert_true(secret not in value, f"{label} exposed a seeded secret")


@th.django_unit_test("Dashboard collectors are permission-first and never invoke Setup")
def test_dashboard_permission_matrix(opts):
    from mojo.apps.account.services import admin_platform

    user = mock.Mock(is_superuser=False)
    user.has_permission.side_effect = lambda values: False
    request = mock.Mock(user=user)
    with mock.patch.object(admin_platform, "_api") as api, \
            mock.patch.object(admin_platform, "_fleet") as fleet, \
            mock.patch.object(admin_platform, "_dashboard_webapps") as webapps, \
            mock.patch.object(admin_platform, "_security") as security, \
            mock.patch.object(admin_platform, "_dashboard_deployment") as deployments, \
            mock.patch("mojo.apps.account.services.system_readiness.run") as setup:
        report = admin_platform.dashboard_overview(request)
    th.assert_eq(report["overall"], "unknown",
                 "denied sources must not manufacture an overall state")
    th.assert_eq(report["observable_sources"], 0,
                 "permission-denied sources are not observable evidence")
    for name, source in report["sources"].items():
        th.assert_eq(source["status"], "permission_denied",
                     f"{name} must distinguish denial from source failure")
    for collector in (api, fleet, webapps, security, deployments, setup):
        th.assert_true(not collector.called,
                       "Dashboard issued a forbidden or Setup readiness read")


@th.django_unit_test("Dashboard status vocabulary preserves evidence semantics")
def test_dashboard_status_vocabulary(opts):
    from mojo.apps.account.services import admin_platform

    cases = {
        "unauthorized": "permission_denied",
        "unavailable": "unknown",
        "timeout": "degraded",
        "unconfigured": "unconfigured",
        "unhealthy": "unhealthy",
    }
    for incoming, expected in cases.items():
        actual = admin_platform._dashboard_envelope({
            "status": incoming, "data": {},
        })["status"]
        th.assert_eq(actual, expected,
                     f"Dashboard collapsed {incoming} into the wrong state")
    stale = admin_platform._dashboard_envelope({
        "status": "healthy", "data": {},
        "stale_after": "2000-01-01T00:00:00+00:00",
    })
    th.assert_eq(stale["status"], "stale",
                 "expired evidence remained healthy")


@th.django_unit_test("real Admin HTTP responses and resulting logs contain no non-reveal secrets")
def test_authenticated_secret_response_and_log_boundary(opts):
    from django.utils import timezone
    from mojo.apps.logit.models import Log

    aws_access = _sentinel("AWS-ACCESS")
    aws_secret = _sentinel("AWS-SECRET")
    with th.server_settings(
            AWS_ACCESS_KEY_ID=aws_access, AWS_SECRET_ACCESS_KEY=aws_secret,
            EDGE_RELEASE_BUCKETS=["admin-integration-bucket"]):
        th.assert_true(opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD),
                       "integration Admin login failed")
        started = timezone.now()
        pre_reveal = [
            opts.client.get(f"/api/user/{opts.integration_target}"),
            opts.client.get("/api/account/admin/bootstrap"),
        ]
        for response in pre_reveal:
            th.assert_eq(response.status_code, 200,
                         "pre-reveal password/reset-state read failed")
            _assert_absent(
                opts.integration_seeded_secrets + [aws_access, aws_secret],
                _body(response), "pre-reveal response")
        reveals = []

        temporary = opts.client.post(
            "/api/account/admin/user/password/temporary",
            json={"user": opts.integration_target})
        th.assert_eq(temporary.status_code, 200,
                     "temporary-password reveal endpoint failed")
        reveals.append((temporary.json.get("data") or {}).get("temporary_password"))

        created_key = opts.client.post("/api/group/apikey", json={
            "group": opts.integration_group,
            "name": f"admin-integration-secret-{uuid.uuid4().hex[:8]}",
            "permissions": {},
        })
        th.assert_eq(created_key.status_code, 200, "API-key reveal endpoint failed")
        created_data = created_key.json.get("data") or {}
        reveals.append(created_data.get("token"))
        rotated_key = opts.client.post("/api/account/admin/apikey/action", json={
            "api_key": created_data.get("id"), "action": "rotate",
        })
        th.assert_eq(rotated_key.status_code, 200, "API-key rotation reveal failed")
        reveals.append((rotated_key.json.get("data") or {}).get("token"))

        operation = str(uuid.uuid4())
        deploy_key = opts.client.post("/api/edge/webapp/link_key", json={
            "webapp": opts.integration_webapp, "action": "mint",
            "operation_id": operation,
        })
        th.assert_eq(deploy_key.status_code, 200,
                     "WebApp deployment-key reveal failed")
        reveals.append((deploy_key.json.get("data") or {}).get("token"))
        th.assert_true(all(isinstance(value, str) and value for value in reveals),
                       "an authorized reveal response omitted its one-time value")

        replay = opts.client.post("/api/edge/webapp/link_key", json={
            "webapp": opts.integration_webapp, "action": "mint",
            "operation_id": operation,
        })
        th.assert_eq(replay.status_code, 200, "deployment-key replay failed")
        th.assert_true((replay.json.get("data") or {}).get("token") is None,
                       "a replay disclosed MOJO_DEPLOY_KEY twice")

        secrets = opts.integration_seeded_secrets + reveals + [aws_access, aws_secret]
        responses = [*pre_reveal, replay]
        for path in (
                "/api/account/admin/bootstrap",
                "/api/account/admin/dashboard",
                "/api/account/admin/platform",
                "/api/account/admin/advanced",
                f"/api/user/{opts.integration_target}",
                f"/api/group/apikey?group={opts.integration_group}",
                f"/api/edge/webapp/key_status?webapp={opts.integration_webapp}",
                f"/api/edge/webapp/summary?webapp={opts.integration_webapp}",
                f"/api/dnsman/credential/{opts.integration_credential}",
                f"/api/dnsman/certificate/{opts.integration_certificate}",
                f"/api/dnsman/purchase/{opts.integration_purchase}",
                "/api/logs?graph=activity&size=50&sort=-created"):
            response = opts.client.get(path)
            th.assert_eq(response.status_code, 200,
                         f"non-reveal integration read failed for {path}")
            responses.append(response)
        for index, response in enumerate(responses):
            _assert_absent(secrets, _body(response), f"non-reveal response {index}")

        deadline = time.monotonic() + 3.0
        logs = []
        while time.monotonic() < deadline:
            logs = list(Log.objects.filter(
                created__gte=started).values(
                    "path", "kind", "log", "payload"))
            if any(row["path"] == "/api/edge/webapp/link_key" for row in logs):
                break
            time.sleep(0.05)
        th.assert_true(bool(logs), "real HTTP requests produced no Log evidence")
        _assert_absent(secrets, json.dumps(logs, default=str), "resulting Log rows")


@th.django_unit_test("onboarding credentials are replaced before file logging")
def test_onboarding_file_log_boundary(opts):
    from mojo.helpers.response import JsonResponse
    from mojo.middleware import logging as request_logging

    sentinel = _sentinel("ONBOARDING-FILE")
    logger = mock.Mock()
    with mock.patch.object(request_logging, "LOGIT_FILE_ALL", True), \
            mock.patch.object(request_logging, "LOGIT_DB_ALL", False), \
            mock.patch.object(request_logging, "LOGGER", logger):
        middleware = request_logging.LoggerMiddleware(
            lambda request: JsonResponse({"status": True, "token": sentinel}))
        for path in (
                "/api/edge/webapp/onboarding/choose",
                "/api/edge/webapp/onboarding/workflow"):
            request = SimpleNamespace(
                method="POST", path=path, ip="127.0.0.1",
                _raw_body=f'{{"confirm_token":"{sentinel}"}}')
            response = middleware(request)
            th.assert_eq(response.status_code, 200, "middleware fixture failed")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and logger.info.call_count < 4:
            time.sleep(0.02)
    output = json.dumps(logger.info.call_args_list, default=str)
    th.assert_true(logger.info.call_count >= 4,
                   "file logger did not observe request and response markers")
    th.assert_true(sentinel not in output,
                   "onboarding credential reached the file logger")
    th.assert_true(output.count("webapp_onboarding_secret") >= 4,
                   "request/response bodies were not replaced with fixed markers")


@th.django_unit_test("malformed forced-password credentials never enter incident metadata")
def test_malformed_forced_password_incident_boundary(opts):
    from mojo import errors as me
    from mojo.apps.account.utils import tokens
    from mojo.apps.incident.models import Event, Incident

    sentinel = uuid.uuid4().hex
    raw = f"tp:{sentinel}abcdef"
    before_event = Event.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
    before_incident = Incident.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
    with th.assert_raises(me.ValueException):
        tokens.verify_forced_password_token(raw)
    evidence = {
        "events": list(Event.objects.filter(pk__gt=before_event).values(
            "metadata", "details")),
        "incidents": list(Incident.objects.filter(pk__gt=before_incident).values(
            "metadata", "details")),
    }
    th.assert_true(bool(evidence["events"] or evidence["incidents"]),
                   "malformed credential produced no incident evidence")
    rendered = json.dumps(evidence, default=str)
    th.assert_true(raw not in rendered and sentinel not in rendered,
                   "malformed forced-password credential persisted in incident evidence")


@th.django_unit_test("private assets use five primary items and scrub every transient secret")
def test_merged_browser_secret_and_route_contract(opts):
    registry = (ROOT / "mojo/apps/account/admin_portal/assets/features/registry.js").read_text()
    routes = (ROOT / "mojo/apps/account/admin_portal/assets/components/routes.js").read_text()
    people = (ROOT / "mojo/apps/account/admin_portal/assets/features/people/page.js").read_text()
    webapps = (ROOT / "mojo/apps/account/admin_portal/assets/features/webapps/page.js").read_text()
    advanced = (ROOT / "mojo/apps/account/admin_portal/assets/features/advanced/page.js").read_text()
    preview = (ROOT / "bin/admin_preview_support/server.py").read_text()
    classifier = (ROOT / "mojo/helpers/request.py").read_text()
    th.assert_true(
        "[dashboard, people, webapps, platform, activity, advanced]" in registry,
        "feature order does not match the five-item product navigation")
    th.assert_true("navigation: () => []" in (
        ROOT / "mojo/apps/account/admin_portal/assets/features/advanced/feature.js").read_text(),
        "Advanced escaped its collapsed Platform disclosure")
    for key in ("subject_type", "subject_id", "subject_model", "inspector", "return"):
        th.assert_true(f"'{key}'" in routes, f"canonical route state omitted {key}")
    th.assert_true("graph=token" not in people,
                   "People recovered a stored API-key token")
    th.assert_true("data-one-time-secret" in people and "content.replaceChildren()" in people,
                   "People secret modals do not scrub their nodes")
    th.assert_true("result.token = null" in webapps and "secretField.value = ''" in webapps,
                   "WebApp secret modal does not scrub its token")
    th.assert_true("quote.token = null" in advanced and "secretPayload.api_secret = ''" in advanced,
                   "Advanced retains registrar or DNS credential secrets")
    th.assert_true("cls._safe_payload(value)" in preview and "[redacted]" in preview,
                   "preview events lack recursive secret redaction")
    for path in ("edge/webapp/link_key", "dnsman/credential/link",
                 "dnsman/registrar/quote", "dnsman/registrar/purchase",
                 "dnsman/certificate/material", "edge/material"):
        th.assert_true(path in classifier,
                       f"secret route classifier omitted {path}")
