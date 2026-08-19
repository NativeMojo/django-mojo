"""Admin Platform/Advanced permission, settings, and packaging boundaries."""

from pathlib import Path
from unittest import mock

from testit import helpers as th


ROOT = Path(__file__).resolve().parents[2]


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


@th.django_unit_test("ignored incidents are excluded from every open rollup")
def test_open_incident_predicate(opts):
    from mojo.apps.account.services import admin_platform
    from mojo.apps.incident.models import Incident
    category = "admin-platform-open-contract"
    Incident.objects.filter(category=category).delete()
    for status in ("new", "investigating", "ignored", "resolved", "closed"):
        Incident.objects.create(category=category, status=status)
    statuses = set(admin_platform._open_incidents().filter(
        category=category).values_list("status", flat=True))
    Incident.objects.filter(category=category).delete()
    assert statuses == {"new", "investigating"}, \
        f"terminal incidents leaked into the open predicate: {statuses!r}"


@th.django_unit_test("WebApp current health is independent from onboarding history")
def test_webapp_current_health_axes(opts):
    from mojo.apps.account.services import admin_platform
    healthy_not_started = [{
        "onboarding": {"status": "not_started"},
        "deployment_key": {"active": False},
        "current_health": {"status": "healthy"},
    }]
    unknown_succeeded = [{
        "onboarding": {"status": "succeeded"},
        "deployment_key": {"active": True},
        "current_health": {"status": "unknown"},
    }]
    assert admin_platform._webapp_collector_status(healthy_not_started) == "healthy", \
        "historical onboarding incorrectly degraded current public health"
    assert admin_platform._webapp_collector_status(unknown_succeeded) == "degraded", \
        "historical success incorrectly made unknown current health green"
    assert admin_platform._webapp_collector_status(
        healthy_not_started, truncated=True) == "degraded", \
        "a capped WebApp collector can still report unprobed rows as green"


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


@th.django_unit_test("denied sections do not execute their collector")
def test_denied_collector_not_run(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import admin_platform
    user = User.objects.get(pk=opts.platform_user)
    request = mock.Mock(user=user)
    collector = mock.Mock(side_effect=AssertionError("collector ran"))
    result = admin_platform._guarded(request, ("view_platform",), collector)
    assert result["status"] == "unauthorized"
    assert not collector.called


@th.django_unit_test("collector failures expose a stable reason, never provider text")
def test_collector_error_sanitized(opts):
    from mojo.apps.account.services import admin_platform
    result = admin_platform._collect(
        lambda: (_ for _ in ()).throw(RuntimeError("token=fixture-secret")))
    assert result["status"] == "unavailable"
    assert result["reason"] == "collector_unavailable"
    assert "fixture-secret" not in str(result)


@th.django_unit_test("Platform and Advanced assets remain feature-owned and previewed")
def test_platform_feature_package(opts):
    from mojo.apps.account.services import admin_assets
    assets = admin_assets.load_manifest()
    required = {
        "assets/features/platform/feature.js", "assets/features/platform/page.js",
        "assets/features/platform/styles.css", "assets/features/advanced/feature.js",
        "assets/features/advanced/page.js", "assets/features/advanced/styles.css",
    }
    assert required <= set(assets)
    platform = (ROOT / "mojo/apps/account/admin_portal/assets/features/platform/feature.js").read_text()
    advanced = (ROOT / "mojo/apps/account/admin_portal/assets/features/advanced/feature.js").read_text()
    preview = (ROOT / "bin/admin_preview_support/server.py").read_text()
    assert "deployments" in platform and "platformPage" in platform
    assert "advancedControlPage" in advanced and "domains" in advanced
    assert "features import activity, advanced, platform" in preview


@th.django_unit_test("all platform writes declare key denial and fixed fresh auth")
def test_platform_write_decorators(opts):
    from mojo.apps.account.rest import admin_platform as views
    funcs = (
        views.on_admin_platform_retry, views.on_admin_platform_verify,
        views.on_admin_platform_converge, views.on_admin_advanced_settings)
    for func in funcs:
        assert getattr(func, "_mojo_denies_key_backed_session", False), func.__name__
        assert getattr(func, "_mojo_requires_fresh_auth", False), func.__name__
        assert getattr(func, "_mojo_fresh_auth_seconds", None) == 600, func.__name__


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
    """One durable attempt whose evidence carries a deploy stderr tail."""
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

    dashboard = _run_dashboard(request, _dashboard_deployment=_REAL)
    assert "stderr_tail" not in str(dashboard["sources"]["last_deployment"]), \
        "the dashboard's last_deployment must be gated the same way"


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
        "_framework": healthy,
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


@th.django_unit_test("the incident row reports how long the oldest item has waited")
def test_attention_oldest_age(opts):
    from datetime import timedelta
    from django.utils import timezone
    from mojo.apps.account.services import admin_platform
    from mojo.apps.incident.models import Incident

    category = "admin-dashboard-oldest"
    Incident.objects.filter(category=category).delete()
    incident = Incident.objects.create(category=category, status="new")
    request = mock.Mock(user=mock.Mock(is_superuser=True))
    try:
        Incident.objects.filter(pk=incident.pk).update(
            created=timezone.now() - timedelta(days=6, hours=1))
        result = admin_platform._attention(request, "incidents", ("view_security",))
        data = result["data"]
        th.assert_eq(result["status"], "degraded",
                     f"an open incident must ask for attention: {result!r}")
        th.assert_true(data["open"] >= 1,
                       f"the open incident was not counted: {data!r}")
        th.assert_true(data["oldest_created"] is not None,
                       f"the row cannot say how long anything has waited: {data!r}")
        th.assert_true(data["oldest_age_days"] >= 6,
                       f"a six-day-old incident was reported as newer: {data!r}")
    finally:
        Incident.objects.filter(category=category).delete()


@th.django_unit_test("no managed certificate is 'not managed here', never a failure")
def test_certificates_empty_is_unconfigured(opts):
    from mojo.apps.account.services import admin_platform

    with mock.patch.object(admin_platform, "_certificates", return_value={
            "counts": {}, "expiring_within_30_days": 0}):
        empty = admin_platform._dashboard_certificates()
    th.assert_eq(empty["_collector_status"], "unconfigured",
                 f"an empty certificate table was read as health: {empty!r}")
    th.assert_eq(empty["total"], 0, "an empty table reported certificates")

    with mock.patch.object(admin_platform, "_certificates", return_value={
            "counts": {"active": 2, "revoked": 1}, "expiring_within_30_days": 0}):
        failing = admin_platform._dashboard_certificates()
    th.assert_eq(failing["_collector_status"], "unhealthy",
                 f"a revoked certificate did not redden the row: {failing!r}")
    th.assert_eq(failing["failing"], 1, f"the failing count is wrong: {failing!r}")


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
