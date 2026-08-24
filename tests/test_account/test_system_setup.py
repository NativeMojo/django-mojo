"""Protected-configuration guards and durable System Setup operations (default tier).

The half of the original module that is safe under the parallel default
tier: refused-write protection guards, pure validators, and durable
operations scoped to test-registered readiness sections. The tests that
actually write protected Setting rows, mutate django.conf.settings, patch
shared production modules, or POST to the settings REST API moved to
tests/test_account_admin_extended_serial/test_system_setup.py (maestro
item #1839).
"""

from testit import helpers as th

TESTIT_TIER = "admin"


ADMIN_EMAIL = "system-setup-admin@test.com"
ADMIN_PASSWORD = "System_setup_Admin_99"
REGULAR_EMAIL = "system-setup-regular@test.com"
REGULAR_PASSWORD = "System_setup_Regular_99"


@th.django_unit_setup()
def setup_system_setup(opts):
    from mojo.apps.account.models import SystemSetupOperation, User

    SystemSetupOperation.objects.all().delete()
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
        "AWS_CLOUDWATCH_ALARM_TOPIC_ARNS", "EDGE_EXPECTED_TOPOLOGY",
        "AUTH_CONFIG", "EDGE_FRAMEWORK_VERSION", "AWS_STABLE_OUTBOUND_IPS"}
    # The test project's testit_support app registers TESTIT_-prefixed
    # sentinels for the denial-contract tests (item #2558); they are reserved
    # test keys, not production roster drift.
    registered = {k for k in system_settings.protected_keys()
                  if not k.startswith("TESTIT_")}
    assert registered == expected, \
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


