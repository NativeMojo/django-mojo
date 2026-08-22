"""WebApp onboarding durability, tenancy, and secret-boundary regressions."""

import socket
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools, declare_release_buckets, login, make_certificate,
    make_domain, make_group, make_group_member, make_user, make_vhost,
    make_upstream, make_webapp, with_setting,
)


TARGET = "edge-webapp-target.example.net"


def _address_operation(opts, domain, label="app", slug=None):
    from mojo.apps.edge.models import WebAppOnboardingOperation

    web_app = make_webapp(opts.group, slug=slug or f"ext{uuid.uuid4().hex[:6]}")
    return WebAppOnboardingOperation.objects.create(
        group=opts.group, actor=opts.actor, web_app=web_app,
        origin="https://example.com", replay_fingerprint=uuid.uuid4().hex,
        cursor="address",
        state={"profile": {}, "choices": {
            "address": {"label": label, "domain": domain.pk}}})


@th.django_unit_setup()
def setup_webapp_onboarding(opts):
    from mojo.apps.edge.models import WebAppOnboardingOperation

    WebAppOnboardingOperation.objects.all().delete()
    declare_release_buckets()
    opts.group = make_group("edge-onboard")
    opts.actor, opts.actor_email, opts.actor_password, _ = make_group_member(
        ["manage_webapp", "manage_dns"], group=opts.group)
    opts.auth_upstream = make_upstream()
    from mojo.apps.account.models.setting import Setting
    Setting.set("EDGE_WEBAPP_AUTH_UPSTREAM", str(opts.auth_upstream.pk), group=None)


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


@th.django_unit_test("group intent rejects empty mixed boolean list zero and unknown forms")
def test_group_intent_validation(opts):
    from types import SimpleNamespace

    from mojo import errors as me
    from mojo.apps.edge.rest import webapp_onboarding

    invalid = (
        {}, {"group": ""}, {"group": 0}, {"group": False}, {"group": 1.0},
        {"group": [opts.group.pk]}, {"group_intent": "other"},
        {"group": opts.group.pk, "group_intent": "new"},
    )
    for data in invalid:
        request = SimpleNamespace(
            user=opts.actor, group_token=None, DATA=data)
        with th.assert_raises(me.ValueException):
            webapp_onboarding._group_intent(request)


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


@th.django_unit_test("generated workflow references the public action and embeds no secret")
def test_secret_free_workflow(opts):
    from mojo.apps.edge.services import webapp_onboarding

    web_app = make_webapp(opts.group, slug="appworkflow")
    web_app.github_repository = "NativeMojo/customer-portal"
    web_app.deployment_ref = "release/2026-08"
    web_app.build_output = "packages/web/dist"
    web_app.save()
    result = webapp_onboarding.workflow(web_app, "https://api.example.com/")

    yaml = result["yaml"]
    assert result["schema_version"] == 1, "workflow contract is not versioned"
    assert "MOJO_DEPLOY_KEY: ${{ secrets.MOJO_DEPLOY_KEY }}" in yaml, \
        "workflow does not consume the named GitHub secret"
    assert ("uses: NativeMojo/django-mojo/examples/github/actions/deploy-webapp@main"
            in yaml), "workflow does not reference the public composite action at @main"
    assert "python -m mojo_webapp" not in yaml, \
        "the generated workflow still names the nonexistent mojo_webapp module"
    assert 'api-url: "https://api.example.com"' in yaml, \
        "the platform origin was not passed through (trailing slash not trimmed?)"
    assert 'artifact-dir: "packages/web/dist"' in yaml, \
        "the validated build output is not passed as artifact-dir"
    assert "Bearer " not in yaml and "preview-token" not in yaml, \
        "generated workflow embedded credential material"


@th.django_unit_test("generated workflow versions every GitHub Actions attempt")
def test_workflow_uses_unique_attempt_version(opts):
    from mojo.apps.edge.services import webapp_onboarding

    web_app = make_webapp(opts.group, slug="appattemptversion")
    yaml = webapp_onboarding.workflow(web_app, "https://api.example.com")["yaml"]

    assert "version: ${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}" in yaml, \
        "workflow reruns would reuse the commit-only immutable release version"
    assert "version: ${{ github.sha }}\n" not in yaml, \
        "workflow retained the commit-only release version"


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


@th.django_unit_test("public verification rejects oversized DNS answers")
def test_public_probe_rejects_address_overflow(opts):
    from mojo.apps.edge.services import public_probe

    answers = [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
         (f"93.184.216.{index}", 443))
        for index in range(1, public_probe.MAX_ADDRESSES + 2)
    ]
    with mock.patch("socket.getaddrinfo", return_value=answers):
        with th.assert_raises(public_probe.UnsafePublicProbe):
            public_probe.public_addresses("example.com")


@th.django_unit_test("public verification bounds outstanding DNS resolutions")
def test_public_probe_resolver_capacity(opts):
    from mojo.apps.edge.services import public_probe

    slots = mock.Mock()
    slots.acquire.return_value = False
    pool = mock.Mock()
    with mock.patch.object(public_probe, "_RESOLVER_SLOTS", slots), \
            mock.patch.object(public_probe, "_RESOLVER_POOL", pool):
        with th.assert_raises(public_probe.UnsafePublicProbe):
            public_probe.public_addresses("example.com", timeout=0.05)
    pool.submit.assert_not_called()


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


@th.django_unit_test("summary v1 datetimes export as JSON strings, not raw datetimes")
def test_summary_datetimes_are_json_serializable(opts):
    import json

    from django.utils import timezone

    from mojo.apps.edge.models import WebAppDeployment
    from mojo.apps.edge.services import webapp_onboarding
    from tests.test_edge._helpers import make_release

    web_app = make_webapp(opts.group)
    release = make_release(web_app, "v1.0.0", status="live")
    web_app.current_release = release
    web_app.save(update_fields=["current_release", "modified"])
    WebAppDeployment.objects.create(
        webapp=web_app, release=release, status="live",
        finished=timezone.now())

    result = webapp_onboarding.summary_for(web_app)

    # The Admin Platform overview and the onboarding REST endpoint both hand
    # this dict straight to JsonResponse; a bare datetime raised
    # "... is not JSON serializable" the moment a webapp had a release/deploy.
    json.dumps(result)

    assert isinstance(result["current_release"]["created"], str), \
        "release.created leaked as a raw datetime and broke JSON serialization"
    assert isinstance(result["latest_deployment"]["created"], str), \
        "deployment.created leaked as a raw datetime and broke JSON serialization"
    assert isinstance(result["latest_deployment"]["finished"], str), \
        "deployment.finished leaked as a raw datetime and broke JSON serialization"


@th.django_unit_test("operation detail authorizes a group member without a global grant")
def test_detail_uses_operation_group_scope(opts):
    from mojo.apps.account.models import GroupMember
    from mojo.apps.edge.models import WebAppOnboardingOperation

    operation = WebAppOnboardingOperation.objects.create(
        group=opts.group, actor=opts.actor, origin=opts.client.host.rstrip("/"),
        replay_fingerprint="c" * 64)
    login(opts, opts.actor_email, opts.actor_password)
    response = opts.client.get(
        f"/api/edge/webapp/onboarding/detail?operation={operation.operation_id}")

    assert response.status_code == 200, (
        "a group-scoped WebApp manager was blocked by an accidental global "
        f"permission gate ({response.status_code}: {response.body})")

    member = GroupMember.objects.get(group=opts.group, user=opts.actor)
    member.permissions = {"manage_dns": True}
    member.save(update_fields=["permissions", "modified"])
    revoked = opts.client.get(
        f"/api/edge/webapp/onboarding/detail?operation={operation.operation_id}")
    assert revoked.status_code in (401, 403), (
        "revoking manage_webapp left operation detail readable through a DNS "
        f"permission ({revoked.status_code}: {revoked.body})")
    member.permissions = {"manage_webapp": True, "manage_dns": True}
    member.save(update_fields=["permissions", "modified"])


@th.django_unit_test("inactive group ancestor revokes request and worker onboarding authority")
def test_inactive_parent_denies_onboarding(opts):
    from pathlib import Path
    from types import SimpleNamespace

    from mojo import errors as me
    from mojo.apps.account.models import Group
    from mojo.apps.edge.models import WebAppOnboardingOperation
    from mojo.apps.edge.rest.webapp_onboarding import _group

    parent = Group.objects.create(name="onboarding-inactive-parent",
                                  kind="organization", is_active=False)
    opts.group.parent = parent
    opts.group.save(update_fields=["parent", "modified"])
    operation = WebAppOnboardingOperation.objects.create(
        group=opts.group, actor=opts.actor, origin="http://testserver",
        replay_fingerprint="e" * 64)
    request = SimpleNamespace(
        user=opts.actor, group_token=None, DATA={"group": opts.group.pk})
    try:
        _group(request)
        group_error = None
    except me.PermissionDeniedException as exc:
        group_error = exc
    assert group_error is not None, \
        "onboarding group selection ignored an inactive ancestor"

    login(opts, opts.actor_email, opts.actor_password)
    detail = opts.client.get(
        f"/api/edge/webapp/onboarding/detail?operation={operation.operation_id}")
    assert detail.status_code in (401, 403), \
        "operation detail ignored an inactive group ancestor"

    root = Path(__file__).resolve().parents[2]
    service = (root / "mojo/apps/edge/services/webapp_onboarding.py").read_text()
    assert service.count("webapp_authority.can_manage_group_webapps") >= 2, \
        "request or worker authority bypasses the centralized two-part gate"
    opts.group.parent = None
    opts.group.save(update_fields=["parent", "modified"])
    parent.delete()


