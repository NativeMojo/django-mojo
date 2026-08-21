"""Protected-configuration writes and probing System Setup matrices.

Moved from tests/test_account/test_system_setup.py (maestro item #1839):
these tests write protected Setting rows (BASE_URL, installation identity,
edge topology), mutate django.conf.settings, patch shared production
modules, or POST to the generic settings REST API — all unsafe under the
parallel default tier. The refused-write guards and durable-operation
contracts remain in the default-tier module.
"""

from concurrent.futures import ThreadPoolExecutor

from testit import helpers as th


ADMIN_EMAIL = "system-setup-admin@test.com"
ADMIN_PASSWORD = "System_setup_Admin_99"
REGULAR_EMAIL = "system-setup-regular@test.com"
REGULAR_PASSWORD = "System_setup_Regular_99"


@th.django_unit_setup()
def setup_system_setup(opts):
    from mojo.apps.account.models import Setting, SystemSetupOperation, User
    from mojo.apps.account.services import system_settings

    SystemSetupOperation.objects.all().delete()
    Setting.objects.filter(key__in=system_settings.protected_keys()).delete()
    User.objects.filter(email__in=[ADMIN_EMAIL, REGULAR_EMAIL]).delete()
    admin = User.objects.create_user(
        username=ADMIN_EMAIL, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)
    admin.is_active = True
    admin.is_email_verified = True
    admin.requires_mfa = False
    admin.is_superuser = True
    admin.save()
    regular = User.objects.create_user(
        username=REGULAR_EMAIL, email=REGULAR_EMAIL, password=REGULAR_PASSWORD)
    regular.is_active = True
    regular.is_email_verified = True
    regular.requires_mfa = False
    regular.save()
    opts.system_setup_admin_id = admin.pk
    opts.system_setup_regular_id = regular.pk


def _request(user, origin="http://testserver"):
    from django.test import RequestFactory
    request = RequestFactory().post(
        "/api/account/admin/setup/create", HTTP_ORIGIN=origin,
        HTTP_HOST=origin.split("//", 1)[1])
    request.user = user
    request.bearer = "bearer"
    request.api_key = None
    return request


@th.django_unit_test("renaming a generic row into a protected key is refused")
def test_key_rename_into_protected_refused(opts):
    from mojo.apps.account.models import Setting
    from mojo import errors as merrors
    row = Setting.objects.create(key="SYSTEM_SETUP_TEST_GENERIC", value="ok")
    row.key = "BASE_URL"
    with th.assert_raises(merrors.PermissionDeniedException):
        row.save()
    Setting.objects.filter(pk=row.pk).delete()


