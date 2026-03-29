"""
Sysinfo Tests - jobs.get_sysinfo() and REST endpoint coverage.

Tests the Python API and REST endpoints for collecting host system info
from job runners. Most tests require at least one live runner to be active.
Tests that don't need a runner (permission guards, shape checks) always run.

Run in your Django project:
    python manage.py testit test_jobs.test_sysinfo
"""
from testit import helpers as th
from testit import TestitSkip
from mojo.helpers.settings import settings

ADMIN_USER = "sysinfo_admin"
ADMIN_PWORD = "testit##mojo"

UNPRIV_USER = "sysinfo_user"
UNPRIV_PWORD = "testit##mojo"


def _require_runners(opts):
    """Raise TestitSkip when no live runners are available."""
    from mojo.apps import jobs
    runners = jobs.get_runners()
    alive = [r for r in runners if r.get('alive')]
    if not alive:
        raise TestitSkip("No live runners available — skipping live sysinfo tests")
    return alive


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

@th.unit_test("sysinfo_rest_unauthenticated")
def test_rest_unauthenticated(opts):
    """Unauthenticated request must be rejected."""
    opts.client.logout()
    resp = opts.client.get("/api/jobs/runners/sysinfo")
    assert resp.status_code in (401, 403), \
        f"Expected 401 or 403 for unauthenticated request, got {resp.status_code}"


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


# ------------------------------------------------------------------
# Live runner tests — skipped when no runners are active
# ------------------------------------------------------------------

@th.unit_test("sysinfo_api_all_runners")
def test_api_all_runners(opts):
    """get_sysinfo() collects a reply from each live runner."""
    from mojo.apps import jobs
    alive = _require_runners(opts)
    results = jobs.get_sysinfo(timeout=5.0)

    assert isinstance(results, list), \
        f"Expected list, got {type(results).__name__}"
    assert len(results) > 0, \
        "Expected at least one reply from a live runner"

    # Store first result for shape tests below
    opts.sysinfo_first = results[0]
    opts.sysinfo_runner_id = results[0]['runner_id']


@th.unit_test("sysinfo_api_reply_shape")
def test_api_reply_shape(opts):
    """Each reply has the expected top-level keys."""
    if not getattr(opts, 'sysinfo_first', None):
        raise TestitSkip("No sysinfo results collected — skipping shape test")

    reply = opts.sysinfo_first
    for key in ('runner_id', 'func', 'status', 'timestamp', 'result'):
        assert key in reply, f"Reply missing expected key: {key}"

    assert reply['status'] == 'success', \
        f"Expected status 'success', got {reply['status']!r}"
    assert reply['func'] == 'mojo.apps.jobs.services.sysinfo_task.collect_sysinfo'


@th.unit_test("sysinfo_api_result_shape")
def test_api_result_shape(opts):
    """The result dict contains expected sysinfo keys."""
    if not getattr(opts, 'sysinfo_first', None):
        raise TestitSkip("No sysinfo results collected — skipping result shape test")

    result = opts.sysinfo_first.get('result', {})
    for key in ('os', 'cpu_load', 'memory', 'disk', 'network'):
        assert key in result, f"sysinfo result missing expected key: {key}"

    # Basic sanity checks
    assert isinstance(result['cpu_load'], (int, float)), \
        "cpu_load should be numeric"
    assert result['memory']['total'] > 0, \
        "memory.total should be positive"
    assert result['disk']['total'] > 0, \
        "disk.total should be positive"


@th.unit_test("sysinfo_api_single_runner")
def test_api_single_runner(opts):
    """get_sysinfo(runner_id=<id>) returns exactly one reply for that runner."""
    if not getattr(opts, 'sysinfo_runner_id', None):
        raise TestitSkip("No runner_id recorded — skipping single-runner test")

    from mojo.apps import jobs
    results = jobs.get_sysinfo(runner_id=opts.sysinfo_runner_id, timeout=5.0)

    assert isinstance(results, list), \
        f"Expected list, got {type(results).__name__}"
    assert len(results) == 1, \
        f"Expected exactly 1 reply, got {len(results)}"
    assert results[0]['runner_id'] == opts.sysinfo_runner_id, \
        f"Expected runner_id {opts.sysinfo_runner_id!r}, got {results[0]['runner_id']!r}"


# ------------------------------------------------------------------
# REST: all-runners endpoint
# ------------------------------------------------------------------

@th.unit_test("sysinfo_rest_all_runners")
def test_rest_all_runners(opts):
    """GET /api/jobs/runners/sysinfo returns correct shape with live runners."""
    alive = _require_runners(opts)
    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get("/api/jobs/runners/sysinfo")

    assert resp.status_code == 200, \
        f"Expected 200, got {resp.status_code}"

    data = resp.json
    assert data.get('status') is True
    assert 'count' in data
    assert 'data' in data
    assert isinstance(data['data'], list)
    assert data['count'] == len(data['data'])
    assert data['count'] > 0, \
        "Expected at least one runner reply"

    # Store runner_id for the specific-runner REST test
    opts.rest_runner_id = data['data'][0]['runner_id']


@th.unit_test("sysinfo_rest_specific_runner")
def test_rest_specific_runner(opts):
    """GET /api/jobs/runners/sysinfo/<runner_id> returns info for that runner."""
    if not getattr(opts, 'rest_runner_id', None):
        raise TestitSkip("No runner_id recorded from all-runners test — skipping")

    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get(f"/api/jobs/runners/sysinfo/{opts.rest_runner_id}")

    assert resp.status_code == 200, \
        f"Expected 200, got {resp.status_code}"

    data = resp.json
    assert data.get('status') is True
    assert 'data' in data

    reply = data['data']
    assert reply['runner_id'] == opts.rest_runner_id
    assert reply['status'] == 'success'
    assert 'result' in reply

    result = reply['result']
    for key in ('os', 'cpu_load', 'memory', 'disk', 'network'):
        assert key in result, f"sysinfo result missing key: {key}"


@th.unit_test("sysinfo_rest_custom_timeout")
def test_rest_custom_timeout(opts):
    """GET /api/jobs/runners/sysinfo?timeout=3.0 is accepted."""
    alive = _require_runners(opts)
    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get("/api/jobs/runners/sysinfo?timeout=3.0")

    assert resp.status_code == 200, \
        f"Expected 200 with explicit timeout param, got {resp.status_code}"
    data = resp.json
    assert data.get('status') is True