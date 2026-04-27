"""
The aggregate ``auth:failures`` metric counter is bumped exactly once per
recorded auth-failure event so the portal Security Dashboard can render
"failed-auth attempts" with one fetch instead of composing categories
client-side.
"""
from contextlib import contextmanager
from testit import helpers as th


@contextmanager
def incident_metrics_enabled():
    """In-process toggle for INCIDENT_EVENT_METRICS.

    The test calls ``report_event()`` directly (no server hop), so
    ``th.server_settings`` does not apply. We patch django.conf.settings
    in-place and restore it on exit. Single-threaded test process, so this
    is safe.
    """
    from django.conf import settings as django_settings
    sentinel = object()
    prev = getattr(django_settings, "INCIDENT_EVENT_METRICS", sentinel)
    django_settings.INCIDENT_EVENT_METRICS = True
    try:
        yield
    finally:
        if prev is sentinel:
            try:
                del django_settings.INCIDENT_EVENT_METRICS
            except AttributeError:
                pass
        else:
            django_settings.INCIDENT_EVENT_METRICS = prev


@th.django_unit_setup()
def setup_auth_failures(opts):
    from mojo.apps.incident.models import Event
    Event.objects.filter(category__in=[
        "invalid_password", "login:unknown", "totp:login_failed",
        "totp:login_unknown", "passkey:login_failed", "login",
    ]).delete()


@th.django_unit_test()
def test_tracked_categories_set(opts):
    """The constant lists every failure category we want on the counter."""
    from mojo.apps.incident.models.event import AUTH_FAILURE_CATEGORIES

    expected = {
        "invalid_password",
        "login:unknown",
        "totp:login_failed",
        "totp:login_unknown",
        "passkey:login_failed",
    }
    assert AUTH_FAILURE_CATEGORIES == expected, \
        f"AUTH_FAILURE_CATEGORIES drifted from spec: {AUTH_FAILURE_CATEGORIES} vs {expected}"


@th.django_unit_test()
def test_auth_failures_counter_bumps_for_tracked_categories(opts):
    from mojo.apps import metrics
    from mojo.apps.incident import report_event

    with incident_metrics_enabled():
        before = metrics.fetch_values(
            ["auth:failures"], granularity="hours", account="incident"
        )["data"]["auth:failures"]

        for category in (
            "invalid_password",
            "login:unknown",
            "totp:login_failed",
            "totp:login_unknown",
            "passkey:login_failed",
        ):
            report_event(f"failure for {category}", category=category, level=4)

        after = metrics.fetch_values(
            ["auth:failures"], granularity="hours", account="incident"
        )["data"]["auth:failures"]

    assert after - before == 5, \
        f"auth:failures should have bumped by 5 (one per tracked category), got {after - before}"


@th.django_unit_test()
def test_auth_failures_counter_skips_other_categories(opts):
    """Successful logins and unrelated events must not bump auth:failures."""
    from mojo.apps import metrics
    from mojo.apps.incident import report_event

    with incident_metrics_enabled():
        before = metrics.fetch_values(
            ["auth:failures"], granularity="hours", account="incident"
        )["data"]["auth:failures"]

        report_event("successful login", category="login", level=1)
        report_event("password reset", category="password_reset", level=2)
        report_event("api error", category="api_error", level=4)

        after = metrics.fetch_values(
            ["auth:failures"], granularity="hours", account="incident"
        )["data"]["auth:failures"]

    assert after == before, \
        f"auth:failures must NOT bump for non-failure categories — got {after - before} new"


@th.django_unit_test()
def test_auth_failures_increments_only_once_per_event(opts):
    """One report_event call → exactly one increment, regardless of bundling."""
    from mojo.apps import metrics
    from mojo.apps.incident import report_event

    with incident_metrics_enabled():
        before = metrics.fetch_values(
            ["auth:failures"], granularity="hours", account="incident"
        )["data"]["auth:failures"]

        report_event("single failure", category="invalid_password", level=4)

        after = metrics.fetch_values(
            ["auth:failures"], granularity="hours", account="incident"
        )["data"]["auth:failures"]

    assert after - before == 1, \
        f"Expected exactly +1 on auth:failures, got {after - before}"
