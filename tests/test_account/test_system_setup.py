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
        request, operation.pk, step["id"], step["version"],
        {"base_url": "https://setup.example.com"})
    assert operation.status == "planned", f"accepted choice did not resume operation: {operation.status}"
    assert operation.steps[operation.cursor]["version"] == step["version"] + 1, \
        "choice did not bump step version to invalidate stale submissions"


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
            request, operation.pk, "wrong-step", step["version"],
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
    module = opts.client.get("/admin/assets/setup.js")
    assert shell.status_code == 200 and module.status_code == 200, \
        f"authenticated browser could not load System Setup source: {shell.status_code}/{module.status_code}"

    origin = opts.client.host.rstrip("/")
    headers = {"Origin": origin}
    created = opts.client.post(
        "/api/account/admin/setup/create",
        json={"mode": "fix", "replay_key": "browser-resume-smoke"}, headers=headers)
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
              "step_version": step.version,
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
