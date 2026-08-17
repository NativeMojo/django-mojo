"""WebApp onboarding durability, tenancy, and secret-boundary regressions."""

import socket
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools, declare_release_buckets, login, make_certificate,
    make_domain, make_group, make_group_member, make_user, make_vhost,
    make_webapp, with_setting,
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
    assert any(rec["name"] == f"app.{domain.name}" and rec["value"] == TARGET
               for rec in r["records"]), \
        f"the app CNAME to publish was not in the records list: {r}"


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
