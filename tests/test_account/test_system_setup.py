"""Protected configuration, readiness, and durable System Setup operations."""

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


@th.django_unit_test("protected keys are registered as one explicit allowlist")
def test_protected_key_registry(opts):
    from mojo.apps.account.services import system_settings
    expected = {
        "BASE_URL", "MOJO_INSTALLATION_UUID", "MOJO_INSTALLATION_SLUG",
        "AWS_CLOUDWATCH_ALARM_TOPIC_ARNS", "EDGE_EXPECTED_TOPOLOGY"}
    assert set(system_settings.protected_keys()) == expected, \
        f"protected key registry drifted: {system_settings.protected_keys()!r}"


@th.django_unit_test("generic Setting.set cannot write protected configuration")
def test_setting_set_refuses_protected_key(opts):
    from mojo.apps.account.models import Setting
    from mojo import errors as merrors
    with th.assert_raises(merrors.PermissionDeniedException):
        Setting.set("BASE_URL", "https://example.com")


@th.django_unit_test("a caller-forged model flag cannot bypass protected writes")
def test_forged_model_flag_does_not_bypass(opts):
    from mojo.apps.account.models import Setting
    from mojo import errors as merrors
    row = Setting(key="BASE_URL", value="https://example.com")
    row._system_setting_write = True
    with th.assert_raises(merrors.PermissionDeniedException):
        row.save()


@th.django_unit_test("renaming a generic row into a protected key is refused")
def test_key_rename_into_protected_refused(opts):
    from mojo.apps.account.models import Setting
    from mojo import errors as merrors
    row = Setting.objects.create(key="SYSTEM_SETUP_TEST_GENERIC", value="ok")
    row.key = "BASE_URL"
    with th.assert_raises(merrors.PermissionDeniedException):
        row.save()
    Setting.objects.filter(pk=row.pk).delete()