@th.django_unit_test("dedicated protected setter persists canonical BASE_URL")
def test_protected_setter_persists(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import system_settings
    admin = User.objects.get(pk=opts.system_setup_admin_id)
    value = system_settings.set_value(admin, "BASE_URL", "https://EXAMPLE.com:443/")
    assert value == "https://example.com", f"BASE_URL was not canonicalized: {value}"
    assert system_settings.get_value("BASE_URL") == value, \
        "protected BASE_URL did not round-trip through database storage"


@th.django_unit_test("installation ownership is immutable across BASE_URL changes")
def test_installation_identity_ignores_base_url(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import system_settings
    admin = User.objects.get(pk=opts.system_setup_admin_id)
    first = system_settings.installation_identity(admin)
    system_settings.set_value(admin, "BASE_URL", "https://first.example.com")
    system_settings.set_value(admin, "BASE_URL", "https://second.example.com")
    second = system_settings.installation_identity(admin)
    assert first == second, f"BASE_URL correction changed ownership identity: {first} != {second}"


@th.django_unit_test("concurrent installation freezes produce one UUID/slug pair")
def test_installation_identity_concurrent(opts):
    from django.db import close_old_connections
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import system_settings
    Setting.objects.filter(key__in=(
        system_settings.INSTALLATION_UUID,
        system_settings.INSTALLATION_SLUG)).delete()

    def freeze():
        close_old_connections()
        actor = User.objects.get(pk=opts.system_setup_admin_id)
        try:
            return system_settings.installation_identity(actor)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(lambda _: freeze(), range(2)))
    assert values[0] == values[1], f"concurrent freezes diverged: {values!r}"


@th.django_unit_test("fix operation waits for a typed late BASE_URL choice")
def test_fix_operation_late_choice(opts):
    from mojo.apps.account.models import Setting, SystemSetupOperation, User
    from mojo.apps.account.services import system_settings, system_setup
    SystemSetupOperation.objects.all().delete()
    Setting.objects.filter(key__in=system_settings.protected_keys()).delete()
    admin = User.objects.get(pk=opts.system_setup_admin_id)
    request = _request(admin)
    operation, replayed = system_setup.create(request, "fix", replay_key="late-choice")
    assert replayed is False, "new operation was incorrectly reported as replayed"
    operation = system_setup.advance(request, operation.pk)
    assert operation.status == "reconciling", f"identity mutation did not reconcile: {operation.status}"
    operation = system_setup.advance(request, operation.pk)
    assert operation.cursor == 1, f"identity reconciliation did not advance: {operation.cursor}"
    operation = system_setup.advance(request, operation.pk)
    assert operation.status == "waiting_for_choice", \
        f"missing BASE_URL did not pause for choice: {operation.status}"
    step = operation.steps[operation.cursor]
    operation = system_setup.choose(
        request, operation.pk, step["id"], step["definition_version"],
        step["choice_revision"],
        {"base_url": "https://setup.example.com"})
    assert operation.status == "planned", f"accepted choice did not resume operation: {operation.status}"
    assert operation.steps[operation.cursor]["choice_revision"] == step["choice_revision"] + 1, \
        "choice did not bump choice revision to invalidate stale submissions"


@th.django_unit_test("stale and cross-step choices are rejected under row lock")
def test_stale_choice_rejected(opts):
    from mojo.apps.account.models import Setting, SystemSetupOperation, User
    from mojo.apps.account.services import system_settings, system_setup
    from mojo import errors as merrors
    SystemSetupOperation.objects.all().delete()
    Setting.objects.filter(key__in=system_settings.protected_keys()).delete()
    request = _request(User.objects.get(pk=opts.system_setup_admin_id))
    operation, _ = system_setup.create(request, "fix", replay_key="stale-choice")
    operation = system_setup.advance(request, operation.pk)
    operation = system_setup.advance(request, operation.pk)
    operation = system_setup.advance(request, operation.pk)
    step = operation.steps[operation.cursor]
    with th.assert_raises(merrors.ValueException):
        system_setup.choose(
            request, operation.pk, "wrong-step", step["definition_version"],
            step["choice_revision"],
            {"base_url": "https://setup.example.com"})


@th.django_unit_test("only one fix operation may remain active")
def test_single_active_fix(opts):
    from mojo.apps.account.models import Setting, SystemSetupOperation, User
    from mojo.apps.account.services import system_settings, system_setup
    from mojo import errors as merrors
    SystemSetupOperation.objects.all().delete()
    Setting.objects.filter(key__in=system_settings.protected_keys()).delete()
    request = _request(User.objects.get(pk=opts.system_setup_admin_id))
    system_setup.create(request, "fix", replay_key="first-fix")
    with th.assert_raises(merrors.ValueException):
        system_setup.create(request, "fix", replay_key="second-fix")


@th.django_unit_test("check operation stores a bounded final readiness rerun")
def test_check_operation_final_report(opts):
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import system_settings, system_setup
    Setting.objects.filter(key=system_settings.BASE_URL, group=None).delete()
    origin = opts.client.host.rstrip("/")
    operation, _ = system_setup.create(
        _request(User.objects.get(pk=opts.system_setup_admin_id), origin=origin),
        "check", replay_key="check-report")
    operation = system_setup.advance(
        _request(User.objects.get(pk=opts.system_setup_admin_id), origin=origin),
        operation.pk)
    assert operation.status == "succeeded", f"check operation did not complete: {operation.status}"
    assert operation.report.get("schema_version") == 1, \
        f"check operation did not persist the normalized report: {operation.report!r}"
    assert len(operation.operation_log) <= 200, \
        f"operation log exceeded its bound: {len(operation.operation_log)}"


@th.django_unit_test("superuser can create and resume a real setup API operation")
def test_setup_api_authenticated_flow(opts):
    from mojo.apps.account.models import Setting, SystemSetupOperation
    from mojo.apps.account.services import system_settings
    SystemSetupOperation.objects.all().delete()
    Setting.objects.filter(key=system_settings.BASE_URL, group=None).delete()
    assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), "System Setup admin login failed"
    origin = opts.client.host.rstrip("/")
    headers = {"Origin": origin}
    created = opts.client.post(
        "/api/account/admin/setup/create",
        json={"mode": "check", "replay_key": "api-check"}, headers=headers)
    assert created.status_code == 200, f"setup create failed: {created.body}"
    operation = created.response.data
    detail = opts.client.get(
        f"/api/account/admin/setup/detail?operation={operation.id}")
    assert detail.status_code == 200, f"setup detail failed: {detail.body}"
    advanced = opts.client.post(
        "/api/account/admin/setup/advance",
        json={"operation": operation.id}, headers=headers)
    assert advanced.status_code == 200, f"setup advance failed: {advanced.body}"
    assert advanced.response.data.status == "succeeded", \
        f"real setup check did not finish: {advanced.response.data}"


