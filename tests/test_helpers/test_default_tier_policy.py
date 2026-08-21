"""Default-tier isolation policy engine (testit/isolation.py).

Regression for maestro item #1839: parallel default-tier modules that patch
shared process state (the settings singleton, production module attributes,
Django settings, the environment, sys.modules) or write protected
configuration keys have corrupted unrelated modules' assertions in release
gates. The policy engine detects the enumerated mutation grammar via AST,
without importing test code.

Every fixture here is source text scanned in isolation — the engine must
never import or execute what it scans.
"""
import os
import tempfile
import textwrap

from testit import helpers as th


def _scan(source):
    from testit import isolation
    return isolation.scan_source(textwrap.dedent(source), filename="<fixture>")


def _codes(source):
    return sorted({v.code for v in _scan(source)})


# ---------------------------------------------------------------------------
# Patch-call detection
# ---------------------------------------------------------------------------

@th.unit_test("policy: string-target mock.patch of a production module attribute")
def test_patch_string_target_production(opts):
    codes = _codes("""
        from unittest import mock

        def test_thing(opts):
            with mock.patch("mojo.apps.incident.report_event"):
                pass
    """)
    assert "patch_shared" in codes, (
        f"a string-target patch of mojo.* must be a violation, got {codes}"
    )


@th.unit_test("policy: aliased patch import is still detected")
def test_patch_aliased_import(opts):
    codes = _codes("""
        from unittest.mock import patch as _p

        def test_thing(opts):
            with _p("mojo.apps.jobs.publish"):
                pass
    """)
    assert "patch_shared" in codes, (
        f"an import-aliased patch of mojo.* must be a violation, got {codes}"
    )


@th.unit_test("policy: patch.object of an imported shared singleton")
def test_patch_object_shared_singleton(opts):
    codes = _codes("""
        from unittest import mock
        from mojo.helpers.settings import settings

        def test_thing(opts):
            with mock.patch.object(settings, "get", return_value=None):
                pass
    """)
    assert "patch_shared" in codes, (
        f"patch.object on the shared settings singleton must be a violation, got {codes}"
    )


@th.unit_test("policy: patch.object of an imported production module")
def test_patch_object_production_module(opts):
    codes = _codes("""
        from unittest import mock
        from mojo.apps.account.services import provider_setup

        def test_thing(opts):
            with mock.patch.object(provider_setup, "_static", return_value=None):
                pass
    """)
    assert "patch_shared" in codes, (
        f"patch.object on an imported production module must be a violation, got {codes}"
    )


@th.unit_test("policy: patch.object of a locally constructed instance is allowed")
def test_patch_object_local_instance_allowed(opts):
    codes = _codes("""
        from unittest import mock
        from mojo.helpers.settings.helper import SettingsHelper

        def test_thing(opts):
            inst = SettingsHelper()
            with mock.patch.object(inst, "get", return_value=None):
                pass
    """)
    assert "patch_shared" not in codes and "patch_unresolved" not in codes, (
        f"patching a locally constructed instance must be allowed, got {codes}"
    )


@th.unit_test("policy: helper-returned patch target is an unresolved violation")
def test_patch_object_helper_returned(opts):
    codes = _codes("""
        from unittest import mock
        from tests.support import get_target

        def test_thing(opts):
            target = get_target()
            with mock.patch.object(target, "attr"):
                pass
    """)
    assert "patch_unresolved" in codes, (
        f"a helper-returned patch target has unresolvable provenance and must be "
        f"a violation with a diagnostic, got {codes}"
    )


@th.unit_test("policy: patching test-owned module paths is allowed")
def test_patch_test_namespace_allowed(opts):
    codes = _codes("""
        from unittest import mock

        def test_thing(opts):
            with mock.patch("test_helpers.support.fake_thing"):
                pass
    """)
    assert codes == [], (
        f"patching a test-owned module path must not be a violation, got {codes}"
    )


@th.unit_test("policy: unresolved-provenance diagnostics are actionable")
def test_patch_unresolved_diagnostic(opts):
    violations = _scan("""
        from unittest import mock
        from tests.support import get_target

        def test_thing(opts):
            target = get_target()
            with mock.patch.object(target, "attr"):
                pass
    """)
    row = next(v for v in violations if v.code == "patch_unresolved")
    assert row.line > 0, "a violation must carry its source line"
    assert "target" in row.detail, (
        f"the diagnostic must name the unresolvable target, got {row.detail!r}"
    )