@th.django_unit_test("dedicated protected setter requires a literal active superuser")
def test_protected_setter_requires_superuser(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import system_settings
    from mojo import errors as merrors
    regular = User.objects.get(pk=opts.system_setup_regular_id)
    with th.assert_raises(merrors.PermissionDeniedException):
        system_settings.set_value(regular, "BASE_URL", "https://example.com")


@th.django_unit_test("dedicated protected setter persists canonical BASE_URL")
def test_protected_setter_persists(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import system_settings
    admin = User.objects.get(pk=opts.system_setup_admin_id)
    value = system_settings.set_value(admin, "BASE_URL", "https://EXAMPLE.com:443/")
    assert value == "https://example.com", f"BASE_URL was not canonicalized: {value}"
    assert system_settings.get_value("BASE_URL") == value, \
        "protected BASE_URL did not round-trip through database storage"


@th.django_unit_test("BASE_URL rejects unsafe and non-origin values")
def test_base_url_validation(opts):
    from mojo.apps.account.services import system_settings
    bad = [
        "http://example.com", "https://localhost", "https://127.0.0.1",
        "https://example.com/path", "https://user@example.com",
        "https://example.com?x=1", "https://bad_host.example.com",
        "https://example.com:invalid"]
    for value in bad:
        try:
            system_settings.validate_base_url(value)
            raise AssertionError(f"unsafe BASE_URL was accepted: {value}")
        except ValueError:
            pass


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


@th.django_unit_test("readiness reports use the stable versioned status schema")
def test_readiness_schema(opts):
    from mojo.apps.account.services import system_readiness
    report = system_readiness.run("django", {
        "local_url": f"{opts.client.host}api/version", "retries": 1,
        "timeout": 5, "probe_public": False})
    assert report["schema_version"] == 1, f"unexpected schema version: {report!r}"
    assert report["overall"] in ("pass", "warn", "fail", "pending"), \
        f"invalid overall readiness status: {report!r}"
    assert report["sections"][0]["code"] == "django", \
        f"stable django section code missing: {report!r}"
    for check in report["sections"][0]["checks"]:
        assert check["status"] in ("pass", "warn", "fail", "pending"), \
            f"invalid readiness row status: {check!r}"
        assert check["explanation"], f"readiness row lacks explanation: {check!r}"


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


@th.django_unit_test("replay key returns the same durable operation")
def test_operation_replay(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import system_setup
    request = _request(User.objects.get(pk=opts.system_setup_admin_id))
    first, replayed = system_setup.create(request, "check", replay_key="same-request")
    second, replayed_again = system_setup.create(request, "check", replay_key="same-request")
    assert replayed is False and replayed_again is True, \
        f"operation replay flags were wrong: {replayed}, {replayed_again}"
    assert first.pk == second.pk, f"replay created a duplicate operation: {first.pk}, {second.pk}"


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


@th.django_unit_test("mutable operations stay bound to their bootstrap Origin")
def test_operation_origin_binding(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import system_setup
    from mojo import errors as merrors
    admin = User.objects.get(pk=opts.system_setup_admin_id)
    operation, _ = system_setup.create(
        _request(admin), "check", replay_key="origin-bound")
    with th.assert_raises(merrors.PermissionDeniedException):
        system_setup.advance(
            _request(admin, origin="http://different.test"), operation.pk)


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


@th.django_unit_test("operation cancellation is terminal and repeatable")
def test_cancel_operation(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import system_setup
    request = _request(User.objects.get(pk=opts.system_setup_admin_id))
    operation, _ = system_setup.create(request, "check", replay_key="cancel-me")
    operation = system_setup.cancel(request, operation.pk)
    again = system_setup.cancel(request, operation.pk)
    assert operation.status == "cancelled" and again.status == "cancelled", \
        f"cancel was not idempotent: {operation.status}, {again.status}"


@th.django_unit_test("regular users cannot call System Setup endpoints")
def test_setup_api_requires_superuser(opts):
    assert opts.client.login(REGULAR_EMAIL, REGULAR_PASSWORD), "regular-user login failed"
    response = opts.client.get("/api/account/admin/setup/options")
    assert response.status_code == 403, \
        f"regular user reached System Setup options: {response.status_code}"


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


@th.django_unit_test("inactive superusers are rejected by the dedicated writer")
def test_inactive_superuser_rejected(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import system_settings
    from mojo import errors as merrors
    actor = User.objects.get(pk=opts.system_setup_admin_id)
    actor.is_active = False
    actor.save(update_fields=["is_active", "modified"])
    with th.assert_raises(merrors.PermissionDeniedException):
        system_settings.set_value(actor, "BASE_URL", "https://inactive.example.com")
    actor.is_active = True
    actor.save(update_fields=["is_active", "modified"])


@th.django_unit_test("key-backed sessions fail the System Setup service gate")
def test_key_backed_setup_request_rejected(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import system_setup
    from mojo import errors as merrors
    request = _request(User.objects.get(pk=opts.system_setup_admin_id))
    request.api_key = object()
    with th.assert_raises(merrors.PermissionDeniedException):
        system_setup.require_request_admin(request)


@th.django_unit_test("mutable Setup endpoints declare the fixed 600-second freshness gate")
def test_mutation_endpoints_require_fixed_freshness(opts):
    import importlib
    setup_rest = importlib.import_module("mojo.apps.account.rest.system_setup")
    endpoints = [
        setup_rest.on_setup_create, setup_rest.on_setup_advance, setup_rest.on_setup_choose,
        setup_rest.on_setup_cancel]
    for endpoint in endpoints:
        assert getattr(endpoint, "_mojo_fresh_auth_seconds", None) == 600, \
            f"{endpoint.__name__} lost the fixed 600-second freshness gate"


@th.django_unit_test("resumed mutations reconcile and never call their fixer twice")
def test_reconcile_before_retry(opts):
    from mojo.apps.account.models import SystemSetupOperation, User
    from mojo.apps.account.services import system_readiness, system_setup
    SystemSetupOperation.objects.all().delete()
    calls = {"fix": 0, "reconcile": 0}

    def check(context):
        return system_readiness.result(
            "test.reconcile", "pass", "Test resource is present.")

    def fix(context, choice):
        calls["fix"] += 1

    def reconcile(context, choice):
        calls["reconcile"] += 1
        return {"status": "proven"}

    system_readiness.register_section(
        "test_reconcile", "Test reconcile", check,
        fix=fix, reconcile=reconcile, order=999)
    request = _request(User.objects.get(pk=opts.system_setup_admin_id))
    operation, _ = system_setup.create(
        request, "fix", section="test_reconcile", replay_key="reconcile-once")
    operation = system_setup.advance(request, operation.pk)
    assert operation.status == "reconciling", \
        f"fix did not persist reconciliation state: {operation.status}"
    operation = system_setup.advance(request, operation.pk)
    assert calls == {"fix": 1, "reconcile": 1}, \
        f"resumed mutation retried or skipped reconciliation: {calls!r}"
    assert operation.cursor == 1, f"proven reconciliation did not advance: {operation.cursor}"


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


@th.django_unit_test("local readiness probe ignores hostile Host destinations")
def test_local_probe_uses_trusted_loopback(opts):
    from django.test import RequestFactory
    from mojo.apps.account.services import system_readiness
    request = RequestFactory().get(
        "/api/account/admin/setup/readiness", HTTP_HOST="169.254.169.254",
        SERVER_PORT="9123")
    url = system_readiness.trusted_local_api_url(request)
    assert url == "http://127.0.0.1:9123/api/version", \
        f"local probe accepted Host-derived destination: {url}"


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
    with mock.patch.object(system_settings.settings, "get_static", return_value="first-name"):
        frozen = system_settings.installation_identity(actor)
    with mock.patch.object(system_settings.settings, "get_static", return_value="second-name"):
        reread = system_settings.installation_identity(actor)
    assert reread == frozen, \
        f"static monitoring-name change invalidated frozen identity: {frozen!r} -> {reread!r}"


@th.django_unit_test("one sanitizer redacts direct and durable payloads with hard bounds")
def test_setup_sanitizer_all_boundaries(opts):
    import json
    from mojo import errors as merrors
    from mojo.apps.account.models import SystemSetupOperation, User
    from mojo.apps.account.services import setup_safety, system_readiness, system_setup
    secret = "AKIAIOSFODNN7EXAMPLE"
    secret_access = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

    def check(context):
        return system_readiness.result(
            "test.safe", "warn", f"credential={secret}",
            details={"ordinary": f"https://user:pass@example.com/file?X-Amz-Signature={secret}",
                     "value": secret_access, "large": "x" * 100000})

    system_readiness.register_section("test_safety", "Test safety", check, order=998)
    direct = system_readiness.run("test_safety", {})
    encoded = json.dumps(direct)
    assert (secret not in encoded and secret_access not in encoded and
            "user:pass" not in encoded and "X-Amz-Signature" not in encoded), \
        f"direct readiness response leaked secret material: {encoded[:500]}"
    assert len(encoded.encode("utf-8")) <= setup_safety.MAX_SERIALIZED_BYTES, \
        f"direct readiness response exceeded byte cap: {len(encoded)}"

    SystemSetupOperation.objects.all().delete()
    request = _request(User.objects.get(pk=opts.system_setup_admin_id))
    operation, _ = system_setup.create(
        request, "check", section="test_safety", replay_key="safe-persist")
    operation = system_setup.advance(request, operation.pk)
    system_setup._log(operation, "provider.note", f"authorization=Bearer {secret}")
    operation.save(update_fields=["operation_log", "modified"])
    persisted_log = json.dumps(operation.operation_log)
    assert secret not in persisted_log, \
        f"persisted setup log leaked secret material: {persisted_log}"
    serialized = json.dumps(system_setup.serialize(operation))
    assert secret not in serialized and "user:pass" not in serialized, \
        f"persisted/serialized setup payload leaked secret material: {serialized[:500]}"
    assert len(serialized.encode("utf-8")) <= setup_safety.MAX_SERIALIZED_BYTES, \
        f"serialized setup operation exceeded byte cap: {len(serialized)}"
    with th.assert_raises(merrors.ValueException):
        system_setup._validate_choice({
            "type": "object", "properties": {"token": {"type": "string"}},
            "required": ["token"], "additionalProperties": False,
        }, {"token": "innocent-looking-value"})
    huge = setup_safety.sanitize({"value": "A" * 10_000_000})
    assert huge["value"] == setup_safety.TRUNCATED, \
        f"huge input was processed instead of pre-bounded: {str(huge)[:100]}"
    long_opaque = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/" * 3
    opaque = setup_safety.sanitize({"value": long_opaque})
    assert opaque["value"] == setup_safety.REDACTED, \
        f"129+ character opaque secret-shaped value was not redacted: {opaque}"


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


@th.django_unit_test("resumed mutation is attributed to the advancing superuser")
def test_resume_uses_current_admin_attribution(opts):
    from mojo.apps.account.models import SystemSetupOperation, User
    from mojo.apps.account.services import system_readiness, system_setup
    SystemSetupOperation.objects.all().delete()
    seen = []

    def check(context):
        return system_readiness.result("test.actor", "pass", "Actor test is ready.")

    def fix(context, choice):
        seen.append(context["actor"].pk)

    system_readiness.register_section(
        "test_actor", "Test actor", check, fix=fix,
        reconcile=lambda context, choice: True, order=997)
    creator = User.objects.get(pk=opts.system_setup_admin_id)
    operation, _ = system_setup.create(
        _request(creator), "fix", section="test_actor", replay_key="actor-resume")
    creator.is_superuser = False
    creator.save(update_fields=["is_superuser", "modified"])
    second = User.objects.create_user(
        username="second-system-admin@test.com", email="second-system-admin@test.com",
        password="Second_system_Admin_99")
    second.is_active = True
    second.is_superuser = True
    second.save(update_fields=["is_active", "is_superuser", "modified"])
    try:
        operation = system_setup.advance(_request(second), operation.pk)
        assert seen == [second.pk], f"mutation was attributed to stale creator: {seen!r}"
        assert operation.created_by_id == creator.pk, "audit creator unexpectedly changed"
    finally:
        creator.is_superuser = True
        creator.save(update_fields=["is_superuser", "modified"])
        second.delete()


@th.django_unit_test("uncertain mutation cannot be cancelled with active or expired lease")
def test_cancel_refuses_uncertain_mutation(opts):
    from datetime import timedelta
    from mojo import errors as merrors
    from mojo.apps.account.models import SystemSetupOperation, User
    from mojo.apps.account.services import system_setup
    from django.utils import timezone
    SystemSetupOperation.objects.all().delete()
    request = _request(User.objects.get(pk=opts.system_setup_admin_id))
    operation, _ = system_setup.create(request, "check", replay_key="cancel-uncertain")
    for state, expiry in (
            ("mutation_attempted", timezone.now() + timedelta(seconds=60)),
            ("reconciling", timezone.now() - timedelta(seconds=60))):
        steps = list(operation.steps)
        steps[0] = dict(steps[0], state=state)
        SystemSetupOperation.objects.filter(pk=operation.pk).update(
            steps=steps, status="reconciling", lease_owner="lease",
            lease_expires_at=expiry)
        with th.assert_raises(merrors.ValueException):
            system_setup.cancel(request, operation.pk)


@th.django_unit_test("active lease blocks safe cancellation but expired safe lease does not")
def test_cancel_obeys_lease_between_steps(opts):
    from datetime import timedelta
    from mojo import errors as merrors
    from mojo.apps.account.models import SystemSetupOperation, User
    from mojo.apps.account.services import system_setup
    from django.utils import timezone
    SystemSetupOperation.objects.all().delete()
    request = _request(User.objects.get(pk=opts.system_setup_admin_id))
    operation, _ = system_setup.create(request, "check", replay_key="cancel-lease")
    SystemSetupOperation.objects.filter(pk=operation.pk).update(
        lease_owner="lease", lease_expires_at=timezone.now() + timedelta(seconds=60))
    with th.assert_raises(merrors.ValueException):
        system_setup.cancel(request, operation.pk)
    SystemSetupOperation.objects.filter(pk=operation.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=60))
    cancelled = system_setup.cancel(request, operation.pk)
    assert cancelled.status == "cancelled", \
        f"expired lease blocked safe between-step cancellation: {cancelled.status}"


@th.django_unit_test("only all-pass proof can succeed a fix operation")
def test_fix_requires_green_final_proof(opts):
    from mojo.apps.account.models import SystemSetupOperation, User
    from mojo.apps.account.services import system_readiness, system_setup
    request = _request(User.objects.get(pk=opts.system_setup_admin_id))
    for status in ("warn", "pending", "fail"):
        code = f"test_proof_{status}"
        system_readiness.register_section(
            code, f"Proof {status}",
            lambda context, value=status: system_readiness.result(
                f"proof.{value}", value, f"Proof is {value}."), order=996)
        SystemSetupOperation.objects.all().delete()
        operation, _ = system_setup.create(
            request, "fix", section=code, replay_key=f"fix-{status}")
        operation = system_setup.advance(request, operation.pk)
        assert operation.status == "failed", \
            f"fix operation represented {status} proof as success: {operation.status}"

    SystemSetupOperation.objects.all().delete()
    operation, _ = system_setup.create(
        request, "check", section="test_proof_warn", replay_key="check-warn")
    operation = system_setup.advance(request, operation.pk)
    assert operation.status == "succeeded" and operation.report.get("overall") == "warn", \
        f"check-mode semantics were conflated with fix proof: {operation.status} {operation.report}"


@th.django_unit_test("ambiguous fixer and reconciliation errors remain reconciling")
def test_ambiguous_provider_errors_never_repeat_or_terminalize(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import SystemSetupOperation, User
    from mojo.apps.account.services import system_readiness, system_setup
    SystemSetupOperation.objects.all().delete()
    calls = {"fix": 0, "reconcile": 0}

    def check(context):
        return system_readiness.result("test.ambiguous", "pass", "Provider is ready.")

    def fix(context, choice):
        calls["fix"] += 1
        raise RuntimeError("provider secret response must not be logged")

    def reconcile(context, choice):
        calls["reconcile"] += 1
        raise ConnectionError("provider status is temporarily unavailable")

    system_readiness.register_section(
        "test_ambiguous", "Test ambiguous", check, fix=fix,
        reconcile=reconcile, order=995)
    request = _request(User.objects.get(pk=opts.system_setup_admin_id))
    operation, _ = system_setup.create(
        request, "fix", section="test_ambiguous", replay_key="ambiguous")
    operation = system_setup.advance(request, operation.pk)
    assert operation.status == "reconciling" and calls == {"fix": 1, "reconcile": 0}, \
        f"ambiguous mutation error terminalized or repeated: {operation.status} {calls}"
    with th.assert_raises(merrors.ValueException):
        system_setup.create(
            request, "fix", section="test_ambiguous", replay_key="replacement-one")
    operation = system_setup.advance(request, operation.pk)
    assert operation.status == "reconciling" and calls == {"fix": 1, "reconcile": 1}, \
        f"ambiguous reconciliation error terminalized or retried mutation: {operation.status} {calls}"
    with th.assert_raises(merrors.ValueException):
        system_setup.create(
            request, "fix", section="test_ambiguous", replay_key="replacement-two")
    encoded_log = str(operation.operation_log)
    assert "provider secret response" not in encoded_log and "temporarily unavailable" not in encoded_log, \
        f"ambiguous exception message leaked into operation log: {encoded_log}"
    assert "RuntimeError" in encoded_log and "ConnectionError" in encoded_log, \
        f"safe exception class evidence was not logged: {encoded_log}"


@th.django_unit_test("stale uncertain definition reconciles through its versioned adapter")
def test_stale_uncertain_step_uses_versioned_adapter(opts):
    from mojo.apps.account.models import SystemSetupOperation, User
    from mojo.apps.account.services import system_readiness, system_setup
    SystemSetupOperation.objects.all().delete()
    calls = {"fix": 0, "adapter": 0}

    def check(context):
        return system_readiness.result("test.adapter", "pass", "Adapter resource exists.")

    def fix(context, choice):
        calls["fix"] += 1

    def old_reconcile(context, choice):
        calls["adapter"] += 1
        return {"status": "proven"}

    system_readiness.register_section(
        "test_adapter", "Test adapter", check, fix=fix,
        reconcile=lambda context, choice: True, definition_version=2,
        reconciliation_adapters={1: old_reconcile}, order=994)
    request = _request(User.objects.get(pk=opts.system_setup_admin_id))
    operation, _ = system_setup.create(
        request, "fix", section="test_adapter", replay_key="old-adapter")
    steps = list(operation.steps)
    steps[0] = dict(steps[0], definition_version=1, state="mutation_attempted")
    SystemSetupOperation.objects.filter(pk=operation.pk).update(
        steps=steps, status="reconciling")
    operation = system_setup.advance(request, operation.pk)
    assert operation.cursor == 1 and operation.status == "planned", \
        f"old uncertain step did not escape through adapter: {operation.cursor}/{operation.status}"
    assert calls == {"fix": 0, "adapter": 1}, \
        f"old adapter path repeated mutation or skipped reconciliation: {calls}"


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


@th.django_unit_test("advance rejects active lease and safely resumes an expired lease")
def test_advance_lease_guard_and_resume(opts):
    from datetime import timedelta
    from django.utils import timezone
    from mojo import errors as merrors
    from mojo.apps.account.models import SystemSetupOperation, User
    from mojo.apps.account.services import system_readiness, system_setup
    SystemSetupOperation.objects.all().delete()
    system_readiness.register_section(
        "test_advance_lease", "Test advance lease",
        lambda context: system_readiness.result(
            "test.advance_lease", "pass", "Lease test is ready."), order=993)
    request = _request(User.objects.get(pk=opts.system_setup_admin_id))
    operation, _ = system_setup.create(
        request, "check", section="test_advance_lease", replay_key="advance-lease")
    SystemSetupOperation.objects.filter(pk=operation.pk).update(
        lease_owner="live", lease_expires_at=timezone.now() + timedelta(seconds=60))
    with th.assert_raises(merrors.ValueException):
        system_setup.advance(request, operation.pk)
    SystemSetupOperation.objects.filter(pk=operation.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=60))
    resumed = system_setup.advance(request, operation.pk)
    assert resumed.status == "succeeded", \
        f"expired lease did not resume safely: {resumed.status}"


@th.django_unit_test("operation log retains only the newest two hundred entries")
def test_operation_log_prunes_to_two_hundred(opts):
    from mojo.apps.account.models import SystemSetupOperation, User
    from mojo.apps.account.services import system_setup
    SystemSetupOperation.objects.all().delete()
    request = _request(User.objects.get(pk=opts.system_setup_admin_id))
    operation, _ = system_setup.create(request, "check", replay_key="log-prune")
    for index in range(250):
        system_setup._log(operation, f"event.{index}", f"Event {index}")
    operation.save(update_fields=["operation_log", "modified"])
    operation.refresh_from_db()
    assert len(operation.operation_log) == 200, \
        f"operation log retained {len(operation.operation_log)} entries instead of 200"
    assert operation.operation_log[0]["code"] == "event.50", \
        f"operation log pruned the wrong edge: {operation.operation_log[0]}"