@th.django_unit_test("new-group create is atomic replay-safe and derives its storage prefix")
def test_new_group_create_and_replay(opts):
    from django.test import RequestFactory
    from mojo.apps.account.models import Group, User
    from mojo.apps.edge.models import WebApp, WebAppOnboardingOperation
    from mojo.apps.edge.services import webapp_onboarding

    actor = User.objects.get(pk=opts.actor.pk)
    actor.permissions = {
        "manage_webapp": True, "manage_dns": True, "manage_groups": True}
    actor.save(update_fields=["permissions", "modified"])
    operation_id = str(uuid.uuid4())
    payload = {
        "operation_id": operation_id, "display_name": "Atomic Customer Portal",
        "slug": "atomic-customer-portal", "bucket": "edge-test-releases",
        "environment": "production", "deployment_ref": "main",
        "build_output": "dist", "github_repository": "",
    }
    before_groups = Group.objects.filter(name="Atomic Customer Portal").count()
    operation, created = webapp_onboarding.create(
        None, actor, "https://admin.example.com", payload, group_intent="new")
    replay, replay_created = webapp_onboarding.create(
        None, actor, "https://admin.example.com", payload, group_intent="new")
    web_app = WebApp.objects.get(pk=operation.web_app_id)

    assert created is True and replay_created is False, \
        "new-group create/replay did not distinguish the first commit"
    assert replay.pk == operation.pk and str(replay.operation_id) == operation_id, \
        "same UUID did not reconcile the authoritative receipt"
    assert operation.cursor == "address", \
        "new-group create did not return directly at Domain & DNS"
    assert operation.group.name == payload["display_name"], \
        "new group was not named from the WebApp display name"
    assert web_app.group_id == operation.group_id, \
        "new Group and WebApp were not paired"
    assert web_app.prefix == web_app.storage_prefix(), \
        "new WebApp retained a pending or foreign storage prefix"
    assert Group.objects.filter(name="Atomic Customer Portal").count() == before_groups + 1, \
        "same UUID created more than one owning group"
    assert WebAppOnboardingOperation.objects.filter(operation_id=operation_id).count() == 1, \
        "same UUID created more than one onboarding receipt"

    request = RequestFactory().post(
        "/api/edge/webapp/onboarding/cancel",
        HTTP_ORIGIN="https://admin.example.com", secure=True,
        HTTP_HOST="admin.example.com")
    request.user = actor
    request.group_token = None
    cancelled = webapp_onboarding.cancel(operation, request)
    assert cancelled.status == "cancelled", \
        "new-group onboarding did not cancel authoritatively"
    assert Group.objects.filter(pk=operation.group_id).exists() and \
        WebApp.objects.filter(pk=operation.web_app_id).exists(), \
        "cancellation deleted the committed recoverable Group/WebApp pair"

    changed = dict(payload, slug="different-profile")
    from mojo import errors as me
    with th.assert_raises(me.ValueException):
        webapp_onboarding.create(
            None, actor, "https://admin.example.com", changed,
            group_intent="new")
    with th.assert_raises(me.PermissionDeniedException):
        webapp_onboarding.create(
            None, actor, "https://other.example.com", payload,
            group_intent="new")


@th.django_unit_test("operation UUID binds actor origin intent and profile exactly")
def test_operation_uuid_identity_boundaries(opts):
    from mojo import errors as me
    from mojo.apps.account.models import User
    from mojo.apps.edge.services import webapp_onboarding

    actor = User.objects.get(pk=opts.actor.pk)
    actor.permissions = {
        "manage_webapp": True, "manage_dns": True, "manage_groups": True}
    actor.save(update_fields=["permissions", "modified"])
    other, _, _ = make_user(
        ["manage_webapp", "manage_dns", "manage_groups"])
    operation_id = str(uuid.uuid4())
    payload = {
        "operation_id": operation_id, "display_name": "Bound Portal",
        "slug": "bound-portal", "bucket": "edge-test-releases",
    }
    operation, _ = webapp_onboarding.create(
        None, actor, "https://admin.example.com", payload,
        group_intent="new")

    with th.assert_raises(me.PermissionDeniedException):
        webapp_onboarding.create(
            None, other, "https://admin.example.com", payload,
            group_intent="new")
    with th.assert_raises(me.ValueException):
        webapp_onboarding.create(
            opts.group, actor, "https://admin.example.com", payload,
            group_intent="existing")
    with th.assert_raises(me.ValueException):
        webapp_onboarding.create(
            None, actor, "https://admin.example.com",
            dict(payload, display_name="Changed Portal"),
            group_intent="new")
    assert operation.group.name == "Bound Portal", \
        "a refused UUID reuse changed the authoritative operation"


@th.django_unit_test("different UUIDs retain concrete-profile compatibility")
def test_existing_profile_different_uuid_compatibility(opts):
    from mojo.apps.account.models import GroupMember, User
    from mojo.apps.edge.services import webapp_onboarding

    actor = User.objects.get(pk=opts.actor.pk)
    actor.permissions = {}
    actor.save(update_fields=["permissions", "modified"])
    member = GroupMember.objects.get(group=opts.group, user=actor)
    member.permissions = {"manage_webapp": True, "manage_dns": True}
    member.save(update_fields=["permissions", "modified"])
    profile = {
        "display_name": "Compatible Existing Portal",
        "slug": "compatible-existing-portal", "bucket": "edge-test-releases",
    }
    first, created = webapp_onboarding.create(
        opts.group, actor, "https://admin.example.com",
        dict(profile, operation_id=str(uuid.uuid4())))
    second, replay_created = webapp_onboarding.create(
        opts.group, actor, "https://admin.example.com",
        dict(profile, operation_id=str(uuid.uuid4())))

    assert created is True and replay_created is False and first.pk == second.pk, \
        "a different UUID broke concrete-group profile reconciliation"


@th.django_unit_test("legacy blank display names remain adoptable during address restore")
def test_advance_app_adopts_legacy_blank_display_name(opts):
    from mojo import errors as me
    from mojo.apps.edge.models import WebAppOnboardingOperation
    from mojo.apps.edge.services import webapp_onboarding

    web_app = make_webapp(opts.group, slug="legacy-blank-name")
    web_app.display_name = ""
    web_app.save(update_fields=["display_name", "modified"])
    profile = {
        "slug": web_app.slug, "display_name": web_app.slug,
        "environment": web_app.environment, "bucket": web_app.bucket,
        "github_repository": web_app.github_repository,
        "deployment_ref": web_app.deployment_ref,
        "build_output": web_app.build_output,
    }
    operation = WebAppOnboardingOperation.objects.create(
        group=opts.group, actor=opts.actor, origin="https://example.com",
        replay_fingerprint=uuid.uuid4().hex, state={"profile": profile})

    assert webapp_onboarding._advance_app(operation) is True, \
        "the legacy WebApp was not adopted"
    assert operation.web_app_id == web_app.pk and operation.cursor == "address", \
        "adoption did not continue the existing WebApp's address restore"

    web_app.display_name = "A genuinely different name"
    web_app.save(update_fields=["display_name", "modified"])
    refused = WebAppOnboardingOperation.objects.create(
        group=opts.group, actor=opts.actor, origin="https://example.com",
        replay_fingerprint=uuid.uuid4().hex, state={"profile": profile})
    with th.assert_raises(me.ValueException):
        webapp_onboarding._advance_app(refused)


@th.django_unit_test("API-key and group-token requests cannot enter onboarding")
def test_noninteractive_credentials_denied(opts):
    from types import SimpleNamespace

    from mojo import errors as me
    from mojo.apps.edge.rest import webapp_onboarding

    requests = (
        SimpleNamespace(user=opts.actor, api_key=object(), group_token=None,
                        DATA={"group": opts.group.pk}),
        SimpleNamespace(user=opts.actor, api_key=None, group_token=object(),
                        DATA={"group": opts.group.pk}),
    )
    for request in requests:
        with th.assert_raises(me.PermissionDeniedException):
            webapp_onboarding._group_intent(request)


@th.django_unit_test("new-group validation and initial failure leave no partial rows")
def test_new_group_create_rolls_back_all_initial_rows(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.edge.models import WebApp, WebAppOnboardingOperation
    from mojo.apps.edge.services import webapp_onboarding

    actor = User.objects.get(pk=opts.actor.pk)
    actor.permissions = {
        "manage_webapp": True, "manage_dns": True, "groups": True}
    actor.save(update_fields=["permissions", "modified"])
    operation_id = str(uuid.uuid4())
    payload = {
        "operation_id": operation_id, "display_name": "Rollback Customer Portal",
        "slug": "rollback-customer-portal", "bucket": "edge-test-releases",
    }
    with mock.patch.object(
            webapp_onboarding, "_advance_app",
            side_effect=RuntimeError("deterministic storage failure")):
        with th.assert_raises(RuntimeError):
            webapp_onboarding.create(
                None, actor, "https://admin.example.com", payload,
                group_intent="new")

    assert not Group.objects.filter(name="Rollback Customer Portal").exists(), \
        "failed initial transaction leaked the owning Group"
    assert not WebApp.objects.filter(slug="rollback-customer-portal").exists(), \
        "failed initial transaction leaked the WebApp"
    assert not WebAppOnboardingOperation.objects.filter(
        operation_id=operation_id).exists(), \
        "failed initial transaction leaked the onboarding receipt"


@th.django_unit_test("concurrent same-UUID new-group requests converge on one receipt")
def test_new_group_create_concurrency(opts):
    from django.db import close_old_connections
    from mojo.apps.account.models import Group, User
    from mojo.apps.edge.models import WebAppOnboardingOperation
    from mojo.apps.edge.services import webapp_onboarding

    actor = User.objects.get(pk=opts.actor.pk)
    actor.permissions = {
        "manage_webapp": True, "manage_dns": True, "manage_groups": True}
    actor.save(update_fields=["permissions", "modified"])
    operation_id = str(uuid.uuid4())
    payload = {
        "operation_id": operation_id, "display_name": "Concurrent Customer Portal",
        "slug": "concurrent-customer-portal", "bucket": "edge-test-releases",
    }

    def create_once():
        close_old_connections()
        try:
            thread_actor = User.objects.get(pk=actor.pk)
            operation, created = webapp_onboarding.create(
                None, thread_actor, "https://admin.example.com", payload,
                group_intent="new")
            return operation.pk, created
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create_once(), range(2)))

    assert len({result[0] for result in results}) == 1, \
        f"concurrent UUID requests returned different receipts: {results}"
    assert sorted(result[1] for result in results) == [False, True], \
        f"concurrent UUID requests did not report one creation: {results}"
    assert Group.objects.filter(name="Concurrent Customer Portal").count() == 1, \
        "concurrent UUID requests committed duplicate groups"
    assert WebAppOnboardingOperation.objects.filter(operation_id=operation_id).count() == 1, \
        "concurrent UUID requests committed duplicate receipts"


@th.django_unit_test("new intent options are state-free and exact-group options retain evidence")
def test_group_intent_options(opts):
    from mojo.apps.edge.models import WebAppOnboardingOperation
    from mojo.apps.edge.services import webapp_onboarding

    before = WebAppOnboardingOperation.objects.count()
    new_options = webapp_onboarding.options(None, group_intent="new")
    existing_options = webapp_onboarding.options(
        opts.group, group_intent="existing")

    assert new_options["github_connected"] is False, \
        "new-group options reported a group-scoped GitHub installation"
    assert new_options["group_intent"] == "new", \
        "new-group options lost their explicit intent"
    assert existing_options["group_intent"] == "existing", \
        "concrete-group options lost their explicit intent"
    assert WebAppOnboardingOperation.objects.count() == before, \
        "options created onboarding state"