@th.django_unit_test("generic Setting REST refuses protected-key creation even for superuser")
def test_generic_setting_rest_refuses_protected(opts):
    assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), "System Setup admin login failed"
    response = opts.client.post(
        "/api/settings", json={"key": "BASE_URL", "value": "https://evil.example.com"})
    assert response.status_code == 403, \
        f"generic Setting REST wrote protected BASE_URL: {response.status_code} {response.body}"


@th.django_unit_test("protected rows cannot be deleted through the model")
def test_protected_row_delete_refused(opts):
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import system_settings
    from mojo import errors as merrors
    actor = User.objects.get(pk=opts.system_setup_admin_id)
    system_settings.set_value(actor, "BASE_URL", "https://delete.example.com")
    row = Setting.objects.get(key="BASE_URL", group=None)
    with th.assert_raises(merrors.PermissionDeniedException):
        row.delete()
    with th.assert_raises(merrors.PermissionDeniedException):
        Setting.remove("BASE_URL")
    assert Setting.objects.filter(pk=row.pk).exists(), \
        "denied protected removal deleted the database row"


@th.django_unit_test("generic REST cannot update or delete an existing protected row")
def test_generic_setting_rest_refuses_update_delete(opts):
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import system_settings
    actor = User.objects.get(pk=opts.system_setup_admin_id)
    system_settings.set_value(actor, "BASE_URL", "https://protected.example.com")
    row = Setting.objects.get(key="BASE_URL", group=None)
    assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), "System Setup admin login failed"
    updated = opts.client.post(
        f"/api/settings/{row.pk}", json={"value": "https://changed.example.com"})
    deleted = opts.client.delete(f"/api/settings/{row.pk}")
    assert updated.status_code == 403, \
        f"generic REST updated protected row: {updated.status_code} {updated.body}"
    assert deleted.status_code == 403, \
        f"generic REST deleted protected row: {deleted.status_code} {deleted.body}"


