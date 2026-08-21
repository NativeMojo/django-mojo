"""Exhaustive Admin Platform/Advanced settings and dashboard matrices.

Moved from tests/test_account/test_admin_platform.py (maestro item #1839):
these tests patch production module attributes, mutate django.conf.settings,
and write protected Setting rows (AUTH_CONFIG, EDGE_FRAMEWORK_VERSION),
which is unsafe under the parallel default tier. The read-only contracts
remain in the default-tier module.
"""

from contextlib import contextmanager
from unittest import mock

from testit import helpers as th


@contextmanager
def _override_setting(name, value):
    """In-process Django settings override (th.server_settings only affects the
    separate server process; override_settings is banned by testing rules)."""
    import django.conf
    sentinel = object()
    original = getattr(django.conf.settings, name, sentinel)
    setattr(django.conf.settings, name, value)
    try:
        yield
    finally:
        if original is sentinel:
            delattr(django.conf.settings, name)
        else:
            setattr(django.conf.settings, name, original)


@th.django_unit_setup()
def setup_admin_platform(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import system_settings
    User.objects.filter(username__in=("platform-root", "platform-user")).delete()
    root = User.objects.create_user(
        email="platform-root@test.com", username="platform-root", password="example")
    root.is_active = True
    root.is_superuser = True
    root.save()
    system_settings.set_value(root, system_settings.AUTH_CONFIG, {})
    user = User.objects.create_user(
        email="platform-user@test.com", username="platform-user", password="example")
    user.is_active = True
    user.save()
    opts.platform_root = root.pk
    opts.platform_user = user.pk


@th.django_unit_test("AUTH_CONFIG is protected from generic save, rename and delete")
def test_auth_config_generic_protection(opts):
    from mojo import errors as me
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import system_settings
    root = User.objects.get(pk=opts.platform_root)
    system_settings.set_auth_safe_fields(root, {"theme.app_title": "Control"})
    row = Setting.objects.get(key="AUTH_CONFIG")
    row.value = "{}"
    with th.assert_raises(me.PermissionDeniedException):
        row.save()
    with th.assert_raises(me.PermissionDeniedException):
        row.delete()
    generic = Setting.objects.create(key="PLATFORM_TEST_GENERIC", value="ok")
    generic.key = "AUTH_CONFIG"
    with th.assert_raises(me.PermissionDeniedException):
        generic.save()
    system_settings.set_value(root, system_settings.AUTH_CONFIG, {})


@th.django_unit_test("safe auth writer supports methods and preserves unknown keys")
def test_auth_safe_merge(opts):
    from mojo import errors as me
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import system_settings
    root = User.objects.get(pk=opts.platform_root)
    # Seed through the private dedicated service primitive to represent a
    # future AUTH_CONFIG key this version does not understand.
    system_settings.set_value(root, system_settings.AUTH_CONFIG, {
        "future": {"retained": True}, "login": {"methods": ["password", "passkey"]}})
    result = system_settings.set_auth_safe_fields(
        root, {
            "theme.app_title": "Platform", "theme.accent_color": "#112233",
            "login.methods": ["password", "passkey"],
            "registration.enabled": True,
            "registration.methods": ["password", "github"],
            "registration.passkey_prompt": "optional",
        })
    assert result["future"] == {"retained": True}
    assert result["login"]["methods"] == ["password", "passkey"]
    assert result["registration"] == {
        "enabled": True, "methods": ["password", "github"],
        "passkey_prompt": "optional",
    }
    with th.assert_raises(me.ValueException):
        system_settings.set_auth_safe_fields(root, {"login.methods": ["passkey"]})
    with th.assert_raises(me.ValueException):
        system_settings.set_auth_safe_fields(root, {
            "registration.enabled": True, "registration.methods": []})
    with th.assert_raises(me.ValueException):
        system_settings.set_auth_safe_fields(root, {
            "login.methods": ["password", "telepathy"]})
    with th.assert_raises(me.ValueException):
        system_settings.set_auth_safe_fields(root, {
            "registration.enabled": "yes"})
    disabled = system_settings.set_auth_safe_fields(root, {
        "registration.enabled": False, "registration.methods": []})
    assert disabled["registration"]["methods"] == []
    for path in (
            "theme.api_base", "theme.success_redirect",
            "theme.back_to_website_url", "theme.terms_url",
            "theme.custom_css_url"):
        with th.assert_raises(me.ValueException):
            system_settings.set_auth_safe_fields(root, {path: "https://unsafe.test"})
    assert Setting.objects.get(key="AUTH_CONFIG").is_secret is False
    system_settings.set_value(root, system_settings.AUTH_CONFIG, {})


@th.django_unit_test("fleet collector uses the exact bounded edge registry")
def test_fleet_exact_bounded_registry(opts):
    from mojo.apps.account.services import admin_platform
    with mock.patch("mojo.apps.jobs.get_runners_bounded", return_value=[]) as roster:
        result = admin_platform._fleet()
    roster.assert_called_once_with("edge", limit=100, timeout=1.0)
    assert result["runners"] == []
    assert result["truncated"] is False, \
        "the exact registry was incorrectly described as a partial SCAN page"


@th.django_unit_test("fleet collector preserves the exact live edge roster")
def test_fleet_roster_projection(opts):
    from mojo.apps.account.services import admin_platform
    rows = [
        {"runner_id": "mv1-engine", "channels": ["edge", "default"],
         "last_heartbeat": "2026-08-11T14:55:22+00:00"},
        {"runner_id": "mv2-engine", "channels": ["edge"],
         "last_heartbeat": "2026-08-11T14:55:25+00:00"},
    ]
    with mock.patch("mojo.apps.jobs.get_runners_bounded", return_value=rows):
        result = admin_platform._fleet()
    assert [row["runner"] for row in result["runners"]] == [
        "mv1-engine", "mv2-engine"], \
        "the Admin fleet rollup dropped a live edge runner"
    assert result["_collector_status"] == "healthy"


@th.django_unit_test("WebApp probe uses the pinned no-redirect public collector")
def test_webapp_current_probe_contract(opts):
    from mojo.apps.account.services import admin_platform
    from mojo.apps.edge.services import public_probe
    summary = {"address": {"https_origin": "https://app.example.com"}}
    with mock.patch.object(public_probe, "probe_https_root", return_value={
            "ok": True, "status": 200, "redirected": False}) as probe:
        result = admin_platform._probe_webapp(summary)
    probe.assert_called_once_with(
        "https://app.example.com", timeout=1.5, max_body=65536)
    assert result["status"] == "healthy" and result["http_status"] == 200, \
        f"current WebApp proof was not freshness-bearing health: {result!r}"
    assert result.get("observed_at") and result.get("stale_after"), \
        f"current WebApp proof omitted freshness bounds: {result!r}"


@th.django_unit_test("security evidence names each disabled secure control")
def test_security_disabled_posture(opts):
    from mojo.apps.account.services import admin_platform
    values = {
        "SECURE_SSL_REDIRECT": False, "SESSION_COOKIE_SECURE": True,
        "CSRF_COOKIE_SECURE": False, "SECURE_HSTS_SECONDS": 0,
    }
    with mock.patch.object(admin_platform.settings, "get_static",
                           side_effect=lambda key, default=None: values.get(key, default)):
        posture = admin_platform._secure_posture()
    assert posture["disabled"] == ["https_redirect", "csrf_cookie_secure", "hsts"], \
        f"disabled security controls were not explicit: {posture!r}"
    assert set(posture["controls"].values()) <= {True, False}, \
        f"security posture exposed values beyond booleans: {posture!r}"


@th.django_unit_test("safe settings writer requires a live literal superuser")
def test_auth_writer_literal_superuser(opts):
    from mojo import errors as me
    from mojo.apps.account.models import User
    from mojo.apps.account.services import system_settings
    user = User.objects.get(pk=opts.platform_user)
    user.add_permission("manage_advanced")
    with th.assert_raises(me.PermissionDeniedException):
        system_settings.set_auth_safe_fields(user, {"theme.app_title": "Nope"})


@th.django_unit_test("the framework hold is written by the typed owner writer, and read back at once")
def test_framework_pin_write_path(opts):
    from mojo import errors as me
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import admin_platform, system_settings
    from mojo.apps.edge.settings_validators import FRAMEWORK_VERSION_KEY

    root = User.objects.get(pk=opts.platform_root)
    Setting.objects.filter(key=FRAMEWORK_VERSION_KEY).delete()
    try:
        assert system_settings.set_value(root, FRAMEWORK_VERSION_KEY, "1.11.6") == "1.11.6", \
            "an explicit published version must be stored verbatim"
        assert admin_platform._deployments()["framework_pin"] == {
            "configured": True, "value": "1.11.6", "mode": "pinned",
            "resolved": "1.11.6"}, \
            "the Platform overview did not reflect the pin that was just written"

        assert system_settings.set_value(root, FRAMEWORK_VERSION_KEY, "HOLD") == "hold", \
            "the hold sentinel must be casefolded on the way in"
        held = admin_platform._deployments()["framework_pin"]
        assert held["mode"] == "hold" and held["value"] == "hold", \
            f"a hold must be reported as such: {held!r}"
        assert "resolved" in held, \
            f"a hold must report what it currently resolves to: {held!r}"

        assert system_settings.set_value(root, FRAMEWORK_VERSION_KEY, "latest") == "", \
            "'latest' is the unset synonym — it must not be stored literally"
        assert admin_platform._deployments()["framework_pin"] == {
            "configured": False, "value": None, "mode": "latest", "resolved": None}, \
            "an unset hold must read back as the newest-release default"

        for bad in ("stable", "v1.2.3", "1.0; rm -rf /"):
            try:
                system_settings.set_value(root, FRAMEWORK_VERSION_KEY, bad)
                raise AssertionError(f"{bad!r} is not a version and must be refused")
            except me.ValueException as err:
                assert "'hold'" in str(err), \
                    f"the refusal must name the accepted forms: {err}"

        # The generic Setting writer must not be able to reach it at all.
        generic = Setting(key=FRAMEWORK_VERSION_KEY, value="9.9.9")
        with th.assert_raises(me.PermissionDeniedException):
            generic.save()
    finally:
        Setting.objects.filter(key=FRAMEWORK_VERSION_KEY).delete()


@th.django_unit_test("_deployments answers what serves now, and whose lease is live")
def test_deployments_currently_serving_and_coordination(opts):
    """Additive payload (item 2225): `currently_serving` is the newest
    CONVERGED attempt — after a failed deploy items[0] is the failure, and
    this block is the answer to "so what is the fleet actually running?" —
    and `coordination` now carries the lease's own sha and timestamp."""
    import json
    import uuid
    from datetime import timedelta

    from django.utils import timezone
    from mojo.apps.account.services import admin_platform
    from mojo.apps.edge.models import PlatformDeployment

    PlatformDeployment.objects.all().delete()
    older = PlatformDeployment.objects.create(
        sha="a" * 40, actor="test", source="test", status="converged",
        framework_version="1.11.0", request_key=str(uuid.uuid4()))
    newer = PlatformDeployment.objects.create(
        sha="b" * 40, actor="test", source="test", status="converged",
        framework_version="1.12.0", request_key=str(uuid.uuid4()),
        finished=timezone.now())
    failed = PlatformDeployment.objects.create(
        sha="c" * 40, actor="test", source="test", status="failed",
        request_key=str(uuid.uuid4()))
    PlatformDeployment.objects.filter(pk=older.pk).update(
        created=timezone.now() - timedelta(minutes=10))
    PlatformDeployment.objects.filter(pk=newer.pk).update(
        created=timezone.now() - timedelta(minutes=5))

    lease = json.dumps({
        "state": "migrating", "sha": "c" * 40,
        "deployment": str(failed.pk), "detail": "",
        "at": "2026-08-19T00:00:00+00:00"})
    with mock.patch.object(admin_platform, "_redis_client") as redis_ctx:
        pipe = redis_ctx.return_value.__enter__.return_value.pipeline.return_value
        pipe.execute.return_value = [None, lease]
        value = admin_platform._deployments()
    try:
        newer.refresh_from_db()
        serving = value["currently_serving"]
        th.assert_eq(serving["deployment"], str(newer.pk),
                     f"currently_serving must be the newest CONVERGED row, got {serving!r}")
        th.assert_eq(serving["sha"], "b" * 40,
                     f"currently_serving must carry the proven sha, got {serving!r}")
        th.assert_eq(serving["framework_version"], "1.12.0",
                     f"currently_serving must carry the framework, got {serving!r}")
        th.assert_eq(serving["converged_at"], newer.finished.isoformat(),
                     f"currently_serving must date the convergence, got {serving!r}")
        th.assert_true(any(item["id"] == str(failed.pk) for item in value["items"]),
                       "the failed attempt must stay visible in items while "
                       "currently_serving answers what actually runs")
        coordination = value["coordination"]
        th.assert_eq(coordination["state"], "migrating",
                     f"the lease state must survive, got {coordination!r}")
        th.assert_eq(coordination["sha"], "c" * 40,
                     f"the lease must say WHICH sha it coordinates, got {coordination!r}")
        th.assert_eq(coordination["at"], "2026-08-19T00:00:00+00:00",
                     f"the lease must say since when, got {coordination!r}")

        with mock.patch.object(admin_platform, "_redis_client") as redis_ctx:
            pipe = redis_ctx.return_value.__enter__.return_value.pipeline.return_value
            pipe.execute.return_value = [None, None]
            PlatformDeployment.objects.filter(
                status="converged").update(status="superseded")
            empty = admin_platform._deployments()
        th.assert_eq(empty["currently_serving"], None,
                     "a fleet with no converged attempt must say so, not guess")
        th.assert_eq(empty["coordination"]["sha"], None,
                     "an empty lease must read back as absent, not invented")
    finally:
        PlatformDeployment.objects.all().delete()


@th.django_unit_test("the framework hold endpoint demands a fresh interactive superuser")
def test_framework_pin_endpoint_authority(opts):
    from mojo.apps.account.models import Setting, User
    from mojo.apps.edge.settings_validators import FRAMEWORK_VERSION_KEY

    Setting.objects.filter(key=FRAMEWORK_VERSION_KEY).delete()
    user = User.objects.get(pk=opts.platform_user)
    user.add_permission("manage_advanced")
    user.save()
    try:
        assert opts.client.login("platform-user@test.com", "example"), \
            "the non-superuser fixture could not establish a session"
        response = opts.client.post(
            "/api/account/admin/advanced/settings", json={"framework_pin": "1.11.6"})
        assert response.status_code in (401, 403), \
            f"a non-superuser wrote the fleet's framework version: {response.response!r}"
        assert not Setting.objects.filter(key=FRAMEWORK_VERSION_KEY).exists(), \
            "a refused write still landed a protected row"
    finally:
        opts.client.logout()
        Setting.objects.filter(key=FRAMEWORK_VERSION_KEY).delete()


def _tail_deployment():
    """One durable attempt whose evidence AND diagnosis carry a stderr tail."""
    import uuid
    from mojo.apps.edge.models import PlatformDeployment
    PlatformDeployment.objects.all().delete()
    return PlatformDeployment.objects.create(
        sha="c" * 40, actor="test", source="test",
        request_key=str(uuid.uuid4()), frozen_roster=["edge-a-engine"],
        status="failed", transitions=[],
        node_evidence=[{
            "runner": "edge-a-engine", "state": "failed",
            "detail": {"phase": "update_script", "exit": 23,
                       "stderr_tail": ["psql: postgres://deploy:hunter2@db/app"]}}],
        diagnosis=[{
            "runner": "edge-a-engine", "at": "2026-08-19T00:00:00+00:00",
            "kind": "failure", "proof": {},
            "detail": {"phase": "update_script", "exit": 23,
                       "stderr_tail": ["psql: postgres://deploy:hunter2@db/app"]}}])


def _viewer(*granted):
    """A request whose user holds exactly the named global permissions."""
    granted = set(granted)
    user = mock.Mock(is_superuser=False)
    user.has_permission.side_effect = lambda perms: bool(granted & set(perms))
    return mock.Mock(user=user)


@th.django_unit_test("read-only platform viewers never receive the deploy stderr tail")
def test_stderr_tail_hidden_from_read_only_viewer(opts):
    """The tail can survive redaction with a credential in it, so it belongs to
    the security tier — while the rest of node_evidence stays visible to the
    read-only role that is meant to see deploy state."""
    from mojo.apps.account.services import admin_platform

    _tail_deployment()
    request = _viewer("view_platform")

    overview = admin_platform.platform_overview(request)
    section = overview["sections"]["deployments"]
    # The section status tracks the deployment's own health (this fixture is a
    # failed attempt); what matters here is that it rendered at all.
    th.assert_true(section["status"] != "unauthorized",
                   "a view_platform viewer must still get the deployments section")
    assert "stderr_tail" not in str(section), \
        "view_platform alone must not reveal the deploy stderr tail"
    detail = section["data"]["items"][0]["node_evidence"][0]["detail"]
    th.assert_eq(detail["phase"], "update_script",
                 "the benign evidence detail must survive for read-only viewers")
    story = section["data"]["items"][0]["diagnosis"][0]["detail"]
    th.assert_eq(story["phase"], "update_script",
                 "the diagnosis story must stay readable at view_platform")
    th.assert_true("stderr_tail" not in story,
                   f"the diagnosis-borne tail leaked below the security tier: {story!r}")

    dashboard = _run_dashboard(request, _dashboard_deployment=_REAL)
    assert "stderr_tail" not in str(dashboard["sources"]["last_deployment"]), \
        "the dashboard's last_deployment must be gated the same way"


@th.django_unit_test("the platform overview honors a sections allowlist without widening authority")
def test_platform_overview_sections_filter(opts):
    from mojo.apps.account.services import admin_platform

    all_sections = {"api", "fleet", "jobs", "sanity", "database", "redis",
                    "deployments", "certificates", "security", "webapps"}
    seen = {}

    def record(request, specs):
        seen["specs"] = dict(specs)
        return {name: {"status": "healthy"} for name in specs}

    def overview(sections=None):
        request = _viewer("view_platform", "view_platform_security")
        request.DATA = {} if sections is None else {"sections": sections}
        return admin_platform.platform_overview(request)

    with mock.patch.object(admin_platform, "_section_map", side_effect=record):
        bare = overview()
        assert set(bare["sections"]) == all_sections, \
            f"a bare overview call no longer collects the full roster: {set(bare['sections'])}"

        narrowed = overview("deployments,api")
        assert set(narrowed["sections"]) == {"deployments", "api"}, \
            f"the sections allowlist did not narrow the roster: {set(narrowed['sections'])}"
        assert set(seen["specs"]) == {"deployments", "api"}, \
            "excluded sections still reached the collector pool — the webapps probe fan-out survives"
        assert seen["specs"]["api"][0] == ("view_platform", "manage_platform", "admin"), \
            "narrowing changed a section's permission tuple"

        typo = overview("deployments,api,bogus")
        assert set(typo["sections"]) == {"deployments", "api"}, \
            f"an unknown section name was not ignored: {set(typo['sections'])}"

    # One unmocked narrowed read through the real pool: only the two cheap
    # local collectors run, and each still returns its bounded envelope.
    live = overview("database,redis")
    assert set(live["sections"]) == {"database", "redis"}, \
        f"the live narrowed overview collected the wrong sections: {set(live['sections'])}"
    for name in ("database", "redis"):
        assert live["sections"][name]["status"] in (
            "healthy", "unhealthy", "timeout", "unavailable"), \
            f"the narrowed {name} section lost its envelope: {live['sections'][name]}"


@th.django_unit_test("security and deploy authority read the tail on Platform, never on Dashboard")
def test_stderr_tail_visible_to_privileged_tiers(opts):
    """Platform is where deploy diagnostics live. The Dashboard row is a sha,
    an outcome, and a time — it projects node evidence away entirely, so the
    tail is unreachable there for EVERY role rather than gated per role."""
    from mojo.apps.account.services import admin_platform

    _tail_deployment()
    for granted in (("view_platform", "view_platform_security"),
                    ("view_platform", "manage_platform")):
        request = _viewer(*granted)
        overview = admin_platform.platform_overview(request)
        section = overview["sections"]["deployments"]
        assert "hunter2" in str(section), \
            f"{granted} must still read the full diagnostic tail"
        tail = section["data"]["items"][0]["diagnosis"][0]["detail"]["stderr_tail"]
        assert "hunter2" in tail[0], \
            f"{granted} must read the diagnosis-borne tail verbatim: {tail!r}"
        dashboard = _run_dashboard(request, _dashboard_deployment=_REAL)
        rendered = str(dashboard["sources"]["last_deployment"])
        assert "hunter2" not in rendered and "stderr_tail" not in rendered, \
            f"{granted} reached the deploy stderr tail through the Dashboard"
        assert dashboard["sources"]["last_deployment"]["data"]["items"][0]["sha"], \
            f"{granted} lost the deployment row along with the tail"


# ---------------------------------------------------------------------------
# Dashboard (schema 2)
# ---------------------------------------------------------------------------

# Marks a collector that must run for real in a given dashboard assertion.
_REAL = object()


def _run_dashboard(request, attention=None, **overrides):
    """dashboard_overview with every provider-touching collector stubbed.

    Only the source under test does real work. Without this, one dashboard
    assertion would depend on live AWS, a PyPI request, and a public HTTPS
    probe — none of which this page's logic is about.
    """
    from contextlib import ExitStack
    from mojo.apps.account.services import admin_platform

    healthy = {"_collector_status": "healthy"}
    collectors = {
        "_load_balancer": healthy, "_compute": healthy,
        "_dashboard_database": healthy, "_dashboard_cache": healthy,
        "_dashboard_certificates": healthy, "_api": healthy,
        "_framework": healthy, "_dashboard_jobs": healthy,
        "_dashboard_sanity": healthy,
        "_dashboard_deployment": {"_collector_status": "healthy",
                                  "items": [{"id": "1", "sha": "a" * 40,
                                             "status": "converged"}]},
    }
    collectors.update(overrides)
    counts = attention or {"incidents": 0, "tickets": 0}

    def attend(_request, name, permissions):
        value = counts[name]
        if isinstance(value, dict):
            return admin_platform._envelope(
                value.get("status", "degraded"), value.get("data") or {},
                reason=value.get("reason"))
        return admin_platform._envelope(
            "healthy" if not value else "degraded", {"open": value})

    with ExitStack() as stack:
        for name, value in collectors.items():
            if value is _REAL:
                continue
            stack.enter_context(
                mock.patch.object(admin_platform, name, return_value=value))
        stack.enter_context(
            mock.patch.object(admin_platform, "_attention", side_effect=attend))
        return admin_platform.dashboard_overview(request)


def _operator():
    return _viewer("view_platform", "view_security")


@th.django_unit_test("a backlog of open incidents never colours the availability sentence")
def test_dashboard_availability_ignores_attention(opts):
    report = _run_dashboard(_operator(), attention={
        "incidents": {"status": "degraded",
                      "data": {"open": 121, "oldest_age_days": 6}},
        "tickets": 0})

    th.assert_eq(report["availability"]["state"], "ok",
                 f"open work was read as an outage: {report['availability']!r}")
    th.assert_eq(report["availability"]["message"], "Everything is running",
                 "the headline must state availability, not workload")
    th.assert_eq(report["availability"]["down"], [],
                 "an incident backlog put a source on the down list")
    message = report["attention"]["message"]
    th.assert_true("121 incidents need review" in message,
                   f"the backlog is missing from the sub-line: {message!r}")
    th.assert_true("nothing is down" in message,
                   f"the sub-line must say the system is still up: {message!r}")


@th.django_unit_test("a failed availability source is named in the headline")
def test_dashboard_availability_names_failure(opts):
    report = _run_dashboard(_operator(), _dashboard_cache={
        "_collector_status": "unhealthy", "_collector_reason": "cache_unreachable",
        "reachable": False})

    th.assert_eq(report["availability"]["state"], "down",
                 f"an unreachable cache did not redden the page: {report['availability']!r}")
    th.assert_eq(report["availability"]["down"], ["Elasticache"],
                 "the failing source was not named with its row label")
    th.assert_eq(report["availability"]["message"], "Elasticache is down",
                 f"the headline must name what failed: {report['availability']!r}")


@th.django_unit_test("amber sources never reach the red availability verdict")
def test_dashboard_amber_is_never_red(opts):
    report = _run_dashboard(
        _operator(),
        _framework={"_collector_status": "degraded", "installed": "1.12.3",
                    "latest": "1.13.0", "update_available": True},
        _dashboard_database={"_collector_status": "healthy",
                             "drift": {"available_major": "16.5"}},
        _load_balancer={"_collector_status": "degraded", "registered": 2,
                        "healthy": 1},
        attention={"incidents": {"status": "degraded", "data": {"open": 3}},
                   "tickets": 0})

    th.assert_eq(report["availability"]["state"], "ok",
                 f"degraded evidence was escalated to an outage: {report['availability']!r}")
    th.assert_eq(report["availability"]["down"], [],
                 "only a proven failure may put a source on the down list")
    th.assert_eq(report["sources"]["framework"]["status"], "degraded",
                 "the framework row lost its own amber state")


@th.django_unit_test("a denied source affects only its own row")
def test_dashboard_denied_source_degrades_one_row(opts):
    action = "elasticloadbalancing:DescribeTargetHealth"
    report = _run_dashboard(_operator(), _load_balancer={
        "_collector_status": "unknown", "_collector_reason": "provider_denied",
        "_collector_reason_detail": {"iam_action": action}})

    source = report["sources"]["load_balancer"]
    th.assert_eq(source["status"], "unknown",
                 f"a denied provider call must never be green: {source!r}")
    th.assert_eq(source["reason_detail"], {"iam_action": action},
                 f"the row cannot name the grant to add: {source!r}")
    th.assert_eq(report["sources"]["database"]["status"], "healthy",
                 "one denied source contaminated an unrelated row")
    th.assert_eq(report["availability"]["state"], "ok",
                 "a denial was treated as a proven failure")
    th.assert_eq(report["availability"]["down"], [],
                 "a denied source was listed as down")


def _jobs_evidence(scheduler=True, pending=1, running=0, failed=11701):
    """What the bare _jobs collector returns, stalled or not."""
    return {"_collector_status": "healthy" if scheduler else "unhealthy",
            "scheduler_active": scheduler,
            "jobs": {"pending": pending, "running": running, "failed": failed}}


@th.django_unit_test("a stalled scheduler or a fresh failure is amber, and a big ledger is not")
def test_dashboard_jobs_row_states(opts):
    from mojo.apps.account.services import admin_platform

    def run(scheduler=True, recent=0, failed=11701):
        with mock.patch.object(admin_platform, "_jobs",
                               return_value=_jobs_evidence(scheduler, failed=failed)), \
                mock.patch.object(admin_platform, "_recent_job_failures",
                                  return_value=recent):
            return admin_platform._dashboard_jobs()

    quiet = run()
    th.assert_eq(quiet["_collector_status"], "healthy",
                 f"a running scheduler with no recent failure is green: {quiet!r}")
    th.assert_eq(quiet["_collector_reason"], None,
                 f"a healthy queue must name no reason: {quiet!r}")
    th.assert_eq(quiet["failed_recent"], 0,
                 f"the one-hour window is missing from the payload: {quiet!r}")
    th.assert_eq(quiet["jobs"]["failed"], 11701,
                 "the all-time ledger must survive for the drill-in")

    stalled = run(scheduler=False)
    th.assert_eq(stalled["_collector_status"], "degraded",
                 f"a stalled scheduler must be amber, never red: {stalled!r}")
    th.assert_eq(stalled["_collector_reason"], "scheduler_inactive",
                 f"the stalled scheduler is not named: {stalled!r}")

    failing = run(recent=4)
    th.assert_eq(failing["_collector_status"], "degraded",
                 f"failures inside the window must be amber: {failing!r}")
    th.assert_eq(failing["_collector_reason"], "recent_failures",
                 f"the recent failures are not named: {failing!r}")
    th.assert_eq(failing["failed_recent"], 4,
                 f"the window count did not reach the payload: {failing!r}")

    # A stalled scheduler is what _jobs itself calls unhealthy. The Dashboard
    # deliberately downgrades it: a backed-up queue is not an outage.
    for value in (quiet, stalled, failing, run(scheduler=False, recent=9)):
        th.assert_true(value["_collector_status"] != "unhealthy",
                       f"the jobs row claimed a proven outage: {value!r}")


def _sanity_rows(migrations=True, local=True):
    return {"_collector_status": "healthy", "checks": [
        {"name": "django apps", "ok": True}, {"name": "database", "ok": True},
        {"name": "migrations", "ok": migrations}, {"name": "redis", "ok": True},
        {"name": "local request", "ok": local}],
        "local_target_source": "configured_static",
        "migration_check": migrations}


@th.django_unit_test("an inferred local-request failure never reaches the Dashboard")
def test_dashboard_sanity_suppresses_unproven_local_request(opts):
    from mojo.apps.account.services import admin_platform

    def run(source, **rows):
        value = _sanity_rows(**rows)
        value["local_target_source"] = source
        with mock.patch.object(admin_platform, "_redis_client"), \
                mock.patch.object(admin_platform, "_sanity", return_value=value):
            return admin_platform._dashboard_sanity()

    inferred = run("default_80", local=False)
    names = [row["name"] for row in inferred["checks"]]
    th.assert_true("local request" not in names,
                   f"an unproven local target was still shown as a check: {names!r}")
    th.assert_eq(inferred["_collector_status"], "healthy",
                 f"a check nobody could prove reddened the row: {inferred!r}")

    configured = run("configured_static", local=False)
    names = [row["name"] for row in configured["checks"]]
    th.assert_true("local request" in names,
                   f"a configured local target lost its check: {names!r}")
    th.assert_eq(configured["_collector_status"], "degraded",
                 f"a configured local target that failed stayed green: {configured!r}")

    failing = run("default_80", migrations=False)
    th.assert_eq(failing["_collector_status"], "degraded",
                 f"a failing migration check must be amber: {failing!r}")
    th.assert_true(failing["_collector_status"] != "unhealthy",
                   "a node sanity check claimed a proven outage")
    th.assert_eq(failing["migration_check"], False,
                 f"the projection dropped the migration fact: {failing!r}")


@th.django_unit_test("the Dashboard's sanity checks run against a bounded Redis client")
def test_dashboard_sanity_uses_bounded_redis(opts):
    from mojo.apps.account.services import admin_platform
    from mojo.apps.edge.services import sanity

    bounded = mock.Mock(name="bounded-redis")
    seen = {}

    def record(options=None, **kwargs):
        seen["options"] = dict(options or {})
        return [{"name": "database", "ok": True, "detail": "ready"}]

    with mock.patch("mojo.helpers.redis.get_bounded_connection",
                    return_value=bounded) as factory, \
            mock.patch.object(sanity, "run", side_effect=record):
        admin_platform._dashboard_sanity()

    th.assert_true(factory.called,
                   "the Dashboard's sanity read used the process-wide Redis client")
    budget = factory.call_args.kwargs.get("timeout")
    th.assert_true(budget is not None and budget <= 1.0,
                   f"the bounded client was built without a small budget: {budget!r}")
    th.assert_true(seen["options"].get("redis_client") is bounded,
                   f"the bounded client never reached the check: {seen['options']!r}")
    th.assert_true(bounded.close.called,
                   "the bounded client was left open after the collector returned")

    # And the seam itself: an injected client is what gets pinged.
    stub = mock.Mock()
    sanity.check_redis({"redis_client": stub})
    th.assert_true(stub.ping.called,
                   "check_redis ignored the injected client and reached for its own")


@th.django_unit_test("jobs and sanity can never redden the availability headline")
def test_dashboard_jobs_and_sanity_never_redden_availability(opts):
    from mojo.apps.account.services import admin_platform

    th.assert_eq(admin_platform.AVAILABILITY_SOURCES,
                 ("load_balancer", "compute", "database", "cache", "certificates",
                  "public_api"),
                 "the availability roster changed — a new source can now redden "
                 "the page")
    for name in ("jobs", "sanity"):
        th.assert_true(name not in admin_platform.AVAILABILITY_SOURCES,
                       f"{name} became an availability source")

    report = _run_dashboard(
        _operator(),
        _dashboard_jobs={"_collector_status": "degraded",
                         "_collector_reason": "scheduler_inactive",
                         "scheduler_active": False, "failed_recent": 4,
                         "jobs": {"pending": 37, "running": 0, "failed": 11705}},
        _dashboard_sanity={"_collector_status": "degraded",
                           "_collector_reason": "sanity_check_failed",
                           "checks": [{"name": "migrations", "ok": False}],
                           "local_target_source": "configured_static"})

    th.assert_eq(report["availability"]["state"], "ok",
                 f"a stalled queue or failing check was read as an outage: "
                 f"{report['availability']!r}")
    th.assert_eq(report["availability"]["down"], [],
                 "jobs or sanity put a source on the down list")
    for name in ("jobs", "sanity"):
        th.assert_eq(report["sources"][name]["status"], "degraded",
                     f"the {name} row lost its own amber state")


@th.django_unit_test("the email source is permissioned evidence, never availability")
def test_dashboard_email_source(opts):
    from mojo.apps.account.services import admin_platform

    th.assert_true("email" not in admin_platform.AVAILABILITY_SOURCES,
                   "email became an availability source")

    degraded = {"_collector_status": "degraded",
                "_collector_reason": "no_sendable_domain",
                "configured": True, "domains": 1, "sendable_domains": 0}
    report = _run_dashboard(_viewer("view_platform", "manage_aws"),
                            _dashboard_email=degraded)
    source = report["sources"]["email"]
    th.assert_eq(source["status"], "degraded",
                 f"the email envelope did not carry the collector state: {source}")
    th.assert_eq(source["data"].get("configured"), True,
                 f"the email envelope lost its payload: {source}")
    th.assert_eq(report["availability"]["state"], "ok",
                 f"a broken email setup was read as an outage: "
                 f"{report['availability']!r}")

    denied = _run_dashboard(_operator(), _dashboard_email=degraded)
    th.assert_eq(denied["sources"]["email"]["status"], "permission_denied",
                 "a caller without the email tier still read the email source")


@th.django_unit_test("the Platform overview payload is unchanged by the Dashboard's wrappers")
def test_platform_overview_payload_unchanged(opts):
    from mojo.apps.account.services import admin_platform
    from mojo.apps.edge.services import sanity

    with mock.patch.object(admin_platform, "_redis_client"):
        jobs = admin_platform._jobs()
    th.assert_eq(set(jobs), {"_collector_status", "scheduler_active", "jobs"},
                 f"the Platform jobs section grew or lost a key: {set(jobs)!r}")
    th.assert_true("failed_recent" not in jobs,
                   "the Dashboard's one-hour window leaked into Platform's payload")

    seen = {}

    def record(options=None, **kwargs):
        seen["options"] = dict(options or {})
        return [{"name": "database", "ok": True, "detail": "ready"}]

    with mock.patch.object(sanity, "run", side_effect=record):
        value = admin_platform._sanity()
    th.assert_eq(set(value), {"_collector_status", "checks", "local_target_source",
                              "migration_check"},
                 f"the Platform sanity section grew or lost a key: {set(value)!r}")
    th.assert_true("redis_client" not in seen["options"],
                   "the bare Platform collector started injecting a client")


@th.django_unit_test("engine drift reaches the RDS row, and stops being relevant on its own")
def test_version_drift_surfaced_on_rds_row(opts):
    from datetime import timedelta
    from django.utils import timezone
    from mojo.apps.account.services import admin_platform
    from mojo.apps.aws.services import version_drift
    from mojo.apps.incident.models import Event

    finding = {"kind": "rds", "resource_id": "mojo-db", "engine": "postgres",
               "current_version": "16.3", "available_major": "16.5",
               "deadline": "2027-02-28T00:00:00+00:00",
               "note": "standard support ends"}
    row = {"metadata": {"findings": [finding]}}

    # The projection is exercised through the query seam: the Event table is
    # shared with tests/test_aws/version_drift.py, which files and deletes
    # rows in this exact category.
    with mock.patch.object(admin_platform, "_newest_drift_event", return_value=row):
        surfaced = admin_platform._version_drift_finding(("rds", "aurora"), "16.3")
        moved = admin_platform._version_drift_finding(("rds", "aurora"), "16.5")
        other_kind = admin_platform._version_drift_finding(("elasticache",), "16.3")
    th.assert_eq(surfaced, {"available_major": "16.5",
                            "deadline": "2027-02-28T00:00:00+00:00",
                            "note": "standard support ends"},
                 f"the RDS row did not receive the published upgrade: {surfaced!r}")
    th.assert_eq(moved, None,
                 "a finding about a version the engine no longer runs stayed amber")
    th.assert_eq(other_kind, None,
                 "an RDS finding leaked onto a cache row")

    with mock.patch.object(admin_platform, "_newest_drift_event", return_value=None):
        th.assert_eq(admin_platform._version_drift_finding(("rds",)), None,
                     "no event must mean no finding, not a crash")
    with mock.patch.object(admin_platform, "_newest_drift_event",
                           return_value={"metadata": {"findings": "not-a-list"}}):
        th.assert_eq(admin_platform._version_drift_finding(("rds",)), None,
                     "malformed event metadata must degrade to no finding")

    # Freshness is the query's job, so that half is asserted against the real
    # table — by proving a backdated row is NOT what comes back, which holds
    # whatever else the shared category currently contains.
    marker = "dm-2146-drift-window"
    event = Event.objects.create(
        category=version_drift.CATEGORY, level=8, title="drift", details="drift",
        metadata={"marker": marker, "findings": [finding]})
    try:
        Event.objects.filter(pk=event.pk).update(
            created=timezone.now() - timedelta(days=4))
        newest = admin_platform._newest_drift_event() or {}
        th.assert_true(
            (newest.get("metadata") or {}).get("marker") != marker,
            "a drift finding older than the freshness window was still believed")
    finally:
        Event.objects.filter(pk=event.pk).delete()


@th.django_unit_test("a raw instance count never proves an outage")
def test_compute_never_unhealthy_without_target_evidence(opts):
    from mojo.apps.account.services import admin_platform

    frontend = {"configured": False, "balancer": None, "groups": [],
                "registered": 0, "healthy": 0, "instance_ids": [],
                "healthy_instance_ids": [], "denied": [], "failures": [],
                "truncated": False}
    helper = mock.Mock()
    helper.ec2.describe_instances.return_value = {"Reservations": [
        {"Instances": [{"InstanceId": "i-01"}, {"InstanceId": "i-02"}]}]}
    with mock.patch.object(admin_platform, "_cached_frontend", return_value=frontend), \
            mock.patch("mojo.helpers.aws.elbv2.LoadBalancerHelper",
                       return_value=helper):
        running = admin_platform._compute()
        helper.ec2.describe_instances.return_value = {"Reservations": []}
        none_running = admin_platform._compute()

    th.assert_eq(running["_collector_status"], "healthy",
                 f"running instances with no balancer are not a failure: {running!r}")
    th.assert_eq(running["total"], 2, f"the instance count is wrong: {running!r}")
    th.assert_eq(running["source"], "ec2",
                 "the fallback must declare it did not use target-group evidence")
    th.assert_true(none_running["_collector_status"] != "unhealthy",
                   f"a bare instance count claimed an outage: {none_running!r}")
    th.assert_eq(none_running["_collector_status"], "unconfigured",
                 f"no instances and no balancer is absence, not failure: {none_running!r}")
    filters = helper.ec2.describe_instances.call_args.kwargs["Filters"]
    th.assert_eq(filters, [{"Name": "instance-state-name", "Values": ["running"]}],
                 f"the fallback counted stopped instances too: {filters!r}")


def _framework_request(user):
    """A request whose user is exactly the given superuser flag."""
    return mock.Mock(user=user, META={})


def _framework_status(installed="1.12.3", latest="1.13.0", pin=None,
                      update_available=None):
    pin = pin or {"mode": "latest", "value": None}
    if update_available is None:
        update_available = bool(latest and pin["mode"] == "latest" and latest != installed)
    return {"installed": installed, "latest": latest,
            "update_available": update_available, "checked_at": "2026-08-18T00:00:00Z",
            "source": "pypi", "pin": pin}


@th.django_unit_test("the framework overview names the one thing blocking an update")
def test_framework_overview_blocked_reasons(opts):
    from mojo.apps.account.services import admin_platform
    from mojo.apps.edge.services import framework_version, platform_deploy

    root = mock.Mock(is_superuser=True)
    plain = mock.Mock(is_superuser=False)
    plain.has_permission.return_value = True

    with mock.patch.object(framework_version, "status",
                           return_value=_framework_status()), \
            mock.patch.object(platform_deploy, "last_converged_deployment",
                              return_value=None):
        blocked = admin_platform.framework_overview(_framework_request(root))
    assert blocked["can_update"] is False, "an unconverged fleet offered an update"
    assert blocked["blocked_reason"] == "no_converged_deployment", \
        f"the block is not named correctly: {blocked['blocked_reason']}"

    row = mock.Mock(pk="row")
    with mock.patch.object(framework_version, "status",
                           return_value=_framework_status()), \
            mock.patch.object(platform_deploy, "last_converged_deployment",
                              return_value=row):
        ready = admin_platform.framework_overview(_framework_request(root))
    assert ready["can_update"] is True and ready["blocked_reason"] is None, \
        f"a converged fleet with a newer release was still blocked: {ready}"
    assert ready["latest"] == "1.13.0" and ready["installed"] == "1.12.3", \
        f"the overview does not report both versions: {ready}"

    # A pin is clearable, but only by a literal superuser — so a non-superuser
    # is told that, rather than offered a control that would be refused.
    pinned = _framework_status(pin={"mode": "pinned", "value": "1.12.0"})
    with mock.patch.object(framework_version, "status", return_value=pinned), \
            mock.patch.object(platform_deploy, "last_converged_deployment",
                              return_value=row):
        refused = admin_platform.framework_overview(_framework_request(plain))
        allowed = admin_platform.framework_overview(_framework_request(root))
    assert refused["blocked_reason"] == "requires_superuser", \
        f"a pinned fleet did not name the superuser requirement: {refused}"
    assert allowed["can_update"] is True, \
        f"a superuser was refused the pin clear-and-update: {allowed}"

    # Nothing newer published is not a block worth explaining twice.
    current = _framework_status(latest="1.12.3")
    with mock.patch.object(framework_version, "status", return_value=current), \
            mock.patch.object(platform_deploy, "last_converged_deployment",
                              return_value=row):
        level = admin_platform.framework_overview(_framework_request(root))
    assert level["blocked_reason"] == "update_unavailable", \
        f"an up-to-date fleet reported {level['blocked_reason']}"


@th.django_unit_test("a framework update CLEARS the pin and redeploys the converged commit")
def test_framework_update_clears_pin_and_redeploys(opts):
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import admin_platform
    from mojo.apps.edge.services import deploy, platform_deploy
    from mojo.apps.edge.settings_validators import FRAMEWORK_VERSION_KEY

    root = User.objects.get(pk=opts.platform_root)
    Setting.objects.filter(key=FRAMEWORK_VERSION_KEY).delete()
    request = mock.Mock(user=root, META={})
    row = mock.Mock(pk="converged-row", sha="a" * 40)
    made = mock.Mock(pk="new-row")
    try:
        from mojo.apps.account.services import system_settings
        system_settings.set_value(root, FRAMEWORK_VERSION_KEY, "1.12.0")
        with mock.patch.object(platform_deploy, "last_converged_deployment",
                               return_value=row), \
                mock.patch.object(platform_deploy, "same_sha_retry",
                                  return_value=made) as retry, \
                mock.patch.object(platform_deploy, "serialize", return_value={"id": "new-row"}), \
                mock.patch.object(admin_platform, "audit_after_commit") as audit:
            result = admin_platform.apply_framework_update(request, "1.13.0")

        assert deploy.framework_version_pin() == "", \
            "the pin survived the update, so the fleet would reinstall the old version"
        assert not Setting.objects.filter(
            key=FRAMEWORK_VERSION_KEY).exclude(value="").exists(), \
            "a version was written INTO the pin instead of clearing it"
        assert retry.call_count == 1, f"the converged commit was not redeployed ({retry.call_count})"
        assert retry.call_args[0][0] is row, \
            "the redeploy did not use the last converged deployment row"
        assert retry.call_args.kwargs["created_by"] is root, \
            "the redeploy is not attributed to the operator who asked for it"
        assert result["cleared_pin"] == "1.12.0" and result["version"] == "1.13.0", \
            f"the result does not report the transition: {result}"

        actions = [call[0][1] for call in audit.call_args_list]
        assert "framework_pin" in actions and "framework_update" in actions, \
            f"the pin clear and the update were not both audited: {actions}"
        target = [call[0][2] for call in audit.call_args_list
                  if call[0][1] == "framework_update"][0]
        assert "1.13.0" in str(target), \
            f"the audit target does not carry the version: {target!r}"
    finally:
        Setting.objects.filter(key=FRAMEWORK_VERSION_KEY).delete()


@th.django_unit_test("an unpinned framework update never touches protected settings")
def test_framework_update_leaves_an_unpinned_fleet_alone(opts):
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import admin_platform, system_settings
    from mojo.apps.edge.services import platform_deploy
    from mojo.apps.edge.settings_validators import FRAMEWORK_VERSION_KEY

    root = User.objects.get(pk=opts.platform_root)
    Setting.objects.filter(key=FRAMEWORK_VERSION_KEY).delete()
    request = mock.Mock(user=root, META={})
    row = mock.Mock(pk="converged-row", sha="b" * 40)
    try:
        with mock.patch.object(platform_deploy, "last_converged_deployment",
                               return_value=row), \
                mock.patch.object(platform_deploy, "same_sha_retry",
                                  return_value=mock.Mock(pk="new-row")), \
                mock.patch.object(platform_deploy, "serialize", return_value={}), \
                mock.patch.object(system_settings, "set_value") as writer, \
                mock.patch.object(admin_platform, "audit_after_commit"):
            result = admin_platform.apply_framework_update(request, "1.13.0")
        assert writer.call_count == 0, \
            "an unpinned fleet still wrote a protected setting"
        assert result["cleared_pin"] is None, \
            f"an unpinned fleet reported clearing a pin: {result}"
    finally:
        Setting.objects.filter(key=FRAMEWORK_VERSION_KEY).delete()


@th.django_unit_test("a fleet that never converged refuses the update instead of guessing")
def test_framework_update_requires_a_converged_commit(opts):
    from mojo import errors as me
    from mojo.apps.account.models import User
    from mojo.apps.account.services import admin_platform
    from mojo.apps.edge.services import platform_deploy

    root = User.objects.get(pk=opts.platform_root)
    request = mock.Mock(user=root, META={})
    with mock.patch.object(platform_deploy, "last_converged_deployment",
                           return_value=None), \
            mock.patch.object(platform_deploy, "same_sha_retry") as retry, \
            mock.patch.object(admin_platform, "audit_after_commit"):
        try:
            admin_platform.apply_framework_update(request, "1.13.0")
            raise AssertionError("an unconverged fleet was redeployed anyway")
        except me.ValueException as err:
            assert err.status == 409, f"the refusal is not a 409: {err.status}"
    assert retry.call_count == 0, "a refused update still queued a deployment"


@th.django_unit_test("a non-superuser cannot clear the pin through the update path")
def test_framework_update_pin_clear_is_superuser_only(opts):
    from mojo import errors as me
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import admin_platform, system_settings
    from mojo.apps.edge.services import platform_deploy
    from mojo.apps.edge.settings_validators import FRAMEWORK_VERSION_KEY

    root = User.objects.get(pk=opts.platform_root)
    user = User.objects.get(pk=opts.platform_user)
    Setting.objects.filter(key=FRAMEWORK_VERSION_KEY).delete()
    try:
        system_settings.set_value(root, FRAMEWORK_VERSION_KEY, "1.12.0")
        request = mock.Mock(user=user, META={})
        with mock.patch.object(platform_deploy, "last_converged_deployment",
                               return_value=mock.Mock(pk="row", sha="c" * 40)), \
                mock.patch.object(platform_deploy, "same_sha_retry") as retry, \
                mock.patch.object(admin_platform, "audit_after_commit"):
            with th.assert_raises(me.PermissionDeniedException):
                admin_platform.apply_framework_update(request, "1.13.0")
        assert retry.call_count == 0, \
            "the refused pin clear still queued a deployment"
        from mojo.apps.edge.services import deploy
        assert deploy.framework_version_pin() == "1.12.0", \
            "the pin was cleared by a caller the writer should have refused"
    finally:
        Setting.objects.filter(key=FRAMEWORK_VERSION_KEY).delete()


# ── INFRASTRUCTURE_MODE ─────────────────────────────────────────────────────

@th.django_unit_test("an external installation refuses the framework update with a named 403")
def test_framework_update_refused_in_external_mode(opts):
    import json
    import inspect
    from objict import objict
    from mojo.helpers import infrastructure
    from mojo.apps.account.models import User
    from mojo.apps.account.rest import admin_platform as views
    from mojo.apps.account.services import admin_platform

    root = User.objects.get(pk=opts.platform_root)
    view = inspect.unwrap(views.on_admin_platform_framework_update)
    request = mock.Mock(user=root, META={},
                        DATA=objict(version="1.13.0", confirm_version="1.13.0"))
    with _override_setting("INFRASTRUCTURE_MODE", "external"):
        with mock.patch.object(admin_platform, "apply_framework_update") as applied:
            response = view(request)
        assert applied.call_count == 0, \
            "an external installation still reached the framework update service"
    assert response.status_code == 403, \
        f"the refusal is not a 403: {response.status_code}"
    payload = json.loads(response.content.decode())
    assert payload["error_code"] == infrastructure.ERROR_CODE, \
        f"the refusal does not carry its documented code: {payload}"
    assert payload["data"] == {"mode": "external", "setting": "INFRASTRUCTURE_MODE"}, \
        f"the refusal does not name the mode and the setting: {payload}"


@th.django_unit_test("the framework update service refuses an external installation on its own")
def test_framework_update_service_backstop(opts):
    from mojo import errors as me
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import admin_platform, system_settings
    from mojo.apps.edge.services import deploy, platform_deploy
    from mojo.apps.edge.settings_validators import FRAMEWORK_VERSION_KEY

    root = User.objects.get(pk=opts.platform_root)
    Setting.objects.filter(key=FRAMEWORK_VERSION_KEY).delete()
    request = mock.Mock(user=root, META={})
    try:
        system_settings.set_value(root, FRAMEWORK_VERSION_KEY, "1.12.0")
        with _override_setting("INFRASTRUCTURE_MODE", "external"):
            with mock.patch.object(platform_deploy, "last_converged_deployment",
                                   return_value=mock.Mock(pk="row", sha="d" * 40)), \
                    mock.patch.object(platform_deploy, "same_sha_retry") as retry, \
                    mock.patch.object(admin_platform, "audit_after_commit"):
                with th.assert_raises(me.PermissionDeniedException):
                    admin_platform.apply_framework_update(request, "1.13.0")
                assert retry.call_count == 0, \
                    "the refused update still queued a deployment"
        assert deploy.framework_version_pin() == "1.12.0", \
            "the refused update still cleared the fleet's version pin"
    finally:
        Setting.objects.filter(key=FRAMEWORK_VERSION_KEY).delete()


@th.django_unit_test("external mode blocks the overview's update without hiding the version facts")
def test_framework_overview_blocked_reason_external(opts):
    from mojo.apps.account.services import admin_platform
    from mojo.apps.edge.services import framework_version, platform_deploy

    root = mock.Mock(is_superuser=True)
    row = mock.Mock(pk="row")
    with mock.patch.object(framework_version, "status",
                           return_value=_framework_status()), \
            mock.patch.object(platform_deploy, "last_converged_deployment",
                              return_value=row):
        managed = admin_platform.framework_overview(_framework_request(root))
        with _override_setting("INFRASTRUCTURE_MODE", "external"):
            external = admin_platform.framework_overview(_framework_request(root))

    assert managed["can_update"] is True and managed["blocked_reason"] is None, \
        f"the managed control case is not offering an update: {managed}"
    assert external["can_update"] is False, \
        "an external installation still offered a framework update"
    assert external["blocked_reason"] == "infrastructure_external", \
        f"the block is not named as the mode: {external['blocked_reason']}"
    # The read itself is never gated: the facts a page reports must stay true.
    assert external["installed"] == "1.12.3" and external["latest"] == "1.13.0", \
        f"external mode hid the version facts: {external}"
    assert external["pin"] == {"mode": "latest", "value": None}, \
        f"external mode hid the pin: {external['pin']}"
    assert external["update_available"] is True, \
        "external mode pretended nothing newer is published"