@th.django_unit_test("revoking either authority half stops worker progress")
def test_worker_rechecks_two_part_authority(opts):
    from mojo.apps.account.models import GroupMember, User
    from mojo.apps.edge.models import WebAppOnboardingOperation
    from mojo.apps.edge.services import webapp_onboarding

    actor = User.objects.get(pk=opts.actor.pk)
    actor.permissions = {}
    actor.save(update_fields=["permissions", "modified"])
    operation = WebAppOnboardingOperation.objects.create(
        group=opts.group, actor=actor, origin="http://testserver",
        replay_fingerprint="f" * 64,
        state={"profile": {
            "slug": "revoked-worker", "display_name": "Revoked worker",
            "environment": "production", "bucket": "edge-test-releases",
            "github_repository": "", "deployment_ref": "main",
            "build_output": "dist"}, "choices": {}, "intent": {},
            "group_intent": "existing"})
    member = GroupMember.objects.get(group=opts.group, user=actor)
    member.permissions = {"manage_webapp": True}
    member.save(update_fields=["permissions", "modified"])

    result = webapp_onboarding.advance(operation.pk, owner="revoked-worker-test")
    operation.refresh_from_db()

    assert result.startswith("waiting:Onboarding authority is no longer current"), \
        f"worker progressed after DNS authority revocation: {result}"
    assert operation.web_app_id is None and operation.cursor == "app", \
        "revoked worker created or advanced the WebApp"
    member.permissions = {"manage_webapp": True, "manage_dns": True}
    member.save(update_fields=["permissions", "modified"])


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


# ---------------------------------------------------------------------------
# URL-first precheck
# ---------------------------------------------------------------------------
@th.django_unit_test("precheck steers a path URL to a subdomain")
def test_precheck_steers_path(opts):
    from mojo.apps.edge.services import webapp_onboarding

    r = webapp_onboarding.precheck(opts.group, "example.com/myapp")
    assert r["verdict"] == "path", f"a path URL was not steered: {r}"
    assert r.get("suggestion") == "myapp.example.com", \
        f"path steering did not suggest a subdomain: {r}"


@th.django_unit_test("precheck offers www for an apex address")
def test_precheck_apex_offers_www(opts):
    from mojo.apps.edge.services import webapp_onboarding

    domain = make_domain(group=opts.group, provider="route53")
    r = webapp_onboarding.precheck(opts.group, f"https://{domain.name}")
    assert r["verdict"] == "apex", f"apex not detected: {r}"
    assert r["suggestion"] == f"www.{domain.name}", f"apex did not offer www: {r}"


@th.django_unit_test("precheck steers a multi-level label to one label")
def test_precheck_deep_label(opts):
    from mojo.apps.edge.services import webapp_onboarding

    domain = make_domain(group=opts.group, provider="route53")
    r = webapp_onboarding.precheck(opts.group, f"https://a.b.{domain.name}")
    assert r["verdict"] == "deep_label", f"deep label not steered: {r}"


@th.django_unit_test("precheck reports an unmatched domain without leaking foreign ones")
def test_precheck_unknown_and_cross_tenant(opts):
    from mojo.apps.edge.services import webapp_onboarding

    unknown = webapp_onboarding.precheck(
        opts.group, "https://app.no-such-group-domain-xyz.com")
    assert unknown["verdict"] == "domain_unknown", f"unknown domain: {unknown}"
    assert "external_available" in unknown["options"], \
        "domain_unknown did not advertise the external-domain option"

    foreign = make_group("edge-onboard-foreign2")
    foreign_domain = make_domain(group=foreign, provider="route53")
    leaked = webapp_onboarding.precheck(opts.group, f"https://app.{foreign_domain.name}")
    assert leaked["verdict"] == "domain_unknown", \
        f"a foreign group's domain was matched cross-tenant: {leaked}"


@th.django_unit_test("precheck reports a taken address")
def test_precheck_taken(opts):
    from mojo.apps.edge.services import webapp_onboarding

    declare_pools()
    domain = make_domain(group=opts.group, provider="route53")
    make_vhost(domain, make_certificate(domain), label="www")
    r = webapp_onboarding.precheck(opts.group, f"https://www.{domain.name}")
    assert r["verdict"] == "taken", f"an occupied address was not reported taken: {r}"


@th.django_unit_test("precheck reports ready for a managed domain and never lists provider records")
def test_precheck_managed_ready_uses_probe_only(opts):
    from unittest import mock

    from mojo.apps.edge.services import webapp_onboarding

    domain = make_domain(group=opts.group, provider="route53")
    with mock.patch("mojo.apps.dnsman.services.dns.list_records") as listed, \
            mock.patch("mojo.helpers.dns.probe.query_cname",
                       return_value=mock.Mock(targets=[])):
        r = with_setting(
            "EDGE_WEBAPP_CNAME_TARGET", TARGET,
            lambda: webapp_onboarding.precheck(opts.group, f"https://app.{domain.name}"))
    assert r["verdict"] == "ready", f"a clear managed address was not ready: {r}"
    listed.assert_not_called()  # a per-click zone enumeration is the whole thing to avoid


@th.django_unit_test("precheck reports records_needed for an external domain")
def test_precheck_external_records_needed(opts):
    from unittest import mock

    from mojo.apps.edge.services import webapp_onboarding

    domain = make_domain(group=opts.group, provider="mojo")
    with mock.patch("mojo.helpers.dns.probe.query_cname",
                    return_value=mock.Mock(targets=[])):
        r = with_setting(
            "EDGE_WEBAPP_CNAME_TARGET", TARGET,
            lambda: webapp_onboarding.precheck(opts.group, f"https://app.{domain.name}"))
    assert r["verdict"] == "records_needed", \
        f"an external domain with no published CNAME was not records_needed: {r}"
    # Exactly one record — the app CNAME. The _acme-challenge was already
    # verified to make this a mojo domain, so re-showing it would be noise (and
    # a second app on the same domain is a one-record add).
    assert len(r["records"]) == 1, \
        f"records-needed showed more than the single app CNAME: {r['records']}"
    assert (r["records"][0]["name"] == f"app.{domain.name}"
            and r["records"][0]["value"] == TARGET), \
        f"the app CNAME to publish was not the record shown: {r}"


# ---------------------------------------------------------------------------
# external-domain address advance
# ---------------------------------------------------------------------------
@th.django_unit_test("external address waits for the user and never writes DNS")
def test_external_address_waits_for_user(opts):
    from unittest import mock

    from mojo.apps.edge.services import webapp_onboarding

    domain = make_domain(group=opts.group, provider="mojo")
    op = _address_operation(opts, domain)

    def run():
        with mock.patch("mojo.helpers.dns.probe.query_cname",
                        return_value=mock.Mock(targets=[])), \
                mock.patch("mojo.apps.dnsman.services.dns.upsert_record") as upsert, \
                mock.patch("mojo.apps.dnsman.services.dns.list_records") as listed:
            outcome = webapp_onboarding._advance_address(op)
            return outcome, upsert, listed

    outcome, upsert, listed = with_setting("EDGE_WEBAPP_CNAME_TARGET", TARGET, run)
    assert outcome == webapp_onboarding.WAIT_FOR_USER, \
        f"an unpublished external CNAME did not wait for the user: {outcome}"
    upsert.assert_not_called()  # the platform holds no credential for this domain
    listed.assert_not_called()
    assert op.evidence.get("address", {}).get("dns") == "unpublished", \
        "the waiting evidence did not report the missing record"


@th.django_unit_test("a verified external CNAME requests the apex+wildcard cert")
def test_external_address_requests_wildcard_cert(opts):
    from unittest import mock

    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.edge.services import webapp_onboarding

    domain = make_domain(group=opts.group, provider="mojo")
    op = _address_operation(opts, domain)

    def issue(domain_arg, names=None):
        # A real row so the FK assignment is valid; created AFTER the reuse
        # scan, exactly as request_certificate does in production.
        return Certificate.objects.create(
            domain=domain_arg, common_name=domain_arg.name,
            sans=[domain_arg.name, f"*.{domain_arg.name}"], status="pending")

    def run():
        with mock.patch("mojo.helpers.dns.probe.query_cname",
                        return_value=mock.Mock(targets=[TARGET])), \
                mock.patch("mojo.apps.dnsman.services.dns.upsert_record") as upsert, \
                mock.patch("mojo.apps.dnsman.services.certs.request_certificate",
                           side_effect=issue) as request:
            outcome = webapp_onboarding._advance_address(op)
            return outcome, upsert, request

    outcome, upsert, request = with_setting("EDGE_WEBAPP_CNAME_TARGET", TARGET, run)
    assert outcome is True, f"a verified external CNAME did not reach cert issuance: {outcome}"
    upsert.assert_not_called()
    request.assert_called_once()
    assert request.call_args.kwargs.get("names") is None, (
        "a delegated cert must request the apex+wildcard profile (names=None), "
        f"got {request.call_args}")


@th.django_unit_test("a failed delegated cert waits for the user instead of spinning")
def test_external_failed_cert_waits(opts):
    from unittest import mock

    from mojo.apps.edge.services import webapp_onboarding

    domain = make_domain(group=opts.group, provider="mojo")
    op = _address_operation(opts, domain)
    op.attempts = 5  # an auto-retry, not a fresh user check
    op.save(update_fields=["attempts"])

    def run():
        with mock.patch("mojo.helpers.dns.probe.query_cname",
                        return_value=mock.Mock(targets=[TARGET])), \
                mock.patch("mojo.apps.dnsman.services.certs.request_certificate") as request:
            outcome = webapp_onboarding._advance_address(op)
            return outcome, request

    outcome, request = with_setting("EDGE_WEBAPP_CNAME_TARGET", TARGET, run)
    assert outcome == webapp_onboarding.WAIT_FOR_USER, \
        f"a failed delegated cert kept spinning instead of waiting: {outcome}"
    request.assert_not_called()  # no fresh cert on an auto-retry — only on user re-check
    assert op.evidence.get("address", {}).get("certificate") == "failed", \
        "the failed-cert wait did not surface the certificate state for the user"


@th.django_unit_test("an external domain reuses a covering wildcard certificate")
def test_external_reuses_wildcard_cert(opts):
    from unittest import mock

    from mojo.apps.edge.services import webapp_onboarding

    declare_pools()
    domain = make_domain(group=opts.group, provider="mojo")
    make_certificate(domain)  # active, sans = apex + *.domain
    op = _address_operation(opts, domain, label="app", slug="extreuse")

    def run():
        with mock.patch("mojo.helpers.dns.probe.query_cname",
                        return_value=mock.Mock(targets=[TARGET])), \
                mock.patch("mojo.apps.dnsman.services.certs.request_certificate") as request:
            outcome = webapp_onboarding._advance_address(op)
            return outcome, request

    outcome, request = with_setting("EDGE_WEBAPP_CNAME_TARGET", TARGET, run)
    assert outcome is True, f"a reusable wildcard cert did not complete the address step: {outcome}"
    request.assert_not_called()  # reused the existing wildcard, requested nothing
    op.web_app.refresh_from_db()
    assert op.web_app.vhost_id is not None, "no serving vhost was linked"
    assert op.web_app.vhost.kind == "site_api", \
        "onboarding did not create a hybrid WebApp vhost"
    from mojo.apps.edge.services import webapp_auth_routes
    routes = set(op.web_app.vhost.routes.values_list("path_prefix", flat=True))
    assert routes == set(webapp_auth_routes.auth_route_prefixes()), \
        f"onboarding omitted the hosted-auth route contract: {routes}"