@th.django_unit_test("expected edge topology is validated and round-trips as JSON")
def test_expected_topology_contract(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import system_settings
    actor = User.objects.get(pk=opts.system_setup_admin_id)
    topology = system_settings.set_value(actor, system_settings.EXPECTED_EDGE_TOPOLOGY, {
        "nodes": ["node-b", "node-a", "node-a"], "pools": ["www"]})
    assert topology == {"nodes": ["node-a", "node-b"], "pools": ["www"]}, \
        f"topology was not normalized: {topology!r}"
    assert system_settings.get_value(system_settings.EXPECTED_EDGE_TOPOLOGY) == topology, \
        "structured protected setting did not deserialize on read"


@th.django_unit_test("authenticated Admin source supports late choice, reload, and final rerun")
def test_authenticated_admin_setup_resume_smoke(opts):
    from mojo.apps.account.models import Setting, SystemSetupOperation
    from mojo.apps.account.services import system_settings
    SystemSetupOperation.objects.all().delete()
    Setting.objects.filter(key__in=system_settings.protected_keys()).delete()
    assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), "System Setup admin login failed"
    source = opts.client.post("/api/account/admin/session", json={})
    assert source.status_code == 200, f"Admin source session failed: {source.body}"
    shell = opts.client.get("/admin/")
    module = opts.client.get("/admin/assets/features/platform/page.js")
    assert shell.status_code == 200 and module.status_code == 200, \
        f"authenticated browser could not load System Setup source: {shell.status_code}/{module.status_code}"

    origin = opts.client.host.rstrip("/")
    headers = {"Origin": origin}
    created = opts.client.post(
        "/api/account/admin/setup/create",
        json={"mode": "fix", "section": "django",
              "replay_key": "browser-resume-smoke"}, headers=headers)
    assert created.status_code == 200, f"browser Fix Setup create failed: {created.body}"
    operation = created.response.data
    for expected in ("reconciling", "planned", "waiting_for_choice"):
        response = opts.client.post(
            "/api/account/admin/setup/advance",
            json={"operation": operation.id}, headers=headers)
        assert response.status_code == 200, f"browser advance failed: {response.body}"
        operation = response.response.data
        assert operation.status == expected, \
            f"browser setup state mismatch, expected {expected}: {operation}"

    step = operation.current_step
    chosen = opts.client.post(
        "/api/account/admin/setup/choose",
        json={"operation": operation.id, "step_id": step.id,
              "definition_version": step.definition_version,
              "choice_revision": step.choice_revision,
              "choice": {"base_url": "https://setup.invalid"}},
        headers=headers)
    assert chosen.status_code == 200, f"browser late choice failed: {chosen.body}"
    reloaded = opts.client.get(
        f"/api/account/admin/setup/detail?operation={operation.id}")
    assert reloaded.status_code == 200 and reloaded.response.data.status == "planned", \
        f"browser reload did not resume chosen operation: {reloaded.body}"
    operation = reloaded.response.data
    for expected in ("reconciling", "planned"):
        response = opts.client.post(
            "/api/account/admin/setup/advance",
            json={"operation": operation.id}, headers=headers)
        assert response.status_code == 200, f"browser BASE_URL advance failed: {response.body}"
        operation = response.response.data
        assert operation.status == expected, \
            f"browser BASE_URL state mismatch, expected {expected}: {operation}"
    final = opts.client.post(
        "/api/account/admin/setup/advance",
        json={"operation": operation.id}, headers=headers)
    assert final.status_code == 200, f"browser final rerun failed: {final.body}"
    assert final.response.data.status in ("succeeded", "failed"), \
        f"browser final rerun did not terminate: {final.response.data}"
    assert final.response.data.report.schema_version == 1, \
        f"browser final rerun did not store readiness proof: {final.response.data.report}"


@th.django_unit_test("local readiness target preserves bounded provenance")
def test_local_probe_target_provenance(opts):
    from django.conf import settings as django_settings
    from django.test import RequestFactory
    from mojo.apps.account.services import system_readiness, system_setup

    sentinel = object()
    previous = getattr(django_settings, "SYSTEM_SETUP_LOCAL_API_URL", sentinel)
    try:
        django_settings.SYSTEM_SETUP_LOCAL_API_URL = ""
        request = RequestFactory().get("/api/account/admin/setup/readiness", SERVER_PORT="9123")
        request_target = system_readiness.trusted_local_api_target(request)
        default_target = system_readiness.trusted_local_api_target()
        django_settings.SYSTEM_SETUP_LOCAL_API_URL = "http://[::1]:8123"
        configured_target = system_readiness.trusted_local_api_target(request)
    finally:
        if previous is sentinel:
            delattr(django_settings, "SYSTEM_SETUP_LOCAL_API_URL")
        else:
            django_settings.SYSTEM_SETUP_LOCAL_API_URL = previous

    assert request_target == {
        "url": "http://127.0.0.1:9123/api/version", "source": "request_server_port"}, \
        f"request-port provenance was lost: {request_target!r}"
    assert default_target == {
        "url": "http://127.0.0.1:80/api/version", "source": "default_80"}, \
        f"default-port provenance was lost: {default_target!r}"
    assert configured_target == {
        "url": "http://[::1]:8123/api/version", "source": "configured_static"}, \
        f"static-target provenance was lost: {configured_target!r}"
    assert system_readiness.trusted_local_api_url(request).endswith("/api/version"), \
        "the existing string API no longer returns a URL"
    context = system_setup._context(object(), object(), request_target)
    assert context["local_source"] == "request_server_port" and \
        context["local_url"] == request_target["url"], \
        f"durable Setup operations dropped target provenance: {context!r}"


