"""
CSP settings behavior — direct `csp.build_policy()` / `csp.apply()` calls that
override django.conf.settings in-process (maestro item 945).

Moved out of tests/test_auth/csp.py (maestro item #1839): these tests mutate
process-global Django settings via setattr/delattr, which is unsafe under the
parallel default tier. The live-server and in-process render coverage stays in
the source module.

These tests CANNOT be asserted against the live server: its Django settings
are fixed at startup and never see an in-process patch.
"""
from testit import helpers as th
from testit import TestitSkip
from testit.helpers import assert_true, assert_eq


_SENTINEL = object()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _directive(policy, name):
    """Value of one directive in a policy string; None when absent."""
    for chunk in policy.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(None, 1)
        if parts[0].lower() == name:
            return parts[1].strip() if len(parts) > 1 else ""
    return None


def _save_conf(name):
    from django.conf import settings as dj_settings
    return getattr(dj_settings, name, _SENTINEL)


def _restore_conf(name, orig):
    from django.conf import settings as dj_settings
    if orig is _SENTINEL:
        if hasattr(dj_settings, name):
            delattr(dj_settings, name)
    else:
        setattr(dj_settings, name, orig)


def _set_conf(name, value):
    from django.conf import settings as dj_settings
    setattr(dj_settings, name, value)


def _require_csp_enabled():
    """Skip when this test project has not opted into the CSP.

    `AUTH_CSP_ENABLED` ships **False** — that is the shipped contract, asserted
    by `test_csp_is_opt_in_by_default`. Turning it on is a per-deployment choice
    made in `testproject/var/django.conf`, and the whole `testproject/` tree is
    gitignored, so a fresh checkout has no way to inherit it.

    Without this guard every test below fails on any machine but the one that
    authored the feature, and on CI — reporting a broken CSP when the CSP is
    behaving exactly as designed. The tests that assert the OFF behavior need no
    guard; they hold either way.

    Read through the SAME accessor `csp.apply()` uses, so the guard and the
    feature can never disagree — a guard reading a different source would either
    skip a test that would have passed, or run one that never could.
    """
    from mojo.helpers.settings import settings
    if not settings.get_static("AUTH_CSP_ENABLED", False, kind="bool"):
        raise TestitSkip(
            "AUTH_CSP_ENABLED is not True for this test project — set it in "
            "testproject/var/django.conf to run the CSP tests")


# ---------------------------------------------------------------------------
# Settings behavior — direct calls only. The live server loaded its settings at
# startup and can never observe an in-process patch.
# ---------------------------------------------------------------------------

@th.django_unit_test("AUTH_CSP_DIRECTIVES replaces a directive wholesale")
def test_directive_override_replaces(opts):
    from mojo.apps.account.services import csp

    orig = _save_conf('AUTH_CSP_DIRECTIVES')
    try:
        _set_conf('AUTH_CSP_DIRECTIVES', {"frame-ancestors": "'self'"})
        policy = csp.build_policy('abc')
        assert_eq(_directive(policy, "frame-ancestors"), "'self'",
                  f"a deployment-supplied frame-ancestors must replace the "
                  f"baseline value. Got: {policy!r}")
        assert_eq(_directive(policy, "default-src"), "'self'",
                  f"an override must leave the other directives alone. "
                  f"Got: {policy!r}")
    finally:
        _restore_conf('AUTH_CSP_DIRECTIVES', orig)


@th.django_unit_test("AUTH_CSP_DIRECTIVES can add an unknown directive and drop a known one")
def test_directive_override_adds_and_drops(opts):
    from mojo.apps.account.services import csp

    orig = _save_conf('AUTH_CSP_DIRECTIVES')
    try:
        _set_conf('AUTH_CSP_DIRECTIVES', {
            "report-uri": "/csp/report",
            "font-src": "",
        })
        policy = csp.build_policy('abc')
        assert_eq(_directive(policy, "report-uri"), "/csp/report",
                  f"an unknown key must be emitted as-is so a deployment can "
                  f"add report-uri. Got: {policy!r}")
        assert_eq(_directive(policy, "font-src"), None,
                  f"an empty override value must DROP the directive. "
                  f"Got: {policy!r}")
    finally:
        _restore_conf('AUTH_CSP_DIRECTIVES', orig)