@th.django_unit_test("change-address swaps the vhost and retires the old one")
def test_change_address_swaps_vhost(opts):
    from unittest import mock

    from mojo.apps.edge.models import Vhost
    from mojo.apps.edge.services import webapp_onboarding

    declare_pools()
    domain = make_domain(group=opts.group, provider="route53")
    cert = make_certificate(domain)
    old_vhost = make_vhost(domain, cert, label="old")
    web_app = make_webapp(opts.group, slug="swapapp", vhost=old_vhost)
    from mojo.apps.edge.models import WebAppOnboardingOperation
    op = WebAppOnboardingOperation.objects.create(
        group=opts.group, actor=opts.actor, web_app=web_app,
        origin="https://example.com", replay_fingerprint=uuid.uuid4().hex,
        cursor="address",
        state={"profile": {}, "choices": {
            "address": {"label": "new", "domain": domain.pk}}})

    def run():
        with mock.patch("mojo.apps.dnsman.services.dns.list_records", return_value=[]), \
                mock.patch("mojo.apps.dnsman.services.dns.upsert_record"):
            return webapp_onboarding._advance_address(op)

    outcome = with_setting("EDGE_WEBAPP_CNAME_TARGET", TARGET, run)
    assert outcome is True, f"the address swap did not complete: {outcome}"
    web_app.refresh_from_db()
    assert web_app.vhost_id and web_app.vhost_id != old_vhost.pk, \
        "the app was not repointed to the new address"
    assert not Vhost.objects.filter(pk=old_vhost.pk).exists(), \
        "the old serving vhost was not retired after the swap"


@th.django_unit_test("restoring an address reapplies an existing live release")
def test_restore_address_reconciles_current_release(opts):
    from unittest import mock

    from mojo.apps.edge.services import releases, webapp_onboarding
    from tests.test_edge._helpers import make_release

    declare_pools()
    domain = make_domain(group=opts.group, provider="route53")
    make_certificate(domain)
    web_app = make_webapp(opts.group, slug="restorelive")
    release = make_release(web_app, "restore-live-v1", status="live")
    web_app.current_release = release
    web_app.save(update_fields=["current_release", "modified"])
    from mojo.apps.edge.models import WebAppOnboardingOperation
    op = WebAppOnboardingOperation.objects.create(
        group=opts.group, actor=opts.actor, web_app=web_app,
        origin="https://example.com", replay_fingerprint=uuid.uuid4().hex,
        cursor="address", state={"profile": {}, "choices": {
            "address": {"label": "restorelive", "domain": domain.pk}}})

    def run():
        with mock.patch("mojo.apps.dnsman.services.dns.list_records",
                        return_value=[]), \
                mock.patch("mojo.apps.dnsman.services.dns.upsert_record"), \
                mock.patch.object(releases, "reconcile_current_release") as reconcile:
            outcome = webapp_onboarding._advance_address(op)
            return outcome, reconcile

    outcome, reconcile = with_setting("EDGE_WEBAPP_CNAME_TARGET", TARGET, run)
    assert outcome is True, f"address restore did not finish: {outcome}"
    reconcile.assert_called_once()
    assert reconcile.call_args.args[0].pk == web_app.pk, \
        "address restore reconciled another WebApp's release"


@th.django_unit_test("wait exhaustion parks as waiting and never fails terminally")
def test_wait_exhaustion_parks(opts):
    from unittest import mock

    from mojo.apps.edge.models.web_app_onboarding_operation import (
        STATUS_FAILED, STATUS_WAITING)
    from mojo.apps.edge.services import webapp_onboarding

    domain = make_domain(group=opts.group, provider="route53")
    op = _address_operation(opts, domain)
    op.attempts = webapp_onboarding.MAX_ATTEMPTS
    op.save(update_fields=["attempts"])

    # A plain provider wait (returns True) at the attempt ceiling must park, not
    # fail: onboarding that just took a while stays user-recoverable.
    def run():
        with mock.patch.object(webapp_onboarding, "_advance_address", return_value=True):
            return webapp_onboarding.advance(op.pk)

    with_setting("EDGE_WEBAPP_CNAME_TARGET", TARGET, run)
    op.refresh_from_db()
    assert op.status == STATUS_WAITING, \
        f"an exhausted wait failed terminally ({op.status}) instead of parking"
    assert op.status != STATUS_FAILED, "onboarding failed on a legitimate long wait"
    assert op.attempts == 0, "the parked step did not reset its attempt budget"


# ---------------------------------------------------------------------------
# destination derivation (item 2191)
# ---------------------------------------------------------------------------
@th.django_unit_test("managed precheck never reports ready with a blank serving destination")
def test_precheck_managed_missing_destination_is_not_blank_ready(opts):
    from unittest import mock

    from mojo.apps.edge.services import webapp_onboarding

    domain = make_domain(group=opts.group, provider="godaddy")

    # No override and no platform address: the platform owns a managed domain
    # but has nowhere to point it. It must not report the address ready carrying
    # a DNS record with a blank destination — the WMWX-stage regression. The
    # _base_url patch makes "unresolvable" deterministic under the shared DB.
    def run():
        with mock.patch("mojo.helpers.dns.probe.query_cname",
                        return_value=mock.Mock(targets=[])), \
                mock.patch("mojo.apps.edge.services.webapp_destination._base_url",
                           return_value=""):
            return webapp_onboarding.precheck(
                opts.group, f"https://app.{domain.name}")

    r = with_setting("EDGE_WEBAPP_CNAME_TARGET", "", run)
    for record in r.get("records") or []:
        assert record.get("value"), \
            f"precheck returned a DNS record with a blank destination: {r}"
    assert r["verdict"] == "configuration_required", (
        "a managed domain with no resolvable serving destination was not "
        f"reported configuration_required: {r}")
    assert r.get("setup_check") == "webapp_destination", \
        f"configuration_required did not point at the Setup check: {r}"
    assert "EDGE_" not in r.get("reason", "") and "vhost" not in r.get("reason", ""), \
        f"the operator-facing reason leaked a setting name or jargon: {r}"


@th.django_unit_test("managed precheck derives its destination from the platform address")
def test_precheck_managed_derives_destination_from_base_url(opts):
    from unittest import mock

    from mojo.apps.edge.services import webapp_onboarding

    for provider in ("route53", "godaddy"):
        domain = make_domain(group=opts.group, provider=provider)

        def run():
            with mock.patch("mojo.helpers.dns.probe.query_cname",
                            return_value=mock.Mock(targets=[])), \
                    mock.patch("mojo.apps.dnsman.services.dns.list_records") as listed, \
                    mock.patch(
                        "mojo.apps.edge.services.webapp_destination._base_url",
                        return_value="https://api.stage.example"):
                return webapp_onboarding.precheck(
                    opts.group, f"https://app.{domain.name}"), listed

        # No EDGE_WEBAPP_CNAME_TARGET — the destination comes from BASE_URL.
        r, listed = with_setting("EDGE_WEBAPP_CNAME_TARGET", "", run)
        assert r["verdict"] == "ready", \
            f"a {provider} domain with a derived destination was not ready: {r}"
        assert r.get("destination") == {
            "type": "CNAME", "value": "api.stage.example",
            "provenance": "platform_base_url"}, \
            f"the managed ready did not carry the derived destination: {r}"
        assert "records" not in r, \
            f"a managed ready must not carry a record to copy: {r}"
        listed.assert_not_called()  # the probe-only precheck rule is preserved


@th.django_unit_test("the address step writes the derived CNAME and records its provenance")
def test_advance_address_derives_destination(opts):
    from unittest import mock

    from mojo.apps.edge.services import webapp_onboarding

    declare_pools()
    domain = make_domain(group=opts.group, provider="route53")
    make_certificate(domain, common_name=f"app.{domain.name}",
                     sans=[f"app.{domain.name}"])
    op = _address_operation(opts, domain)

    def run():
        with mock.patch("mojo.apps.dnsman.services.dns.list_records", return_value=[]), \
                mock.patch("mojo.apps.dnsman.services.dns.upsert_record") as upsert, \
                mock.patch(
                    "mojo.apps.edge.services.webapp_destination._base_url",
                    return_value="https://api.stage.example"):
            outcome = webapp_onboarding._advance_address(op)
            return outcome, upsert

    # No override; the write target is derived from the platform address.
    outcome, upsert = with_setting("EDGE_WEBAPP_CNAME_TARGET", "", run)
    assert outcome is True, f"the derived-destination address step did not complete: {outcome}"
    upsert.assert_called_once()
    assert upsert.call_args.args[3] == ["api.stage.example"], \
        f"the platform wrote a CNAME to the wrong destination: {upsert.call_args}"
    address = op.evidence.get("address", {})
    assert address.get("writable") is True, \
        f"a managed address did not record that the platform writes it: {address}"
    assert address.get("destination", {}).get("provenance") == "platform_base_url", \
        f"the address evidence did not record the destination provenance: {address}"


@th.django_unit_test("a wildcard-synthesized CNAME answer is not a precheck conflict")
def test_precheck_wildcard_synthesis_is_not_conflict(opts):
    from unittest import mock

    from mojo.apps.edge.services import webapp_onboarding

    domain = make_domain(group=opts.group, provider="route53")

    # Every name in the zone answers with the same CNAME target — the shape of
    # a `*.{domain}` record's synthesis, never of a host-specific record. A
    # specific record the platform writes takes precedence over the wildcard,
    # so this must not block onboarding.
    def run():
        with mock.patch("mojo.helpers.dns.probe.query_cname",
                        return_value=mock.Mock(targets=["legacy.example.net"])), \
                mock.patch("mojo.apps.dnsman.services.dns.list_records") as listed:
            return webapp_onboarding.precheck(
                opts.group, f"https://app.{domain.name}"), listed

    r, listed = with_setting("EDGE_WEBAPP_CNAME_TARGET", TARGET, run)
    assert r["verdict"] == "ready", \
        f"a wildcard-synthesized answer was reported as a conflict: {r}"
    listed.assert_not_called()  # the probe-only precheck rule is preserved

    # A genuine host-specific record pointing elsewhere still blocks: the
    # random sibling label resolves clean while the requested hostname does not.
    def host_specific(fqdn, *args, **kwargs):
        if str(fqdn) == f"app.{domain.name}":
            return mock.Mock(targets=["legacy.example.net"])
        return mock.Mock(targets=[])

    def run_conflict():
        with mock.patch("mojo.helpers.dns.probe.query_cname",
                        side_effect=host_specific):
            return webapp_onboarding.precheck(
                opts.group, f"https://app.{domain.name}")

    r = with_setting("EDGE_WEBAPP_CNAME_TARGET", TARGET, run_conflict)
    assert r["verdict"] == "conflict", \
        f"a genuine foreign host-specific CNAME was not reported: {r}"


