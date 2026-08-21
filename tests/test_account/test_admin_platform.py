"""Admin Platform/Advanced read-only contracts (default tier).

Read-only half of the original module: pure collector projections, asset
and decorator contracts, and rollups over test-owned rows. The mutation
and patching matrices — AUTH_CONFIG writers, the framework pin, dashboard
collector stubs — moved to
tests/test_account_admin_extended_serial/test_admin_platform.py (maestro
item #1839) because they patch production module attributes and write
protected Setting rows, which is unsafe under the parallel default tier.
"""

from pathlib import Path
from unittest import mock

from testit import helpers as th


ROOT = Path(__file__).resolve().parents[2]


@th.django_unit_setup()
def setup_admin_platform(opts):
    from mojo.apps.account.models import User
    User.objects.filter(username__in=("platform-root", "platform-user")).delete()
    root = User.objects.create_user(
        email="platform-root@test.com", username="platform-root", password="example")
    root.is_active = True
    root.is_superuser = True
    root.save()
    user = User.objects.create_user(
        email="platform-user@test.com", username="platform-user", password="example")
    user.is_active = True
    user.save()
    opts.platform_root = root.pk
    opts.platform_user = user.pk


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
        "assets/features/webapps/api.js",
    }
    assert required <= set(assets)
    platform = (ROOT / "mojo/apps/account/admin_portal/assets/features/platform/feature.js").read_text()
    platform_page = (ROOT / "mojo/apps/account/admin_portal/assets/features/platform/page.js").read_text()
    webapps = (ROOT / "mojo/apps/account/admin_portal/assets/features/webapps/feature.js").read_text()
    advanced = (ROOT / "mojo/apps/account/admin_portal/assets/features/advanced/feature.js").read_text()
    advanced_page = (ROOT / "mojo/apps/account/admin_portal/assets/features/advanced/page.js").read_text()
    preview = (ROOT / "bin/admin_preview_support/server.py").read_text()
    assert "routes: ['setup', 'metrics', 'maintenance']" in platform, \
        "Platform is no longer exactly Setup, Metrics and Maintenance"
    assert "platformPage" not in platform and "platformPage" not in platform_page, \
        "the dissolved Platform health page came back"
    assert "platformDestinations" not in platform_page, \
        "Platform still renders a destination grid instead of primary navigation"
    assert "deployments" not in platform, \
        "the deployments route crept back into the Platform descriptor"
    assert "deployments" in webapps, \
        "the merged Deployments lane does not claim the deployments route"
    # `id: 'advanced'` stays — the feature still owns the hosting routes. What
    # must be gone is the route itself and the page it rendered.
    assert "const ROUTES = ['domains'," in advanced \
        and "advancedControlPage" not in advanced \
        and "advancedControlPage" not in advanced_page, \
        "the raw Advanced diagnostics destination came back"
    assert "domains" in advanced, \
        "Advanced lost the Domains & DNS destination it still owns"
    # Named individually rather than as one contiguous string: the import list
    # is alphabetical, so a new sibling provider used to break this assertion
    # without breaking anything it was protecting.
    for provider in ("activity", "advanced", "platform", "maintenance"):
        assert f"from .features import" in preview and provider in preview, \
            f"the preview server does not import the {provider} provider"


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


@th.django_unit_test("certificate health reflects the latest attempt for each name set")
def test_certificates_use_current_name_set_state(opts):
    from mojo.apps.account.services import admin_platform

    rows = [
        {"id": 8, "domain_id": 1, "common_name": "example.test",
         "sans": ["example.test", "*.example.test"], "status": "active"},
        {"id": 7, "domain_id": 1, "common_name": "example.test",
         "sans": ["*.example.test", "example.test"], "status": "failed"},
        {"id": 6, "domain_id": 2, "common_name": "other.test",
         "sans": ["other.test"], "status": "revoked"},
    ]
    current = admin_platform._current_certificate_rows(rows)
    th.assert_eq([row["id"] for row in current], [8, 6],
                 f"historical attempts still counted as current: {current!r}")

    counts = admin_platform._certificate_counts(current)
    th.assert_eq(counts, {"active": 1, "revoked": 1},
                 f"current-state counts are wrong: {counts!r}")


@th.django_unit_test("the framework update endpoint declares key denial and fixed fresh auth")
def test_framework_update_decorators(opts):
    from mojo.apps.account.rest import admin_platform as views

    func = views.on_admin_platform_framework_update
    assert getattr(func, "_mojo_denies_key_backed_session", False), \
        "the framework update accepts key-backed sessions"
    assert getattr(func, "_mojo_requires_fresh_auth", False), \
        "the framework update does not require fresh auth"
    assert getattr(func, "_mojo_fresh_auth_seconds", None) == 600, \
        "the framework update does not pin its fresh-auth window"