# ---------------------------------------------------------------------------
# Direct mutation detection
# ---------------------------------------------------------------------------

@th.unit_test("policy: direct assignment to the shared settings singleton")
def test_singleton_direct_assignment(opts):
    codes = _codes("""
        from mojo.helpers.settings import settings

        def test_thing(opts):
            original = settings.get
            settings.get = lambda *a, **kw: None
            settings.get = original
    """)
    assert "settings_singleton_mutation" in codes, (
        f"settings.get = ... must be a violation even when restored, got {codes}"
    )


@th.unit_test("policy: import-aliased singleton assignment is detected")
def test_singleton_aliased_assignment(opts):
    codes = _codes("""
        from mojo.helpers.settings import settings as settings_obj

        def test_thing(opts):
            settings_obj.get = lambda *a, **kw: None
    """)
    assert "settings_singleton_mutation" in codes, (
        f"an aliased settings singleton assignment must be detected, got {codes}"
    )


@th.unit_test("policy: local-name rebinding through assignment is detected")
def test_singleton_assignment_alias_chain(opts):
    codes = _codes("""
        from mojo.helpers.settings import settings

        def test_thing(opts):
            s = settings
            s.get = lambda *a, **kw: None
    """)
    assert "settings_singleton_mutation" in codes, (
        f"a local alias of the singleton must still be detected, got {codes}"
    )


@th.unit_test("policy: deletion of a singleton attribute is detected")
def test_singleton_attribute_deletion(opts):
    codes = _codes("""
        from mojo.helpers.settings import settings

        def test_thing(opts):
            del settings.get
    """)
    assert "settings_singleton_mutation" in codes, (
        f"del settings.<attr> must be a violation, got {codes}"
    )


@th.unit_test("policy: production module attribute assignment is detected")
def test_production_module_attr_assignment(opts):
    codes = _codes("""
        from mojo.apps.account.services import webhooks

        def test_thing(opts):
            webhooks.PUBLISHED_JOB_ID_CAP = 1
    """)
    assert "production_attr_mutation" in codes, (
        f"assigning an attribute on an imported production module must be a "
        f"violation, got {codes}"
    )


@th.unit_test("policy: django.conf settings mutation is detected")
def test_django_settings_mutation(opts):
    codes = _codes("""
        from django.conf import settings as dj_settings

        def test_thing(opts):
            dj_settings.DEBUG = True
    """)
    assert "django_settings_mutation" in codes, (
        f"django.conf.settings attribute assignment must be a violation, got {codes}"
    )


@th.unit_test("policy: os.environ mutation is detected")
def test_environ_mutation(opts):
    codes = _codes("""
        import os

        def test_thing(opts):
            os.environ["AUTH_MODE"] = "test"
            os.environ.update({"OTHER": "1"})
            del os.environ["AUTH_MODE"]
    """)
    assert "environ_mutation" in codes, (
        f"os.environ writes must be violations, got {codes}"
    )


@th.unit_test("policy: sys.modules mutation is detected")
def test_sys_modules_mutation(opts):
    codes = _codes("""
        import sys

        def test_thing(opts):
            sys.modules["mojo.apps.fake"] = object()
    """)
    assert "sys_modules_mutation" in codes, (
        f"sys.modules writes must be violations, got {codes}"
    )


@th.unit_test("policy: reads of shared state are always allowed")
def test_reads_allowed(opts):
    codes = _codes("""
        import os
        from mojo.helpers.settings import settings
        from django.conf import settings as dj_settings

        def test_thing(opts):
            value = settings.get("AUTH_CONFIG")
            debug = dj_settings.DEBUG
            path = os.environ.get("HOME")
    """)
    assert codes == [], f"reads must never be violations, got {codes}"


# ---------------------------------------------------------------------------
# Protected configuration writes (ORM / service / REST)
# ---------------------------------------------------------------------------

@th.unit_test("policy: protected Setting ORM create is detected")
def test_protected_setting_create(opts):
    codes = _codes("""
        from mojo.apps.account.models import Setting

        def test_thing(opts):
            Setting.objects.create(key="AUTH_CONFIG", value="{}")
    """)
    assert "protected_setting_write" in codes, (
        f"creating a protected Setting row must be a violation, got {codes}"
    )