@th.django_unit_test("a covering wildcard CNAME means the address step writes no DNS")
def test_advance_address_wildcard_covers_no_write(opts):
    from unittest import mock

    from objict import objict

    from mojo.apps.edge.services import webapp_onboarding

    declare_pools()
    domain = make_domain(group=opts.group, provider="route53")
    make_certificate(domain)  # active apex+wildcard cert — reused, no issuance
    op = _address_operation(opts, domain, slug="wcskip")

    zone = [objict(type="CNAME", name=f"*.{domain.name}",
                   record_values=[TARGET], ttl=300)]

    def run():
        with mock.patch("mojo.apps.dnsman.services.dns.list_records",
                        return_value=zone), \
                mock.patch("mojo.apps.dnsman.services.dns.upsert_record") as upsert, \
                mock.patch("mojo.apps.dnsman.services.certs.request_certificate") as request:
            outcome = webapp_onboarding._advance_address(op)
            return outcome, upsert, request

    outcome, upsert, request = with_setting("EDGE_WEBAPP_CNAME_TARGET", TARGET, run)
    assert outcome is True, \
        f"a wildcard-covered address did not complete the step: {outcome}"
    upsert.assert_not_called()  # the wildcard already routes this hostname
    request.assert_not_called()  # and the wildcard certificate is reused
    op.web_app.refresh_from_db()
    assert op.web_app.vhost_id is not None, \
        "the wildcard-covered address produced no serving vhost"


@th.django_unit_test("a matching legacy A record is migrated to the managed CNAME")
def test_advance_address_migrates_matching_legacy_a(opts):
    from objict import objict

    from mojo.apps.edge.services import webapp_onboarding

    declare_pools()
    domain = make_domain(group=opts.group, provider="route53")
    make_certificate(domain)
    op = _address_operation(opts, domain, label="legacy-a", slug="legacy-a")
    hostname = f"legacy-a.{domain.name}"
    address = "93.184.216.34"
    zone = [objict(type="A", name=hostname,
                   record_values=[address], ttl=60)]
    answers = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                (address, 443))]

    def run():
        with mock.patch("socket.getaddrinfo", return_value=answers), \
                mock.patch("mojo.apps.dnsman.services.dns.list_records",
                           return_value=zone), \
                mock.patch("mojo.apps.dnsman.services.dns.delete_record") as delete, \
                mock.patch("mojo.apps.dnsman.services.dns.upsert_record") as upsert:
            outcome = webapp_onboarding._advance_address(op)
            return outcome, delete, upsert

    outcome, delete, upsert = with_setting(
        "EDGE_WEBAPP_CNAME_TARGET", TARGET, run)
    assert outcome is True, f"the matching legacy A record did not migrate: {outcome}"
    delete.assert_called_once_with(domain, "A", hostname, [address])
    upsert.assert_called_once_with(domain, "CNAME", hostname, [TARGET], ttl=300)


@th.django_unit_test("a foreign A record is refused and never overwritten")
def test_advance_address_refuses_foreign_a(opts):
    from objict import objict

    from mojo import errors as me
    from mojo.apps.edge.services import webapp_onboarding

    domain = make_domain(group=opts.group, provider="route53")
    op = _address_operation(opts, domain, label="foreign-a", slug="foreign-a")
    hostname = f"foreign-a.{domain.name}"
    zone = [objict(type="A", name=hostname,
                   record_values=["198.51.100.40"], ttl=60)]
    answers = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                ("93.184.216.34", 443))]

    def run():
        with mock.patch("socket.getaddrinfo", return_value=answers), \
                mock.patch("mojo.apps.dnsman.services.dns.list_records",
                           return_value=zone), \
                mock.patch("mojo.apps.dnsman.services.dns.delete_record") as delete, \
                mock.patch("mojo.apps.dnsman.services.dns.upsert_record") as upsert:
            with th.assert_raises(me.ValueException):
                webapp_onboarding._advance_address(op)
            return delete, upsert

    delete, upsert = with_setting("EDGE_WEBAPP_CNAME_TARGET", TARGET, run)
    delete.assert_not_called()
    upsert.assert_not_called()


@th.django_unit_test("a managed domain issues the apex+wildcard certificate profile")
def test_managed_cert_requests_wildcard_profile(opts):
    from unittest import mock

    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.edge.services import webapp_onboarding

    declare_pools()
    domain = make_domain(group=opts.group, provider="route53")
    op = _address_operation(opts, domain, slug="wcprofile")

    def issue(domain_arg, names=None):
        return Certificate.objects.create(
            domain=domain_arg, common_name=domain_arg.name,
            sans=[domain_arg.name, f"*.{domain_arg.name}"], status="pending")

    def run():
        with mock.patch("mojo.apps.dnsman.services.dns.list_records",
                        return_value=[]), \
                mock.patch("mojo.apps.dnsman.services.dns.upsert_record"), \
                mock.patch("mojo.apps.dnsman.services.certs.request_certificate",
                           side_effect=issue) as request:
            outcome = webapp_onboarding._advance_address(op)
            return outcome, request

    outcome, request = with_setting("EDGE_WEBAPP_CNAME_TARGET", TARGET, run)
    assert outcome is True, \
        f"the managed cert-issuance path did not queue and continue: {outcome}"
    request.assert_called_once()
    assert request.call_args.kwargs.get("names") is None and \
        len(request.call_args.args) == 1, (
        "a managed domain must issue the apex+wildcard profile (no names "
        f"narrowing) so one certificate serves every app: {request.call_args}")


@th.django_unit_test("the destination resolver honors override precedence and refuses unusable topology")
def test_destination_resolver(opts):
    from unittest import mock

    from mojo import errors as me
    from mojo.apps.edge.services import webapp_destination

    # The explicit override wins over the platform address, trailing dot stripped.
    def override_beats_base():
        with mock.patch("mojo.apps.edge.services.webapp_destination._base_url",
                        return_value="https://api.platform.example"):
            return webapp_destination.resolve()
    resolved = with_setting("EDGE_WEBAPP_CNAME_TARGET", "edge.example.net.", override_beats_base)
    assert resolved.provenance == "override" and resolved.value == "edge.example.net", \
        f"the override did not take precedence with its stripped value: {resolved}"

    # An invalid override is configuration-required, not a mid-flight crash.
    with th.assert_raises(webapp_destination.DestinationUnavailable):
        with_setting("EDGE_WEBAPP_CNAME_TARGET", "not a hostname",
                     webapp_destination.resolve)

    def with_base(value):
        def run():
            with mock.patch("mojo.apps.edge.services.webapp_destination._base_url",
                            return_value=value):
                return webapp_destination.resolve()
        return with_setting("EDGE_WEBAPP_CNAME_TARGET", "", run)

    derived = with_base("https://api.stage.example")
    assert derived.provenance == "platform_base_url" and derived.value == "api.stage.example", \
        f"a hostname BASE_URL did not derive a usable destination: {derived}"

    # A numeric platform address cannot be a CNAME destination.
    with th.assert_raises(webapp_destination.DestinationUnavailable):
        with_base("https://203.0.113.9")
    # No platform address at all is configuration-required.
    with th.assert_raises(webapp_destination.DestinationUnavailable):
        with_base("")

    # A hostname that is the platform's own address is a bad request, not an
    # unserveable installation.
    def self_ref():
        with mock.patch("mojo.apps.edge.services.webapp_destination._base_url",
                        return_value="https://app.stage.example"):
            return webapp_destination.resolve("app.stage.example")
    try:
        with_setting("EDGE_WEBAPP_CNAME_TARGET", "", self_ref)
        raised = None
    except webapp_destination.DestinationUnavailable as exc:
        raised = ("unavailable", exc)
    except me.ValueException as exc:
        raised = ("value", exc)
    assert raised and raised[0] == "value", \
        f"a self-referential hostname was not a plain value error: {raised}"


@th.django_unit_test("the create endpoint refuses before app creation on an unserveable installation")
def test_create_endpoint_refuses_without_destination(opts):
    from unittest import mock

    from django.test import RequestFactory
    from mojo.apps.account.models import User
    from mojo.apps.edge.models import WebApp, WebAppOnboardingOperation
    from mojo.apps.edge.rest import webapp_onboarding as rest_onboarding
    from mojo.apps.edge.services import webapp_destination

    actor = User.objects.get(pk=opts.actor.pk)
    before_ops = WebAppOnboardingOperation.objects.filter(group=opts.group).count()
    before_apps = WebApp.objects.filter(group=opts.group).count()
    payload = {
        "group": opts.group.pk, "operation_id": str(uuid.uuid4()),
        "slug": f"noserve{uuid.uuid4().hex[:6]}", "display_name": "No Destination",
        "bucket": "edge-test-releases", "environment": "production",
        "deployment_ref": "main", "build_output": "dist", "github_repository": "",
    }
    request = RequestFactory().post(
        "/api/edge/webapp/onboarding/create",
        HTTP_ORIGIN="https://admin.example.com", secure=True,
        HTTP_HOST="admin.example.com")
    request.user = actor
    request.group_token = None
    request.DATA = payload

    def run():
        with mock.patch("mojo.apps.edge.services.webapp_destination._base_url",
                        return_value=""):
            return rest_onboarding.on_webapp_onboarding_create(request)

    # The connected-domain and purchase paths reach create without a precheck
    # verdict; the endpoint must refuse before any WebApp exists, and before a
    # purchase could move money in the address step that follows.
    with th.assert_raises(webapp_destination.DestinationUnavailable):
        with_setting("EDGE_WEBAPP_CNAME_TARGET", "", run)
    assert WebAppOnboardingOperation.objects.filter(group=opts.group).count() == before_ops, \
        "the refused create still minted an onboarding operation"
    assert WebApp.objects.filter(group=opts.group).count() == before_apps, \
        "the refused create still minted a WebApp"


@th.django_unit_test("options reports the resolved destination or a plain configuration error")
def test_options_reports_destination(opts):
    from unittest import mock

    from mojo.apps.edge.services import webapp_onboarding

    def resolved():
        with mock.patch("mojo.apps.edge.services.webapp_destination._base_url",
                        return_value="https://api.stage.example"):
            return webapp_onboarding.options(opts.group)
    ok = with_setting("EDGE_WEBAPP_CNAME_TARGET", "", resolved)
    assert ok["destination"] == {
        "type": "CNAME", "value": "api.stage.example",
        "provenance": "platform_base_url"}, \
        f"options did not report the resolved destination: {ok}"
    assert ok["destination_error"] is None, \
        f"a resolvable destination still reported an error: {ok}"

    def unresolved():
        with mock.patch("mojo.apps.edge.services.webapp_destination._base_url",
                        return_value=""):
            return webapp_onboarding.options(opts.group)
    bad = with_setting("EDGE_WEBAPP_CNAME_TARGET", "", unresolved)
    assert bad["destination"] is None and bad["destination_error"], \
        f"options did not surface the configuration error for the wizard gate: {bad}"