@th.django_unit_test("dedicated protected setter requires a literal active superuser")
def test_protected_setter_requires_superuser(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import system_settings
    from mojo import errors as merrors
    regular = User.objects.get(pk=opts.system_setup_regular_id)
    with th.assert_raises(merrors.PermissionDeniedException):
        system_settings.set_value(regular, "BASE_URL", "https://example.com")


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


@th.django_unit_test("large readiness reports preserve typed arrays when bounded")
def test_readiness_truncation_preserves_typed_arrays(opts):
    import json
    from mojo.apps.account.services import setup_safety, system_readiness

    def large_check(context):
        return [{
            "code": f"test.large.{index}",
            "status": "warn" if index == 0 else "pass",
            "explanation": f"Readiness evidence row {index}",
            "remediation": "Inspect the bounded partial report.",
        } for index in range(64)]

    system_readiness.register_section(
        "test_large_report", "Large readiness report", large_check, order=999)
    report = system_readiness.run("test_large_report", {})
    serialized = json.dumps(report)

    assert report.get("truncated") is True, \
        f"large readiness report did not announce truncation: {report!r}"
    assert report["sections"] and all(isinstance(item, dict) for item in report["sections"]), \
        f"readiness sections contain a scalar sentinel: {report['sections']!r}"
    for section in report["sections"]:
        assert section["status"] in system_readiness.STATUSES, \
            f"truncated readiness section has no supported status: {section!r}"
        assert all(isinstance(item, dict) for item in section["checks"]), \
            f"readiness checks contain a scalar sentinel: {section['checks']!r}"
        assert all(item.get("status") in system_readiness.STATUSES
                   for item in section["checks"]), \
            f"truncated readiness check has no supported status: {section['checks']!r}"
    assert len(serialized.encode("utf-8")) <= setup_safety.MAX_SERIALIZED_BYTES, \
        f"typed readiness report escaped the byte cap: {len(serialized)}"


@th.django_unit_test("bounded readiness keeps complete totals and the worst omitted result")
def test_readiness_bounded_coverage_is_truthful(opts):
    from mojo.apps.account.services import system_readiness

    def many_checks(context):
        return [system_readiness.result(
            f"test.coverage.{index}",
            "fail" if index == 69 else "pass",
            f"Coverage result {index}",
            "Repair the final result.",
            fixable=True,
        ) for index in range(70)]

    system_readiness.register_section(
        "test_bounded_coverage", "Bounded coverage", many_checks,
        fix=lambda context, choice: None, order=999)
    report = system_readiness.run("test_bounded_coverage", {})
    section = report["sections"][0]

    assert report["overall"] == "fail" and section["status"] == "fail", \
        f"the failure beyond the display budget was hidden: {report!r}"
    assert report["summary"]["pass"] == 69 and report["summary"]["fail"] == 1, \
        f"the complete result totals were not preserved: {report['summary']!r}"
    assert report["coverage"]["checks"] == {
        "total": 70,
        "returned": len(section["checks"]),
        "omitted": 70 - len(section["checks"]),
    }, f"global coverage is not explicit: {report!r}"
    assert section["coverage"] == {
        "total": 70,
        "returned": len(section["checks"]),
        "omitted": 70 - len(section["checks"]),
    }, f"section coverage is not explicit: {section!r}"
    assert report.get("truncated") is True, \
        f"partial coverage was presented as complete: {report!r}"
    assert any(check["code"] == "test.coverage.69" for check in section["checks"]), \
        f"the worst omitted result was not selected for display: {section!r}"
    assert section["fixable"] is True, \
        f"an actionable failing section was not fixable: {section!r}"


@th.django_unit_test("passing readiness findings have no repair semantics")
def test_readiness_pass_is_not_runnable(opts):
    from mojo.apps.account.services import system_readiness

    check = system_readiness.result(
        "test.already_ready", "pass", "The resource is ready.",
        "Repair this resource.", fixable=True,
        required_choice={"type": "string"})
    assert check["remediation"] == "", \
        f"a passing result retained remediation: {check!r}"
    assert check["fixable"] is False, \
        f"a passing result remained runnable: {check!r}"
    assert check["required_choice"] is None, \
        f"a passing result retained a repair choice: {check!r}"

    system_readiness.register_section(
        "test_already_ready", "Already ready", lambda context: [check],
        fix=lambda context, choice: None, order=999)
    section = system_readiness.run("test_already_ready", {})["sections"][0]
    assert section["status"] == "pass" and section["fixable"] is False, \
        f"a passing section was advertised as fixable: {section!r}"


@th.django_unit_test("Fix all builds steps only for selected actionable sections")
def test_fix_all_selected_sections(opts):
    from mojo.apps.account.services import system_setup

    aws_only = system_setup._build_steps(
        "fix", selected_sections=["aws_s3"])
    assert [step["id"] for step in aws_only] == [
        "section:aws_s3", "final_readiness"], \
        f"Fix all included a passing or unselected section: {aws_only!r}"

    django_only = system_setup._build_steps(
        "fix", selected_sections=["django"])
    assert [step["id"] for step in django_only] == [
        "installation_identity", "base_url", "final_readiness"], \
        f"the selected Django repair lost its built-in steps: {django_only!r}"

    proof_only = system_setup._build_steps("fix", selected_sections=[])
    assert [step["id"] for step in proof_only] == ["final_readiness"], \
        f"an empty actionable set still scheduled mutations: {proof_only!r}"


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


@th.django_unit_test("Setup safety preserves bounded report detail leaves")
def test_setup_safety_preserves_report_detail_leaves(opts):
    from mojo.apps.account.services import setup_safety

    report = setup_safety.sanitize({
        "schema_version": 1,
        "sections": [{
            "code": "aws_s3", "checks": [{
                "code": "aws.bucket.cors",
                "details": {"bucket": "existing-media", "rule_count": 1},
            }],
        }],
    })
    details = report["sections"][0]["checks"][0]["details"]
    assert details == {"bucket": "existing-media", "rule_count": 1}, \
        f"ordinary Setup evidence was truncated by envelope depth: {report!r}"

    too_deep = "leaf"
    for _ in range(setup_safety.MAX_DEPTH + 2):
        too_deep = {"nested": too_deep}
    assert setup_safety.TRUNCATED in str(setup_safety.sanitize(too_deep)), \
        "the Setup sanitizer no longer bounds genuinely deep structures"

    typed = setup_safety.sanitize({
        "rows": [{"status": "pass"} for _ in range(setup_safety.MAX_ITEMS)],
    })
    assert typed.get("truncated") is True, \
        f"collection truncation lacks root metadata: {typed!r}"
    assert all(isinstance(item, dict) for item in typed["rows"]), \
        f"collection truncation injected a scalar sentinel: {typed['rows']!r}"

    long_unicode = "\u754c" * 1000
    raw = long_unicode.encode("utf-8")
    legacy_expected = (
        raw[:setup_safety.MAX_STRING_BYTES].decode("utf-8", errors="ignore") +
        setup_safety.TRUNCATED
    )
    assert setup_safety._safe_string(long_unicode) == legacy_expected, \
        "the shared scalar extraction changed Setup's retained-prefix contract"


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