@th.django_unit_test("Setup omits inferred local-listener and optional static-directory noise")
def test_operator_readiness_omits_inferred_node_noise(opts):
    from unittest import mock
    from mojo.apps.account.services import system_readiness, system_settings
    from mojo.apps.edge.services import sanity

    results = [
        {"name": name, "ok": name != "local request", "detail": "ready"}
        for name, _ in sanity.CHECKS
    ]
    with mock.patch.object(sanity, "run", return_value=results), \
            mock.patch.object(
                sanity, "check_static_directories",
                side_effect=AssertionError("Setup inspected optional static paths")), \
            mock.patch.object(
                system_settings, "get_value", return_value="https://api.example.com"), \
            mock.patch.object(system_readiness, "probe_public_api", return_value=True):
        inferred = system_readiness._core_check({
            "local_url": "http://127.0.0.1:443/api/version",
            "local_source": "request_server_port",
        })
        configured = system_readiness._core_check({
            "local_url": "http://127.0.0.1:8000/api/version",
            "local_source": "configured_static",
        })

    inferred_codes = {row["code"] for row in inferred}
    assert "django.local_request" not in inferred_codes, \
        f"an inferred node-local probe still affected operator readiness: {inferred!r}"
    assert "django.static_directories" not in inferred_codes, \
        f"optional static source directories still affected operator readiness: {inferred!r}"
    configured_local = [
        row for row in configured if row["code"] == "django.local_request"]
    assert len(configured_local) == 1 and configured_local[0]["status"] == "fail", \
        f"an explicitly configured node-local target lost its diagnostic: {configured!r}"


@th.django_unit_test("public probe rejects private metadata and DNS rebinding answers")
def test_public_probe_rejects_non_global_dns(opts):
    from unittest import mock
    from mojo.apps.account.services import system_readiness

    answers = [
        (2, 1, 6, "", ("93.184.216.34", 443)),
        (2, 1, 6, "", ("169.254.169.254", 443)),
    ]
    with mock.patch.object(system_readiness.socket, "getaddrinfo", return_value=answers), \
            mock.patch.object(system_readiness, "_PinnedHTTPSConnection") as connector:
        with th.assert_raises(system_readiness.UnsafePublicProbe):
            system_readiness.probe_public_api("https://example.com")
        assert not connector.called, "DNS rebinding answer opened a public probe connection"

    metadata = [(2, 1, 6, "", ("169.254.169.254", 443))]
    with mock.patch.object(system_readiness.socket, "getaddrinfo", return_value=metadata):
        with th.assert_raises(system_readiness.UnsafePublicProbe):
            system_readiness.probe_public_api("https://metadata.example")
    private = [(2, 1, 6, "", ("10.0.0.7", 443))]
    with mock.patch.object(system_readiness.socket, "getaddrinfo", return_value=private):
        with th.assert_raises(system_readiness.UnsafePublicProbe):
            system_readiness.probe_public_api("https://private.example")


@th.django_unit_test("unsafe public pinning is reported pending without a request")
def test_unsafe_public_probe_reports_pending(opts):
    from unittest import mock
    from mojo.apps.account.models import User
    from mojo.apps.account.services import system_readiness, system_settings
    from mojo.apps.edge.services import sanity
    actor = User.objects.get(pk=opts.system_setup_admin_id)
    system_settings.set_value(actor, system_settings.BASE_URL, "https://pending.example.com")
    ready = [{"name": name, "ok": True, "detail": "ready"}
             for name, _ in sanity.CHECKS]
    with mock.patch.object(sanity, "run", return_value=ready), \
            mock.patch.object(sanity, "check_static_directories"), \
            mock.patch.object(
                system_readiness, "probe_public_api",
                side_effect=system_readiness.UnsafePublicProbe("unsafe")) as probe:
        rows = system_readiness._core_check({"local_url": "http://127.0.0.1/api/version"})
    public = [row for row in rows if row["code"] == "django.public_api"]
    assert len(public) == 1 and public[0]["status"] == "pending", \
        f"unsafe public pinning was not reported pending: {public!r}"
    assert probe.call_count == 1, f"unsafe probe path retried unexpectedly: {probe.call_count}"