@th.unit_test("policy: protected key family prefixes are detected")
def test_protected_prefix_families(opts):
    codes = _codes("""
        from mojo.apps.account.models import Setting

        def test_thing(opts):
            Setting.objects.create(key="EDGE_RELEASE_BUCKETS", value="[]")
            Setting.objects.create(key="GEOFENCE_ENABLED", value="true")
            Setting.objects.create(key="JOBS_WEBHOOK_MAX_RETRIES", value="1")
    """)
    assert "protected_setting_write" in codes, (
        f"EDGE_/GEOFENCE_/JOBS_WEBHOOK_ family writes must be violations, got {codes}"
    )


@th.unit_test("policy: protected queryset delete via literal collection")
def test_protected_queryset_delete_literal_collection(opts):
    codes = _codes("""
        from mojo.apps.account.models import Setting

        KEYS = ("AUTH_CONFIG", "BASE_URL")

        def test_thing(opts):
            Setting.objects.filter(key__in=KEYS).delete()
    """)
    assert "protected_setting_write" in codes, (
        f"deleting protected keys through a module-constant collection must be "
        f"a violation, got {codes}"
    )


@th.unit_test("policy: module-constant key names are resolved")
def test_protected_key_module_constant(opts):
    codes = _codes("""
        from mojo.apps.account.models import Setting

        KEY = "SECRET_KEY"

        def test_thing(opts):
            Setting.objects.create(key=KEY, value="x")
    """)
    assert "protected_setting_write" in codes, (
        f"a module-level constant resolving to a protected key must be detected, "
        f"got {codes}"
    )


@th.unit_test("policy: TESTIT_ namespace keys are allowed")
def test_testit_namespace_allowed(opts):
    codes = _codes("""
        from mojo.apps.account.models import Setting

        def test_thing(opts):
            Setting.objects.create(key="TESTIT_FIXTURE_FLAG", value="1")
            Setting.objects.filter(key="TESTIT_FIXTURE_FLAG").delete()
    """)
    assert codes == [], (
        f"writes under the reserved TESTIT_ namespace must be allowed, got {codes}"
    )


@th.unit_test("policy: unprotected setting writes are allowed")
def test_unprotected_setting_write_allowed(opts):
    codes = _codes("""
        from mojo.apps.account.models import Setting

        def test_thing(opts):
            Setting.objects.filter(key="SOME_APP_LOCAL_KEY").delete()
    """)
    assert codes == [], (
        f"writes to keys outside the protected roster must be allowed, got {codes}"
    )


@th.unit_test("policy: dynamic setting keys are rejected as unresolved")
def test_protected_setting_dynamic_key(opts):
    codes = _codes("""
        from mojo.apps.account.models import Setting

        def test_thing(opts):
            key = opts.some_key
            Setting.objects.create(key=key, value="x")
    """)
    assert "protected_setting_unresolved" in codes, (
        f"a dynamic key on a Setting write cannot be proven safe and must be "
        f"rejected as unresolved, got {codes}"
    )


@th.unit_test("policy: literal REST settings writes are detected")
def test_protected_rest_write(opts):
    codes = _codes("""
        def test_thing(opts):
            opts.client.post("/api/settings", {"AUTH_CONFIG": {}})
    """)
    assert "protected_rest_write" in codes, (
        f"a literal POST to /api/settings must be a violation, got {codes}"
    )


@th.unit_test("policy: REST settings reads are allowed")
def test_rest_settings_read_allowed(opts):
    codes = _codes("""
        def test_thing(opts):
            opts.client.get("/api/settings")
    """)
    assert codes == [], (
        f"a GET of /api/settings mutates nothing and must be allowed, got {codes}"
    )


@th.unit_test("policy: injected callables and local fakes are allowed")
def test_injection_allowed(opts):
    codes = _codes("""
        def test_thing(opts):
            calls = []

            def fake_reporter(**kwargs):
                calls.append(kwargs)

            from mojo.apps.account.services import webhooks
            webhooks.handle_fanout(opts.job, reporter=fake_reporter)
    """)
    assert codes == [], (
        f"passing a local fake through a dependency seam must be allowed, got {codes}"
    )


# ---------------------------------------------------------------------------
# File and package level behavior
# ---------------------------------------------------------------------------

@th.unit_test("policy: syntax errors fail closed with a scan_error")
def test_syntax_error_fails_closed(opts):
    from testit import isolation
    violations = isolation.scan_source("def broken(:\n", filename="<bad>")
    codes = [v.code for v in violations]
    assert "scan_error" in codes, (
        f"unparseable source must produce a scan_error violation, not silence, "
        f"got {codes}"
    )