@th.django_unit_test("external precheck with no destination is configuration_required, never a blank record")
def test_precheck_external_missing_destination(opts):
    from unittest import mock

    from mojo.apps.edge.services import webapp_onboarding

    domain = make_domain(group=opts.group, provider="mojo")

    def run():
        with mock.patch("mojo.helpers.dns.probe.query_cname",
                        return_value=mock.Mock(targets=[])), \
                mock.patch("mojo.apps.edge.services.webapp_destination._base_url",
                           return_value=""):
            return webapp_onboarding.precheck(
                opts.group, f"https://app.{domain.name}")

    r = with_setting("EDGE_WEBAPP_CNAME_TARGET", "", run)
    assert r["verdict"] == "configuration_required", \
        f"an external domain with no destination was not configuration_required: {r}"
    for record in r.get("records") or []:
        assert record.get("value"), \
            f"external precheck offered a record with a blank destination: {r}"


# ---------------------------------------------------------------------------
# GitHub optional (item 2223 phase 1)
# ---------------------------------------------------------------------------
@th.django_unit_test("github step skips on request or when no repository exists")
def test_github_skip_advances_to_verify(opts):
    from mojo.apps.edge.models import WebAppOnboardingOperation
    from mojo.apps.edge.services import webapp_onboarding

    for choices in ({"github": {"skip": True}}, {"github": {}}):
        web_app = make_webapp(opts.group, slug=f"skip{uuid.uuid4().hex[:6]}")
        op = WebAppOnboardingOperation.objects.create(
            group=opts.group, actor=opts.actor, web_app=web_app,
            origin="https://example.com", replay_fingerprint=uuid.uuid4().hex,
            cursor="github",
            state={"profile": {"github_repository": ""}, "choices": choices})
        outcome = webapp_onboarding._advance_github(op)
        assert outcome is True, \
            f"a repo-less github step did not continue for {choices}: {outcome}"
        assert op.cursor == "verify", \
            f"a repo-less github step did not advance to verify: {op.cursor}"
        assert op.evidence.get("github", {}).get("status") == "skipped", \
            f"the skipped github step left the wrong evidence: {op.evidence}"
        assert op.attempts == 0 and op.next_attempt_at is None, \
            "the skipped github step retained an error budget"
        web_app.refresh_from_db()
        assert web_app.github_repository == "", \
            "a skipped github step wrote repository state onto the WebApp"


@th.django_unit_test("a given repository still runs the full github evidence path")
def test_github_repo_still_validated(opts):
    from mojo.apps.edge.models import WebAppOnboardingOperation
    from mojo.apps.edge.services import webapp_onboarding

    web_app = make_webapp(opts.group, slug=f"ghfull{uuid.uuid4().hex[:6]}")
    op = WebAppOnboardingOperation.objects.create(
        group=opts.group, actor=opts.actor, web_app=web_app,
        origin="https://example.com", replay_fingerprint=uuid.uuid4().hex,
        cursor="github",
        state={"profile": {"github_repository": ""},
               "choices": {"github": {"repository": "NativeMojo/portal"}}})
    with mock.patch.object(
            webapp_onboarding, "_github_evidence",
            return_value={"status": "verified"}) as evidence:
        outcome = webapp_onboarding._advance_github(op)

    assert outcome is True and op.cursor == "verify", \
        f"a given repository did not verify and advance: {op.cursor}"
    evidence.assert_called_once()
    web_app.refresh_from_db()
    assert web_app.github_repository == "NativeMojo/portal", \
        "the verified repository was not recorded on the WebApp"
    assert op.evidence.get("github", {}).get("status") == "verified", \
        f"the github evidence was not recorded: {op.evidence}"


@th.django_unit_test("workflow renders without a repository and unchanged with one")
def test_workflow_repository_optional(opts):
    from mojo.apps.edge.services import webapp_onboarding

    web_app = make_webapp(opts.group, slug=f"wfopt{uuid.uuid4().hex[:6]}")
    assert web_app.github_repository == "", "fixture unexpectedly set a repository"
    result = webapp_onboarding.workflow(web_app, "https://api.example.com")

    assert result["repository"] is None, \
        f"a repo-less workflow did not report repository None: {result['repository']}"
    assert f'webapp-id: "{web_app.pk}"' in result["yaml"], \
        "the repo-less workflow lost the webapp id"
    assert 'api-url: "https://api.example.com"' in result["yaml"], \
        "the repo-less workflow lost the api origin"

    web_app.github_repository = "NativeMojo/portal"
    web_app.save(update_fields=["github_repository", "modified"])
    with_repo = webapp_onboarding.workflow(web_app, "https://api.example.com")
    assert with_repo["repository"] == "NativeMojo/portal", \
        "a set repository was not validated and returned unchanged"


# ---------------------------------------------------------------------------
# apps-domain resolver + converge (item 2223 phase 1)
# ---------------------------------------------------------------------------
def _fresh_group_tree():
    """parent -> child, plus an unrelated sibling-of-parent group."""
    from mojo.apps.account.models import Group

    parent = make_group("edge-appsdom")
    child = Group.objects.create(
        name=f"edge-appsdom-child_{uuid.uuid4().hex[:8]}",
        kind="organization", parent=parent)
    stranger = make_group("edge-appsdom-stranger")
    return parent, child, stranger


@th.django_unit_test("the apps domain resolves for the group and its descendants only")
def test_apps_domain_resolver_scope(opts):
    from mojo.apps.edge.services import webapp_apps_domain

    parent, child, stranger = _fresh_group_tree()
    domain = make_domain(group=parent, provider="route53")

    with mock.patch(
            "mojo.apps.edge.services.webapp_apps_domain._base_url",
            return_value="https://api.unrelated.example"):
        own = webapp_apps_domain.resolve(parent)
        inherited = webapp_apps_domain.resolve(child)
        foreign = webapp_apps_domain.resolve(stranger)
        reason = webapp_apps_domain.no_domain_reason(stranger)

    assert own is not None and own.pk == domain.pk, \
        f"the owning group did not resolve its own domain: {own}"
    assert inherited is not None and inherited.pk == domain.pk, \
        f"a child group did not inherit its ancestor's domain: {inherited}"
    assert foreign is None, \
        f"an unrelated group resolved someone else's domain: {foreign}"
    assert reason == webapp_apps_domain.NO_DOMAIN_REASON, \
        f"the no-domain reason was not the plain-language one: {reason}"


@th.django_unit_test("the apps domain prefers the BASE_URL suffix and skips unwritable domains")
def test_apps_domain_resolver_preference(opts):
    from mojo.apps.edge.services import webapp_apps_domain

    parent, _, _ = _fresh_group_tree()
    preferred = make_domain(group=parent, provider="route53")
    make_domain(group=parent, provider="godaddy")

    with mock.patch(
            "mojo.apps.edge.services.webapp_apps_domain._base_url",
            return_value=f"https://api.{preferred.name}"):
        picked = webapp_apps_domain.resolve(parent)
    assert picked is not None and picked.pk == preferred.pk, \
        f"the BASE_URL-suffix domain was not preferred: {picked}"

    # Two candidates and no suffix match: no silent guess.
    with mock.patch(
            "mojo.apps.edge.services.webapp_apps_domain._base_url",
            return_value="https://api.unrelated.example"):
        ambiguous = webapp_apps_domain.resolve(parent)
        reason = webapp_apps_domain.no_domain_reason(parent)
    assert ambiguous is None, \
        f"an ambiguous multi-domain workspace resolved a guess: {ambiguous}"
    assert reason == webapp_apps_domain.AMBIGUOUS_REASON, \
        f"the ambiguous case did not carry its own plain reason: {reason}"

    # An external (provider mojo) domain is not writable and never resolves.
    mojo_only = make_group("edge-appsdom-ext")
    make_domain(group=mojo_only, provider="mojo")
    with mock.patch(
            "mojo.apps.edge.services.webapp_apps_domain._base_url",
            return_value=""):
        external = webapp_apps_domain.resolve(mojo_only)
    assert external is None, \
        f"an unwritable external domain resolved as the apps domain: {external}"


@th.django_unit_test("converge writes wildcard coverage once and is idempotent")
def test_apps_domain_converge_idempotent(opts):
    from objict import objict

    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.edge.services import webapp_apps_domain

    parent, _, _ = _fresh_group_tree()
    domain = make_domain(group=parent, provider="route53")

    def issue(domain_arg, names=None):
        return Certificate.objects.create(
            domain=domain_arg, common_name=domain_arg.name,
            sans=[domain_arg.name, f"*.{domain_arg.name}"], status="pending")

    def first_pass():
        with mock.patch("mojo.apps.dnsman.services.dns.list_records",
                        return_value=[]), \
                mock.patch("mojo.apps.dnsman.services.dns.upsert_record") as upsert, \
                mock.patch("mojo.apps.dnsman.services.certs.request_certificate",
                           side_effect=issue) as request:
            return webapp_apps_domain.converge(domain), upsert, request

    first, upsert, request = with_setting(
        "EDGE_WEBAPP_CNAME_TARGET", TARGET, first_pass)
    assert first.record_written is True, \
        f"an uncovered domain did not get its wildcard record: {first}"
    assert first.certificate_requested is True and first.certificate, \
        f"an uncovered domain did not get a certificate request: {first}"
    upsert.assert_called_once_with(
        domain, "CNAME", f"*.{domain.name}", [TARGET], ttl=300)
    request.assert_called_once()
    assert first.destination == TARGET, \
        f"converge did not report the serving destination: {first}"

    zone = [objict(type="CNAME", name=f"*.{domain.name}",
                   record_values=[TARGET], ttl=300)]

    def second_pass():
        with mock.patch("mojo.apps.dnsman.services.dns.list_records",
                        return_value=zone), \
                mock.patch("mojo.apps.dnsman.services.dns.upsert_record") as upsert, \
                mock.patch("mojo.apps.dnsman.services.certs.request_certificate") as request:
            return webapp_apps_domain.converge(domain), upsert, request

    second, upsert, request = with_setting(
        "EDGE_WEBAPP_CNAME_TARGET", TARGET, second_pass)
    assert second.record_written is False and second.certificate_requested is False, \
        f"a covered domain was converged again: {second}"
    upsert.assert_not_called()
    request.assert_not_called()