@th.django_unit_test("public probe pins one address and rejects redirects")
def test_public_probe_is_pinned_and_does_not_redirect(opts):
    from unittest import mock
    from mojo.apps.account.services import system_readiness

    connection = mock.Mock()
    connection.getresponse.return_value.status = 302
    answers = [(2, 1, 6, "", ("93.184.216.34", 443))]
    with mock.patch.object(system_readiness.socket, "getaddrinfo", return_value=answers), \
            mock.patch.object(system_readiness, "_PinnedHTTPSConnection",
                              return_value=connection) as connector:
        result = system_readiness.probe_public_api("https://example.com")
    assert result is False, "redirect response was accepted as public readiness"
    connector.assert_called_once_with("example.com", 443, "93.184.216.34", 2.0)
    connection.request.assert_called_once()
    assert connection.request.call_args.args[1] == "/api/version", \
        f"public connector followed an unexpected path: {connection.request.call_args}"


@th.django_unit_test("protected first writes serialize and publish cache after commit")
def test_protected_first_write_concurrency_and_cache_commit(opts):
    from django.db import close_old_connections, transaction
    from unittest import mock
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import system_settings
    Setting.objects.filter(key=system_settings.BASE_URL, group=None).delete()

    def write(index):
        close_old_connections()
        actor = User.objects.get(pk=opts.system_setup_admin_id)
        try:
            return system_settings.set_value(
                actor, system_settings.BASE_URL, f"https://write-{index}.example.com")
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, range(2)))
    count = Setting.objects.filter(key=system_settings.BASE_URL, group=None).count()
    assert count == 1, f"concurrent protected first write created {count} global rows"

    Setting.objects.filter(key=system_settings.BASE_URL, group=None).delete()
    actor = User.objects.get(pk=opts.system_setup_admin_id)
    with mock.patch.object(Setting, "push_to_cache") as publish:
        try:
            with transaction.atomic():
                system_settings.set_value(
                    actor, system_settings.BASE_URL, "https://rollback.example.com")
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass
        assert not publish.called, "rolled-back protected value was published to Redis"


