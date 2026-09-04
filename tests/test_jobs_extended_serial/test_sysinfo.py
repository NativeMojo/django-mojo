"""Live-runner sysinfo integration tests.

Runner heartbeats and Redis control channels are process-global. These tests
therefore run in the existing serial Jobs tier instead of observing short-lived
runners created by parallel test modules.
"""

from testit import helpers as th
from testit import TestitSkip


ADMIN_USER = "sysinfo_admin"
ADMIN_PWORD = "testit##mojo"


@th.django_unit_setup()
def setup_sysinfo_tests(opts):
    from mojo.apps.account.models import User

    admin = User.objects.filter(username=ADMIN_USER).last()
    if admin is None:
        admin = User(
            username=ADMIN_USER,
            display_name=ADMIN_USER,
            email=f"{ADMIN_USER}@example.com",
        )
        admin.save()
    admin.remove_all_permissions()
    admin.add_permission([
        "manage_jobs", "view_jobs", "manage_users", "view_global",
    ])
    admin.is_staff = True
    admin.is_superuser = True
    admin.is_email_verified = True
    admin.save_password(ADMIN_PWORD)


def _require_runners():
    from mojo.apps import jobs

    alive = [row for row in jobs.get_runners() if row.get("alive")]
    if not alive:
        raise TestitSkip("No live runners available — skipping live sysinfo tests")
    return alive


@th.unit_test("sysinfo_api_all_runners")
def test_api_all_runners(opts):
    """get_sysinfo() collects a reply from each live runner."""
    from mojo.apps import jobs

    alive = _require_runners()
    results = jobs.get_sysinfo(timeout=5.0)

    assert isinstance(results, list), \
        f"Expected list, got {type(results).__name__}"
    if not results:
        expected = {row.get("runner_id") for row in alive}
        still_alive = {
            row.get("runner_id") for row in jobs.get_runners()
            if row.get("alive")
        }
        if not expected.intersection(still_alive):
            raise TestitSkip("Discovered runners stopped before the sysinfo request")
    assert results, "Expected at least one reply from a runner that remains live"
    opts.sysinfo_first = results[0]
    opts.sysinfo_runner_id = results[0]["runner_id"]


@th.unit_test("sysinfo_api_reply_shape")
def test_api_reply_shape(opts):
    """Each reply has the expected top-level keys."""
    if not getattr(opts, "sysinfo_first", None):
        raise TestitSkip("No sysinfo results collected — skipping shape test")
    reply = opts.sysinfo_first

    for key in ("runner_id", "func", "status", "timestamp", "result"):
        assert key in reply, f"Reply missing expected key: {key}"
    assert reply["status"] == "success", \
        f"Expected status 'success', got {reply['status']!r}"
    assert reply["func"] == "mojo.apps.jobs.services.sysinfo_task.collect_sysinfo"


@th.unit_test("sysinfo_api_result_shape")
def test_api_result_shape(opts):
    """The result dict contains expected sysinfo keys."""
    if not getattr(opts, "sysinfo_first", None):
        raise TestitSkip("No sysinfo results collected — skipping result shape test")
    result = opts.sysinfo_first.get("result", {})

    for key in ("os", "cpu_load", "memory", "disk", "network"):
        assert key in result, f"sysinfo result missing expected key: {key}"
    assert isinstance(result["cpu_load"], (int, float)), \
        "cpu_load should be numeric"
    assert result["memory"]["total"] > 0, \
        "memory.total should be positive"
    assert result["disk"]["total"] > 0, \
        "disk.total should be positive"


@th.unit_test("sysinfo_api_single_runner")
def test_api_single_runner(opts):
    """get_sysinfo(runner_id=<id>) returns exactly that runner's reply."""
    if not getattr(opts, "sysinfo_runner_id", None):
        raise TestitSkip("No runner_id recorded — skipping single-runner test")
    from mojo.apps import jobs

    runner_id = opts.sysinfo_runner_id
    results = jobs.get_sysinfo(runner_id=runner_id, timeout=5.0)
    assert len(results) == 1, f"Expected exactly 1 reply, got {len(results)}"
    assert results[0]["runner_id"] == runner_id


@th.unit_test("sysinfo_rest_all_runners")
def test_rest_all_runners(opts):
    """GET /api/jobs/runners/sysinfo returns live runner data."""
    _require_runners()
    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get("/api/jobs/runners/sysinfo")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    data = resp.json
    assert data.get("status") is True
    assert isinstance(data.get("data"), list)
    assert data.get("count") == len(data["data"])
    assert data["count"] > 0, "Expected at least one runner reply"
    opts.rest_runner_id = data["data"][0]["runner_id"]


@th.unit_test("sysinfo_rest_specific_runner")
def test_rest_specific_runner(opts):
    """GET /api/jobs/runners/sysinfo/<runner_id> returns that runner."""
    if not getattr(opts, "rest_runner_id", None):
        raise TestitSkip("No runner_id recorded — skipping specific-runner test")
    runner_id = opts.rest_runner_id
    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get(f"/api/jobs/runners/sysinfo/{runner_id}")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    data = resp.json
    assert data.get("status") is True
    reply = data["data"]
    assert reply["runner_id"] == runner_id
    assert reply["status"] == "success"
    for key in ("os", "cpu_load", "memory", "disk", "network"):
        assert key in reply["result"], f"sysinfo result missing key: {key}"


@th.unit_test("sysinfo_rest_custom_timeout")
def test_rest_custom_timeout(opts):
    """GET /api/jobs/runners/sysinfo?timeout=3.0 is accepted."""
    _require_runners()
    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get("/api/jobs/runners/sysinfo?timeout=3.0")

    assert resp.status_code == 200, \
        f"Expected 200 with explicit timeout param, got {resp.status_code}"
    data = resp.json
    assert data.get("status") is True