@th.django_unit_test("precheck and the address step accept an ancestor-owned domain")
def test_ancestor_domain_onboards_end_to_end(opts):
    from mojo.apps.edge.models import WebAppOnboardingOperation
    from mojo.apps.edge.services import webapp_onboarding

    declare_pools()
    parent, child, stranger = _fresh_group_tree()
    domain = make_domain(group=parent, provider="route53")
    make_certificate(domain)  # active apex+wildcard: issuance is already done
    # Writes to an ancestor-owned domain require authority in the OWNING
    # group. Grants inherit downward, so the organization grants at the
    # PARENT — which covers this child's onboarding too. (Previously this
    # test rode on opts.actor, whose grants live in an unrelated group.)
    parent_actor, _, _, _ = make_group_member(
        ["manage_webapp", "manage_dns"], group=parent)

    # Precheck: the child group sees its ancestor's domain...
    def run_precheck(group):
        with mock.patch("mojo.helpers.dns.probe.query_cname",
                        return_value=mock.Mock(targets=[])), \
                mock.patch(
                    "mojo.apps.edge.services.webapp_apps_domain._base_url",
                    return_value=""):
            return webapp_onboarding.precheck(group, f"https://app.{domain.name}")

    ready = with_setting(
        "EDGE_WEBAPP_CNAME_TARGET", TARGET, lambda: run_precheck(child))
    assert ready["verdict"] == "ready", \
        f"a child group's precheck did not accept the ancestor's domain: {ready}"

    # ...while an unrelated group still gets the non-disclosing verdict.
    unknown = with_setting(
        "EDGE_WEBAPP_CNAME_TARGET", TARGET, lambda: run_precheck(stranger))
    assert unknown["verdict"] == "domain_unknown", \
        f"an unrelated group learned about a foreign domain: {unknown}"

    # Address advance: the child onboards under the parent's domain, and the
    # lazy backstop converges the WILDCARD instead of writing a per-host CNAME.
    web_app = make_webapp(child, slug=f"anc{uuid.uuid4().hex[:6]}")
    op = WebAppOnboardingOperation.objects.create(
        group=child, actor=parent_actor, web_app=web_app,
        origin="https://example.com", replay_fingerprint=uuid.uuid4().hex,
        cursor="address",
        state={"profile": {}, "choices": {
            "address": {"label": "app", "domain": domain.pk}}})

    def run_advance():
        with mock.patch("mojo.apps.dnsman.services.dns.list_records",
                        return_value=[]), \
                mock.patch("mojo.apps.dnsman.services.dns.upsert_record") as upsert, \
                mock.patch("mojo.apps.dnsman.services.certs.request_certificate") as request, \
                mock.patch(
                    "mojo.apps.edge.services.webapp_apps_domain._base_url",
                    return_value=""):
            outcome = webapp_onboarding._advance_address(op)
            return outcome, upsert, request

    outcome, upsert, request = with_setting(
        "EDGE_WEBAPP_CNAME_TARGET", TARGET, run_advance)
    assert outcome is True, \
        f"the ancestor-domain address step did not complete: {outcome}"
    upsert.assert_called_once_with(
        domain, "CNAME", f"*.{domain.name}", [TARGET], ttl=300)
    request.assert_not_called()  # the active wildcard certificate is reused
    web_app.refresh_from_db()
    assert web_app.vhost_id is not None, \
        "the ancestor-domain onboarding produced no serving vhost"
    assert web_app.vhost.server_name == f"app.{domain.name}", \
        f"the vhost serves the wrong name: {web_app.vhost.server_name}"

    # A sibling branch choosing the same domain keeps the same refusal.
    from mojo import errors as me
    foreign_app = make_webapp(stranger, slug=f"anc{uuid.uuid4().hex[:6]}")
    foreign_op = WebAppOnboardingOperation.objects.create(
        group=stranger, actor=opts.actor, web_app=foreign_app,
        origin="https://example.com", replay_fingerprint=uuid.uuid4().hex,
        cursor="address",
        state={"profile": {}, "choices": {
            "address": {"label": "other", "domain": domain.pk}}})
    with th.assert_raises(me.PermissionDeniedException):
        with_setting("EDGE_WEBAPP_CNAME_TARGET", TARGET,
                     lambda: webapp_onboarding._advance_address(foreign_op))


@th.django_unit_test("ancestor-domain writes require authority in the owning workspace")
def test_ancestor_domain_write_requires_owning_group_authority(opts):
    from mojo import errors as me
    from mojo.apps.edge.models import WebAppOnboardingOperation
    from mojo.apps.edge.services import webapp_onboarding

    declare_pools()
    parent, child, _ = _fresh_group_tree()
    domain = make_domain(group=parent, provider="route53")
    make_certificate(domain)  # active apex+wildcard — no issuance in this test

    # Granted at the CHILD only: reading the ancestor's domain is the
    # inheritance contract, writing to it is not — grants inherit downward,
    # never upward.
    child_actor, _, _, _ = make_group_member(
        ["manage_webapp", "manage_dns"], group=child)
    web_app = make_webapp(child, slug=f"upw{uuid.uuid4().hex[:6]}")
    op = WebAppOnboardingOperation.objects.create(
        group=child, actor=child_actor, web_app=web_app,
        origin="https://example.com", replay_fingerprint=uuid.uuid4().hex,
        cursor="address",
        state={"profile": {}, "choices": {
            "address": {"label": "app", "domain": domain.pk}}})

    def run_denied():
        with mock.patch("mojo.apps.dnsman.services.dns.list_records") as listed, \
                mock.patch("mojo.apps.dnsman.services.dns.upsert_record") as upsert, \
                mock.patch(
                    "mojo.apps.edge.services.webapp_apps_domain.converge") as converge:
            try:
                webapp_onboarding._advance_address(op)
                error = None
            except me.PermissionDeniedException as exc:
                error = exc
            return error, listed, upsert, converge

    error, listed, upsert, converge = with_setting(
        "EDGE_WEBAPP_CNAME_TARGET", TARGET, run_denied)
    assert error is not None, \
        "a child-only grant wrote DNS in the ancestor's workspace"
    assert "workspace that owns it" in str(error), \
        f"the refusal was not the plain owning-workspace message: {error}"
    upsert.assert_not_called()  # refused before ANY provider write
    converge.assert_not_called()
    listed.assert_not_called()
    web_app.refresh_from_db()
    assert web_app.vhost_id is None, \
        "the refused onboarding still produced a serving vhost"

    # The SAME grant made at the PARENT instead — inherited downward over the
    # child — lets the identical onboarding proceed exactly as before.
    parent_actor, _, _, _ = make_group_member(
        ["manage_webapp", "manage_dns"], group=parent)
    op.actor = parent_actor
    op.save(update_fields=["actor", "modified"])

    def run_granted():
        with mock.patch("mojo.apps.dnsman.services.dns.list_records",
                        return_value=[]), \
                mock.patch("mojo.apps.dnsman.services.dns.upsert_record") as upsert, \
                mock.patch("mojo.apps.dnsman.services.certs.request_certificate") as request, \
                mock.patch(
                    "mojo.apps.edge.services.webapp_apps_domain._base_url",
                    return_value=""):
            outcome = webapp_onboarding._advance_address(op)
            return outcome, upsert, request

    outcome, upsert, request = with_setting(
        "EDGE_WEBAPP_CNAME_TARGET", TARGET, run_granted)
    assert outcome is True, \
        f"a parent-level grant did not complete the ancestor address step: {outcome}"
    upsert.assert_called_once_with(
        domain, "CNAME", f"*.{domain.name}", [TARGET], ttl=300)
    request.assert_not_called()  # the active wildcard certificate is reused
    web_app.refresh_from_db()
    assert web_app.vhost_id is not None, \
        "the parent-granted onboarding produced no serving vhost"


@th.django_unit_test("an address serving another app is refused before any cert work")
def test_advance_address_refuses_foreign_apps_vhost(opts):
    from mojo import errors as me
    from objict import objict

    from mojo.apps.edge.services import webapp_onboarding

    declare_pools()
    domain = make_domain(group=opts.group, provider="route53")
    cert = make_certificate(domain)
    occupied = make_vhost(domain, cert, label="app", kind="site_api")
    occupant = make_webapp(
        opts.group, slug=f"occ{uuid.uuid4().hex[:6]}", vhost=occupied)
    op = _address_operation(opts, domain, label="app",
                            slug=f"newby{uuid.uuid4().hex[:6]}")

    zone = [objict(type="CNAME", name=f"app.{domain.name}",
                   record_values=[TARGET], ttl=300)]

    def run():
        with mock.patch("mojo.apps.dnsman.services.dns.list_records",
                        return_value=zone), \
                mock.patch("mojo.apps.dnsman.services.dns.upsert_record") as upsert, \
                mock.patch("mojo.apps.dnsman.services.certs.request_certificate") as request:
            try:
                webapp_onboarding._advance_address(op)
                error = None
            except me.PermissionDeniedException as exc:
                error = exc
            return error, upsert, request

    error, upsert, request = with_setting("EDGE_WEBAPP_CNAME_TARGET", TARGET, run)
    assert error is not None, \
        "an address bound to another app was adopted instead of refused"
    assert "already used by another app" in str(error), \
        f"the refusal was not the plain already-used message: {error}"
    request.assert_not_called()  # refused before any certificate work
    occupant.refresh_from_db()
    assert occupant.vhost_id == occupied.pk, \
        "the refusal still unlinked the occupying app's address"
    op.web_app.refresh_from_db()
    assert op.web_app.vhost_id is None, \
        "the refused onboarding still adopted another app's vhost"


@th.django_unit_test("taken discloses the occupying slug to the owning group only")
def test_precheck_taken_slug_disclosure(opts):
    from mojo.apps.edge.services import webapp_onboarding

    declare_pools()
    parent, child, _ = _fresh_group_tree()
    domain = make_domain(group=parent, provider="route53")
    vhost = make_vhost(domain, make_certificate(domain), label="www")
    occupant = make_webapp(
        parent, slug=f"tkn{uuid.uuid4().hex[:6]}", vhost=vhost)

    own = webapp_onboarding.precheck(parent, f"https://www.{domain.name}")
    assert own["verdict"] == "taken", \
        f"the owning group's occupied address was not reported taken: {own}"
    assert own["app"] == occupant.slug, \
        f"the owning group did not see which of its apps holds the address: {own}"

    inherited = webapp_onboarding.precheck(child, f"https://www.{domain.name}")
    assert inherited["verdict"] == "taken", \
        f"the ancestor-domain occupied address lost its taken verdict: {inherited}"
    assert inherited["app"] is None, \
        f"a child group learned another workspace's app slug: {inherited}"


