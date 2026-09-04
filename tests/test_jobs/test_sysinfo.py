"""
Sysinfo Tests - jobs.get_sysinfo() and REST endpoint coverage.

Tests the Python API and REST endpoints that do not need a live runner.
Live-runner integration coverage lives in the serial sibling package because
runner discovery and Redis control channels are shared process-wide.

Run in your Django project:
    python manage.py testit test_jobs.test_sysinfo

The live-runner cases are discovered from test_jobs_extended_serial.
"""

TESTIT_TIER = "slow"
from testit import helpers as th

ADMIN_USER = "sysinfo_admin"
ADMIN_PWORD = "testit##mojo"

UNPRIV_USER = "sysinfo_user"
UNPRIV_PWORD = "testit##mojo"


# ------------------------------------------------------------------
# Setup
# ------------------------------------------------------------------

@th.django_unit_setup()
def setup_sysinfo_tests(opts):
    """Create admin and unprivileged test users."""
    from mojo.apps.account.models import User

    admin = User.objects.filter(username=ADMIN_USER).last()
    if admin is None:
        admin = User(
            username=ADMIN_USER,
            display_name=ADMIN_USER,
            email=f"{ADMIN_USER}@example.com"
        )
        admin.save()
    admin.remove_all_permissions()
    admin.add_permission(["manage_jobs", "view_jobs", "manage_users", "view_global"])
    admin.is_staff = True
    admin.is_superuser = True
    admin.is_email_verified = True
    admin.save_password(ADMIN_PWORD)

    unpriv = User.objects.filter(username=UNPRIV_USER).last()
    if unpriv is None:
        unpriv = User(
            username=UNPRIV_USER,
            display_name=UNPRIV_USER,
            email=f"{UNPRIV_USER}@example.com"
        )
        unpriv.save()
    unpriv.remove_all_permissions()
    unpriv.is_email_verified = True
    unpriv.save_password(UNPRIV_PWORD)


# ------------------------------------------------------------------
# Auth helpers
# ------------------------------------------------------------------

@th.unit_test("sysinfo_admin_login")
def test_admin_login(opts):
    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "admin authentication failed"


@th.unit_test("sysinfo_unpriv_login")
def test_unpriv_login(opts):
    resp = opts.client.login(UNPRIV_USER, UNPRIV_PWORD)
    assert opts.client.is_authenticated, "unprivileged user authentication failed"


# ------------------------------------------------------------------
# Permission guard tests — always run, no runners needed
# ------------------------------------------------------------------

@th.tier("core")
@th.unit_test("sysinfo_rest_unauthenticated")
def test_rest_unauthenticated(opts):
    """Unauthenticated request must be rejected."""
    opts.client.logout()
    resp = opts.client.get("/api/jobs/runners/sysinfo")
    assert resp.status_code in (401, 403), \
        f"Expected 401 or 403 for unauthenticated request, got {resp.status_code}"


@th.tier("core")
@th.unit_test("sysinfo_rest_forbidden_no_perms")
def test_rest_forbidden_no_perms(opts):
    """Unprivileged user must be rejected."""
    opts.client.login(UNPRIV_USER, UNPRIV_PWORD)
    resp = opts.client.get("/api/jobs/runners/sysinfo")
    assert resp.status_code in (401, 403), \
        f"Expected 401 or 403 for user without perms, got {resp.status_code}"


@th.unit_test("sysinfo_rest_specific_runner_unauthenticated")
def test_rest_specific_runner_unauthenticated(opts):
    """Unauthenticated request to specific-runner endpoint must be rejected."""
    opts.client.logout()
    resp = opts.client.get("/api/jobs/runners/sysinfo/runner-fake-id")
    assert resp.status_code in (401, 403), \
        f"Expected 401 or 403 for unauthenticated request, got {resp.status_code}"


# ------------------------------------------------------------------
# REST: unknown runner returns 404
# ------------------------------------------------------------------

# extended: ~5s, all of it spent waiting for a Redis round-trip to a runner that
# does not exist. A 404-on-unknown-id detail, not a framework contract.
@th.requires_extra("extended")
@th.unit_test("sysinfo_rest_unknown_runner_404")
def test_rest_unknown_runner_404(opts):
    """Requesting sysinfo for a non-existent runner must return 404."""
    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get("/api/jobs/runners/sysinfo/runner-does-not-exist-xyz")
    assert resp.status_code == 404, \
        f"Expected 404 for unknown runner, got {resp.status_code}"
    data = resp.json
    assert data.get('status') is False


# ------------------------------------------------------------------
# Python API: get_sysinfo() returns a list always
# ------------------------------------------------------------------

# extended: ~5s of runner-discovery timeout to assert a return type.
@th.requires_extra("extended")
@th.unit_test("sysinfo_api_returns_list")
def test_api_returns_list(opts):
    """get_sysinfo() always returns a list (may be empty when no runners)."""
    from mojo.apps import jobs
    result = jobs.get_sysinfo()
    assert isinstance(result, list), \
        f"Expected list from get_sysinfo(), got {type(result).__name__}"


@th.unit_test("sysinfo_api_unknown_runner_returns_empty_list")
def test_api_unknown_runner_returns_empty_list(opts):
    """get_sysinfo(runner_id=<unknown>) returns [] on timeout, not an error."""
    from mojo.apps import jobs
    result = jobs.get_sysinfo(runner_id="runner-does-not-exist-xyz", timeout=1.0)
    assert isinstance(result, list), \
        f"Expected list, got {type(result).__name__}"
    assert result == [], \
        f"Expected empty list for unknown runner, got {result}"