@th.django_unit_test("installation identity is create-once and validates its pair")
def test_installation_identity_rejects_overwrite_and_malformed_pair(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import system_settings
    actor = User.objects.get(pk=opts.system_setup_admin_id)
    Setting.objects.filter(key__in=(
        system_settings.INSTALLATION_UUID, system_settings.INSTALLATION_SLUG)).delete()
    identity = system_settings.installation_identity(actor)
    with th.assert_raises(merrors.PermissionDeniedException):
        system_settings.set_value(actor, system_settings.INSTALLATION_UUID, identity["uuid"])
    Setting.objects.filter(key=system_settings.INSTALLATION_SLUG, group=None).delete()
    with th.assert_raises(merrors.ValueException):
        system_settings.installation_identity(actor)
    Setting.objects.filter(key__in=(
        system_settings.INSTALLATION_UUID, system_settings.INSTALLATION_SLUG)).delete()
    identity = system_settings.installation_identity(actor)
    Setting.objects.filter(
        key=system_settings.INSTALLATION_UUID, group=None).update(value="not-a-uuid")
    with th.assert_raises(merrors.ValueException):
        system_settings.installation_identity(actor)
    Setting.objects.filter(
        key=system_settings.INSTALLATION_UUID, group=None).update(value=identity["uuid"])
    Setting.objects.filter(key__in=(
        system_settings.INSTALLATION_UUID, system_settings.INSTALLATION_SLUG)).delete()
    system_settings.installation_identity(actor)


@th.django_unit_test("frozen installation identity survives static monitoring-name changes")
def test_installation_identity_ignores_later_static_name(opts):
    from unittest import mock
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import system_settings
    actor = User.objects.get(pk=opts.system_setup_admin_id)
    Setting.objects.filter(key__in=(
        system_settings.INSTALLATION_UUID, system_settings.INSTALLATION_SLUG)).delete()
    with mock.patch.object(system_settings, "_static_value", return_value="first-name"):
        frozen = system_settings.installation_identity(actor)
    with mock.patch.object(system_settings, "_static_value", return_value="second-name"):
        reread = system_settings.installation_identity(actor)
    assert reread == frozen, \
        f"static monitoring-name change invalidated frozen identity: {frozen!r} -> {reread!r}"


@th.django_unit_test("step definition version rejects stale choose and advance")
def test_step_definition_version_is_immutable(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import Setting, SystemSetupOperation, User
    from mojo.apps.account.services import system_settings, system_setup
    SystemSetupOperation.objects.all().delete()
    Setting.objects.filter(key=system_settings.BASE_URL, group=None).delete()
    request = _request(User.objects.get(pk=opts.system_setup_admin_id))
    operation, _ = system_setup.create(request, "fix", replay_key="definition-choice")
    operation = system_setup.advance(request, operation.pk)
    operation = system_setup.advance(request, operation.pk)
    operation = system_setup.advance(request, operation.pk)
    steps = list(operation.steps)
    step = dict(steps[operation.cursor])
    submitted_definition = step["definition_version"]
    step["definition_version"] += 1
    steps[operation.cursor] = step
    SystemSetupOperation.objects.filter(pk=operation.pk).update(steps=steps)
    with th.assert_raises(merrors.ValueException):
        system_setup.choose(
            request, operation.pk, step["id"], submitted_definition,
            step["choice_revision"], {"base_url": "https://setup.example.com"})
    with th.assert_raises(merrors.ValueException):
        system_setup.advance(request, operation.pk)
    cancelled = system_setup.cancel(request, operation.pk)
    assert cancelled.status == "cancelled", \
        f"planned stale definition could not be cancelled safely: {cancelled.status}"


@th.django_unit_test("stale built-in identity and BASE_URL use retained v1 adapters")
def test_stale_builtin_steps_use_read_only_v1_adapters(opts):
    from unittest import mock
    from mojo import errors as merrors
    from mojo.apps.account.models import Setting, SystemSetupOperation, User
    from mojo.apps.account.services import system_settings, system_setup
    SystemSetupOperation.objects.all().delete()
    actor = User.objects.get(pk=opts.system_setup_admin_id)
    Setting.objects.filter(key__in=(
        system_settings.INSTALLATION_UUID, system_settings.INSTALLATION_SLUG,
        system_settings.BASE_URL)).delete()
    system_settings.installation_identity(actor)
    intended_url = system_settings.set_value(
        actor, system_settings.BASE_URL, "https://retained-v1.example.com")
    request = _request(actor)
    operation, _ = system_setup.create(
        request, "fix", replay_key="retained-builtins")
    steps = list(operation.steps)
    steps[0] = dict(steps[0], definition_version=1, state="mutation_attempted")
    SystemSetupOperation.objects.filter(pk=operation.pk).update(
        steps=steps, status="reconciling")

    with mock.patch.object(system_setup, "STEP_DEFINITION_VERSION", 2), \
            mock.patch.object(system_settings, "installation_identity") as identity_mutation, \
            mock.patch.object(system_settings, "set_value") as base_url_mutation:
        with th.assert_raises(merrors.ValueException):
            system_setup.create(
                request, "fix", replay_key="blocked-before-identity-proof")
        operation = system_setup.advance(request, operation.pk)
        assert operation.cursor == 1 and operation.status == "planned", \
            f"retained identity v1 adapter did not prove the old step: {operation.cursor}/{operation.status}"
        assert not identity_mutation.called, "stale identity adapter reran the mutation initializer"

        steps = list(operation.steps)
        steps[1] = dict(steps[1], definition_version=1, state="mutation_attempted")
        SystemSetupOperation.objects.filter(pk=operation.pk).update(
            steps=steps, choices={"base_url": {"base_url": intended_url}},
            status="reconciling")
        with th.assert_raises(merrors.ValueException):
            system_setup.create(
                request, "fix", replay_key="blocked-before-base-url-proof")
        operation = system_setup.advance(request, operation.pk)
        assert operation.cursor == 2 and operation.status == "planned", \
            f"retained BASE_URL v1 adapter did not prove the old step: {operation.cursor}/{operation.status}"
        assert not base_url_mutation.called, "stale BASE_URL adapter reran the protected setter"