@th.django_unit_test("options carries the apps domain or its plain-language reason")
def test_options_reports_apps_domain(opts):
    from mojo.apps.edge.services import webapp_apps_domain, webapp_onboarding

    parent, _, _ = _fresh_group_tree()
    domain = make_domain(group=parent, provider="route53")

    def resolved():
        with mock.patch(
                "mojo.apps.edge.services.webapp_apps_domain._base_url",
                return_value=""), \
                mock.patch(
                    "mojo.apps.edge.services.webapp_destination._base_url",
                    return_value="https://api.stage.example"):
            return webapp_onboarding.options(parent)

    ok = with_setting("EDGE_WEBAPP_CNAME_TARGET", "", resolved)
    assert ok["apps_domain"] == {
        "id": domain.pk, "name": domain.name, "provider": "route53"}, \
        f"options did not carry the resolved apps domain: {ok['apps_domain']}"
    assert ok["apps_domain_error"] is None, \
        f"a resolved apps domain still reported an error: {ok}"

    empty = make_group("edge-appsdom-none")

    def unresolved():
        with mock.patch(
                "mojo.apps.edge.services.webapp_apps_domain._base_url",
                return_value=""), \
                mock.patch(
                    "mojo.apps.edge.services.webapp_destination._base_url",
                    return_value="https://api.stage.example"):
            return webapp_onboarding.options(empty)

    none = with_setting("EDGE_WEBAPP_CNAME_TARGET", "", unresolved)
    assert none["apps_domain"] is None, \
        f"a domain-less group still resolved an apps domain: {none['apps_domain']}"
    assert none["apps_domain_error"] == webapp_apps_domain.NO_DOMAIN_REASON, \
        f"the domain-less reason was not the plain-language one: {none}"


# ---------------------------------------------------------------------------
# actor/origin seams (item 2571): the assistant continues its OWN operations
# ---------------------------------------------------------------------------
def _seam_operation(opts, origin, cursor="github"):
    from mojo.apps.edge.models import WebAppOnboardingOperation

    web_app = make_webapp(opts.group, slug=f"seam{uuid.uuid4().hex[:6]}")
    return WebAppOnboardingOperation.objects.create(
        group=opts.group, actor=opts.actor, web_app=web_app, origin=origin,
        replay_fingerprint=uuid.uuid4().hex, cursor=cursor,
        state={"profile": {"slug": web_app.slug}, "choices": {}, "intent": {}})


def _seam_state(operation):
    operation.refresh_from_db()
    return {
        "choices": (operation.state or {}).get("choices"),
        "intent": (operation.state or {}).get("intent"),
        "status": operation.status,
        "cursor": operation.cursor,
        "attempts": operation.attempts,
        "revision": operation.revision,
        "last_error": operation.last_error,
        "activity": [row.get("message") for row in (operation.activity or [])],
    }


@th.django_unit_test("read authority is actor-bound and deliberately origin-free")
def test_read_authority_is_origin_free(opts):
    from mojo import errors as me
    from mojo.apps.edge.services import webapp_onboarding

    operation = _seam_operation(opts, webapp_onboarding.ASSISTANT_ORIGIN)
    # No raise: the SAME administrator may report on a setup they started on
    # the other surface. Continuing it is what stays origin-bound.
    webapp_onboarding.assert_read_authority(operation, opts.actor)

    stranger, _, _ = make_user(["manage_webapp", "manage_dns"])
    with th.assert_raises(me.PermissionDeniedException):
        webapp_onboarding.assert_read_authority(operation, stranger)


@th.django_unit_test("continue authority binds the origin and refuses group tokens")
def test_continue_authority_binds_origin(opts):
    from mojo import errors as me
    from mojo.apps.edge.services import webapp_onboarding

    operation = _seam_operation(opts, webapp_onboarding.ASSISTANT_ORIGIN)
    webapp_onboarding.assert_continue_authority(
        operation, opts.actor, webapp_onboarding.ASSISTANT_ORIGIN)

    with th.assert_raises(me.PermissionDeniedException):
        webapp_onboarding.assert_continue_authority(
            operation, opts.actor, "https://admin.example.com")

    browser = _seam_operation(opts, "https://admin.example.com")
    with th.assert_raises(me.PermissionDeniedException):
        webapp_onboarding.assert_continue_authority(
            browser, opts.actor, webapp_onboarding.ASSISTANT_ORIGIN)

    token_error = None
    try:
        webapp_onboarding.assert_continue_authority(
            operation, opts.actor, webapp_onboarding.ASSISTANT_ORIGIN,
            has_group_token=True)
    except me.PermissionDeniedException as exc:
        token_error = exc
    assert token_error is not None and "Group tokens" in str(token_error), \
        f"a group token continued onboarding: {token_error}"


@th.django_unit_test("_assert_current keeps its refusal order for a cross-origin stranger")
def test_assert_current_refusal_order_unchanged(opts):
    from django.test import RequestFactory

    from mojo import errors as me
    from mojo.apps.edge.services import webapp_onboarding

    operation = _seam_operation(opts, "https://admin.example.com")
    stranger, _, _ = make_user(["manage_webapp", "manage_dns"])
    request = RequestFactory().post(
        "/api/edge/webapp/onboarding/choose",
        HTTP_ORIGIN="https://evil.example.com", secure=True,
        HTTP_HOST="admin.example.com")
    request.user = stranger
    request.group_token = None

    error = None
    try:
        webapp_onboarding._assert_current(operation, request, mutate=True)
    except me.PermissionDeniedException as exc:
        error = exc
    assert error is not None, "a foreign actor continued another admin's onboarding"
    assert str(error) == "Only the initiating administrator may continue", (
        "the foreign-actor refusal changed once the origin check moved; a "
        f"cross-origin request now answers first: {error}")


@th.django_unit_test("choose_for_actor and the browser choose produce identical state")
def test_choose_for_actor_matches_browser_choose(opts):
    from django.test import RequestFactory

    from mojo.apps.edge.services import webapp_onboarding

    browser_op = _seam_operation(opts, "https://admin.example.com")
    assistant_op = _seam_operation(opts, webapp_onboarding.ASSISTANT_ORIGIN)
    payload = {"revision": browser_op.revision, "step": "github",
               "choice": {"skip": True}}

    request = RequestFactory().post(
        "/api/edge/webapp/onboarding/choose",
        HTTP_ORIGIN="https://admin.example.com", secure=True,
        HTTP_HOST="admin.example.com")
    request.user = opts.actor
    request.group_token = None

    webapp_onboarding.choose(browser_op, request, dict(payload))
    webapp_onboarding.choose_for_actor(
        assistant_op, opts.actor, webapp_onboarding.ASSISTANT_ORIGIN,
        dict(payload))

    assert _seam_state(browser_op) == _seam_state(assistant_op), (
        "the assistant and browser choose paths diverged: "
        f"{_seam_state(browser_op)} vs {_seam_state(assistant_op)}")
    assert _seam_state(assistant_op)["choices"] == {"github": {"skip": True}}, \
        f"the choice was not recorded: {_seam_state(assistant_op)}"


@th.django_unit_test("choose_for_actor refuses domain purchase from the assistant origin")
def test_choose_for_actor_refuses_purchase(opts):
    from mojo import errors as me
    from mojo.apps.edge.services import webapp_onboarding

    operation = _seam_operation(
        opts, webapp_onboarding.ASSISTANT_ORIGIN, cursor="address")
    before = _seam_state(operation)
    for choice in ({"label": "app", "purchase": 7},
                   {"label": "app", "confirm_token": "one-use-token"}):
        error = None
        try:
            webapp_onboarding.choose_for_actor(
                operation, opts.actor, webapp_onboarding.ASSISTANT_ORIGIN,
                {"revision": operation.revision, "step": "address",
                 "choice": choice})
        except me.PermissionDeniedException as exc:
            error = exc
        assert error is not None, \
            f"the assistant origin reached the money path with {choice}"
        assert "purchase is not available" in str(error), \
            f"the purchase refusal changed wording: {error}"
    assert _seam_state(operation) == before, \
        "a refused purchase choice still mutated the operation"


@th.django_unit_test("cancel_for_actor cancels and preserves committed resources")
def test_cancel_for_actor_matches_browser_cancel(opts):
    from mojo.apps.edge.models import WebApp
    from mojo.apps.edge.services import webapp_onboarding

    operation = _seam_operation(opts, webapp_onboarding.ASSISTANT_ORIGIN)
    cancelled = webapp_onboarding.cancel_for_actor(
        operation, opts.actor, webapp_onboarding.ASSISTANT_ORIGIN)

    assert cancelled.status == "cancelled", \
        f"cancel_for_actor did not cancel the operation: {cancelled.status}"
    assert WebApp.objects.filter(pk=operation.web_app_id).exists(), \
        "cancelling deleted the committed recoverable WebApp"


@th.django_unit_test("webapp_lifecycle.take_offline drops the address and every alias")
def test_lifecycle_take_offline_parity(opts):
    from mojo.apps.edge.models import Vhost, WebApp
    from mojo.apps.edge.services import webapp_lifecycle

    domain = make_domain(group=opts.group)
    certificate = make_certificate(domain)
    primary = make_vhost(domain, certificate, label=f"off{uuid.uuid4().hex[:6]}")
    site = make_webapp(opts.group, slug=f"off{uuid.uuid4().hex[:6]}", vhost=primary)
    alias = make_vhost(domain, certificate, label=f"al{uuid.uuid4().hex[:6]}",
                       kind="site_api", alias_of=site)

    result = webapp_lifecycle.take_offline(site)

    assert result == {"webapp": site.pk, "address": None}, \
        f"take_offline changed the REST detach payload: {result}"
    assert WebApp.objects.filter(pk=site.pk).exists(), \
        "take_offline deleted the whole app"
    site.refresh_from_db()
    assert site.vhost_id is None, "take_offline left the app linked to its vhost"
    assert not Vhost.objects.filter(pk=primary.pk).exists(), \
        "take_offline left the primary address serving"
    assert not Vhost.objects.filter(pk=alias.pk).exists(), \
        "take_offline left a custom domain serving after going offline"


@th.django_unit_test("webapp_lifecycle.teardown removes the app, key, and every address")
def test_lifecycle_teardown_parity(opts):
    from mojo.apps.account.models import ApiKey
    from mojo.apps.edge.models import Vhost, WebApp
    from mojo.apps.edge.services import webapp_keys, webapp_lifecycle

    domain = make_domain(group=opts.group)
    certificate = make_certificate(domain)
    primary = make_vhost(domain, certificate, label=f"del{uuid.uuid4().hex[:6]}",
                         kind="site_api")
    site = make_webapp(opts.group, slug=f"del{uuid.uuid4().hex[:6]}", vhost=primary)
    alias = make_vhost(domain, certificate, label=f"dal{uuid.uuid4().hex[:6]}",
                       kind="site_api", alias_of=site)
    _, key, _, _ = webapp_keys.link(site)
    site.refresh_from_db()

    result = webapp_lifecycle.teardown(site)

    assert result == {"webapp": site.pk, "deleted": True}, \
        f"teardown did not report the deletion: {result}"
    assert not WebApp.objects.filter(pk=site.pk).exists(), \
        "teardown left the app row behind"
    assert not Vhost.objects.filter(pk__in=[primary.pk, alias.pk]).exists(), \
        "teardown orphaned a serving address"
    assert ApiKey.objects.get(pk=key.pk).is_active is False, \
        "teardown left the CI deploy key active"