@th.django_unit_test("the nonce is appended to a deployment-supplied script-src and cannot be removed")
def test_nonce_survives_script_src_override(opts):
    from mojo.apps.account.services import csp

    orig = _save_conf('AUTH_CSP_DIRECTIVES')
    try:
        _set_conf('AUTH_CSP_DIRECTIVES', {"script-src": "'self' https://cdn.example.com"})
        policy = csp.build_policy('deadbeef')
        script_src = _directive(policy, "script-src") or ""
        assert_true("'nonce-deadbeef'" in script_src,
                    f"the per-request nonce must always be appended to the "
                    f"final script-src, or the page's own inline scripts stop "
                    f"running. Got: {script_src!r}")
        assert_true('https://cdn.example.com' in script_src,
                    f"the deployment's own script source must survive. "
                    f"Got: {script_src!r}")

        _set_conf('AUTH_CSP_DIRECTIVES', {"script-src": ""})
        policy = csp.build_policy('deadbeef')
        assert_eq(_directive(policy, "script-src"), "'nonce-deadbeef'",
                  f"emptying script-src must NOT drop it — the nonce is not "
                  f"removable, AUTH_CSP_ENABLED=False is the opt-out. "
                  f"Got: {policy!r}")
    finally:
        _restore_conf('AUTH_CSP_DIRECTIVES', orig)


@th.django_unit_test("AUTH_CSP_ENABLED=False sets no CSP header at all")
def test_csp_can_be_disabled(opts):
    # Guarded whole: the tail asserts the header returns once the project's own
    # True is restored. The OFF half stays covered by test_csp_is_opt_in_by_default,
    # which needs no guard.
    _require_csp_enabled()
    from django.http import HttpResponse
    from mojo.apps.account.services import csp

    orig = _save_conf('AUTH_CSP_ENABLED')
    try:
        _set_conf('AUTH_CSP_ENABLED', False)
        response = csp.apply(HttpResponse('ok'), 'abc')
        assert_true(csp.HEADER not in response,
                    f"AUTH_CSP_ENABLED=False must set no enforcing header. "
                    f"Got: {response.get(csp.HEADER)!r}")
        assert_true(csp.REPORT_ONLY_HEADER not in response,
                    f"AUTH_CSP_ENABLED=False must set no report-only header "
                    f"either. Got: {response.get(csp.REPORT_ONLY_HEADER)!r}")
    finally:
        _restore_conf('AUTH_CSP_ENABLED', orig)

    response = csp.apply(HttpResponse('ok'), 'abc')
    assert_true(csp.HEADER in response,
                "with AUTH_CSP_ENABLED restored to the test project's True the "
                "header must come back — the setting is read per request, not "
                "at import")


@th.django_unit_test("CSP is OPT-IN: absent AUTH_CSP_ENABLED sets no header")
def test_csp_is_opt_in_by_default(opts):
    """The shipped default is OFF. This is the upgrade-safety test.

    A deployment that upgrades without asking for a CSP must get exactly what
    it had before: no header. The test project sets AUTH_CSP_ENABLED = True so
    the live-server tests can assert a real header, so this test removes
    the setting entirely to see what a stock deployment gets.
    """
    from django.http import HttpResponse
    from mojo.apps.account.services import csp

    orig = _save_conf('AUTH_CSP_ENABLED')
    try:
        from django.conf import settings as dj_settings
        if hasattr(dj_settings, 'AUTH_CSP_ENABLED'):
            delattr(dj_settings, 'AUTH_CSP_ENABLED')
        response = csp.apply(HttpResponse('ok'), 'abc')
        assert_true(csp.HEADER not in response,
                    f"with AUTH_CSP_ENABLED unset the framework must send NO "
                    f"enforcing CSP — the header is opt-in. "
                    f"Got: {response.get(csp.HEADER)!r}")
        assert_true(csp.REPORT_ONLY_HEADER not in response,
                    f"an unset AUTH_CSP_ENABLED must not send a report-only "
                    f"header either. Got: {response.get(csp.REPORT_ONLY_HEADER)!r}")
    finally:
        _restore_conf('AUTH_CSP_ENABLED', orig)


@th.django_unit_test("AUTH_CSP_REPORT_ONLY=True swaps the enforcing header for the report-only one")
def test_report_only_mode(opts):
    # Report-only still requires the CSP to be switched on at all.
    _require_csp_enabled()
    from django.http import HttpResponse
    from mojo.apps.account.services import csp

    orig = _save_conf('AUTH_CSP_REPORT_ONLY')
    try:
        _set_conf('AUTH_CSP_REPORT_ONLY', True)
        response = csp.apply(HttpResponse('ok'), 'abc')
        assert_true(csp.REPORT_ONLY_HEADER in response,
                    "AUTH_CSP_REPORT_ONLY=True must set "
                    "Content-Security-Policy-Report-Only")
        assert_true(csp.HEADER not in response,
                    f"report-only mode must NOT also set the enforcing header "
                    f"— the page would still be enforced. Got: "
                    f"{response.get(csp.HEADER)!r}")
        assert_true("'nonce-abc'" in response[csp.REPORT_ONLY_HEADER],
                    f"the report-only policy must still carry the nonce. "
                    f"Got: {response[csp.REPORT_ONLY_HEADER]!r}")
    finally:
        _restore_conf('AUTH_CSP_REPORT_ONLY', orig)