@th.unit_test("policy: scan_package aggregates violations across files")
def test_scan_package_aggregates(opts):
    from testit import isolation

    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "__init__.py"), "w") as fh:
            fh.write("TESTIT = {}\n")
        with open(os.path.join(tmpdir, "test_one.py"), "w") as fh:
            fh.write(
                "from unittest import mock\n"
                "def test_a(opts):\n"
                "    with mock.patch('mojo.apps.incident.report_event'):\n"
                "        pass\n"
            )
        with open(os.path.join(tmpdir, "test_two.py"), "w") as fh:
            fh.write("def test_b(opts):\n    assert True, 'fine'\n")

        result = isolation.scan_package(tmpdir)

    assert result.files_scanned == 2, (
        f"both test files must be scanned, got {result.files_scanned}"
    )
    assert any(v.code == "patch_shared" for v in result.violations), (
        f"the offending file's violation must surface, got {result.violations}"
    )
    offending = [v for v in result.violations if v.code == "patch_shared"]
    assert all("test_one.py" in v.file for v in offending), (
        f"violations must name their file, got {offending}"
    )


@th.unit_test("policy: formatted output is actionable")
def test_format_violations(opts):
    from testit import isolation
    violations = isolation.scan_source(
        "from mojo.helpers.settings import settings\n"
        "def test_x(opts):\n"
        "    settings.get = None\n",
        filename="tests/test_pkg/test_mod.py",
    )
    text = isolation.format_violations(violations)
    assert "tests/test_pkg/test_mod.py" in text and "3" in text, (
        f"formatted violations must carry file and line, got {text!r}"
    )
    assert "settings" in text, (
        f"formatted violations must describe the mutation, got {text!r}"
    )


# ---------------------------------------------------------------------------
# Package state machine (phase-3 activation contract, dormant here)
# ---------------------------------------------------------------------------

def _state(config, *, violations=False, origin="django_mojo", has_config=True):
    from testit import isolation
    fake_violations = []
    if violations:
        fake_violations = [
            isolation.violation("patch_shared", "<f>", 1, "fixture violation")]
    return isolation.evaluate_package_state(
        config, fake_violations, origin=origin, has_config=has_config)


@th.unit_test("policy state: clean default_core package is valid")
def test_state_default_valid(opts):
    problems = _state({"default_core": True, "serial": False, "requires_extra": []})
    assert problems == [], (
        f"a clean default-core package must be valid, got {problems}"
    )


@th.unit_test("policy state: opt-in serial mutation package is valid")
def test_state_opt_in_valid(opts):
    problems = _state(
        {"default_core": False, "serial": True, "requires_extra": ["extended"]},
        violations=True)
    assert problems == [], (
        f"an opt-in serial package may carry mutations, got {problems}"
    )


@th.unit_test("policy state: default package with violations is invalid")
def test_state_default_with_violations(opts):
    problems = _state(
        {"default_core": True, "serial": False, "requires_extra": []},
        violations=True)
    assert problems, "a default-core package carrying mutations must fail policy"


@th.unit_test("policy state: default_core cannot also be opt-in")
def test_state_default_cannot_be_opt_in(opts):
    problems = _state(
        {"default_core": True, "serial": False, "requires_extra": ["slow"]})
    assert problems, "default_core=True with requires_extra must fail policy"


@th.unit_test("policy state: opt-in mutation package must be serial")
def test_state_opt_in_requires_serial(opts):
    problems = _state(
        {"default_core": False, "serial": False, "requires_extra": ["extended"]},
        violations=True)
    assert problems, (
        "requires_extra alone is not isolation — a mutation package must also "
        "declare serial=True"
    )


@th.unit_test("policy state: repository package without config fails closed")
def test_state_missing_config_fails_closed(opts):
    problems = _state({}, has_config=False)
    assert problems, (
        "a repository test package without a readable literal TESTIT config "
        "must fail policy, never inherit the permissive default"
    )


@th.unit_test("policy state: undeclared repository package fails closed")
def test_state_undeclared_package_fails_closed(opts):
    problems = _state({"serial": False, "requires_extra": []})
    assert problems, (
        "a repository package that declares neither default_core=True nor a "
        "nonempty requires_extra is in no valid state and must fail policy"
    )


@th.unit_test("policy state: consumer packages keep the permissive default")
def test_state_consumer_exempt(opts):
    problems = _state({}, origin="consumer", has_config=False)
    assert problems == [], (
        f"consumer/application test roots are not part of the repository "
        f"migration and must not be required to declare default_core, got {problems}"
    )
