"""Admin bootstrap tests that override django.conf.settings in-process.

Moved out of tests/test_account/test_admin_portal.py (maestro item #1839):
these tests mutate process-global Django settings via setattr/delattr, which
is unsafe under the parallel default tier. The rest of the Admin delivery,
feature-bootstrap, and asset-boundary coverage stays in the source module.
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


ADMIN_EMAIL = "admin_portal_serial@test.com"
ADMIN_PASSWORD = "Admin_portal_serial_pw_99"


@th.django_unit_setup()
def setup_admin_portal_serial(opts):
    from django.core.cache import cache
    from mojo.apps.account.models import User

    cache.clear()
    User.objects.filter(email=ADMIN_EMAIL).delete()
    user = User.objects.create_user(username=ADMIN_EMAIL, email=ADMIN_EMAIL,
                                    password=ADMIN_PASSWORD)
    user.display_name = "Portal Admin (serial)"
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.is_superuser = True
    user.save()
    opts.portal_user_id = user.pk


def _bootstrap(opts):
    import inspect
    from mojo.apps.account.models import User
    from mojo.apps.account.rest import admin_portal as views

    user = User.objects.get(pk=opts.portal_user_id)
    return inspect.unwrap(views.on_admin_bootstrap)(mock.Mock(user=user))


@th.django_unit_test("bootstrap publishes the infrastructure mode as a fact and a capability")
def test_bootstrap_publishes_infrastructure_mode(opts):
    managed = _bootstrap(opts)
    with _override_setting("INFRASTRUCTURE_MODE", "external"):
        external = _bootstrap(opts)

    assert managed["infrastructure"] == {"mode": "managed", "managed": True}, \
        f"a default installation is not published as managed: {managed['infrastructure']}"
    assert managed["capabilities"]["infrastructure_managed"] is True, \
        "the managed capability is missing from a default installation"
    assert external["infrastructure"] == {"mode": "external", "managed": False}, \
        f"external mode is not published: {external['infrastructure']}"
    assert external["capabilities"]["infrastructure_managed"] is False, \
        "the capability did not flip with the mode"

    # The feature validator accepts named booleans only, so the mode STRING
    # must never ride inside a feature's capabilities — it would disable the
    # whole lane rather than describe it.
    for payload in (managed, external):
        for name, feature in payload["features"].items():
            for key, value in feature["capabilities"].items():
                assert isinstance(value, bool), \
                    f"features.{name}.capabilities.{key} is not a bool: {value!r}"


@th.django_unit_test("bootstrap publishes the file-only edge HTTP posture")
def test_bootstrap_publishes_edge_http_posture(opts):
    from mojo.apps.account.models.setting import Setting

    Setting.set("EDGE_HTTP_ENABLED", True, group=None)
    try:
        with _override_setting("EDGE_HTTP_ENABLED", "false"):
            disabled = _bootstrap(opts)
        with _override_setting("EDGE_HTTP_ENABLED", "true"):
            enabled = _bootstrap(opts)
    finally:
        Setting.remove("EDGE_HTTP_ENABLED", group=None)

    assert disabled["edge"] == {
        "available": True, "http_enabled": False,
        "dnsman_issuance": "dns-01"}, \
        f"file-disabled edge posture is wrong: {disabled['edge']}"
    assert enabled["edge"]["http_enabled"] is True, \
        f"file-enabled edge posture is wrong: {enabled['edge']}"

    from mojo.apps.account.rest import admin_portal as views
    with mock.patch.object(views.apps, "is_installed", return_value=False):
        absent = _bootstrap(opts)
    assert absent["edge"] == {
        "available": False, "http_enabled": None,
        "dnsman_issuance": None}, \
        f"an installation without optional Edge did not degrade safely: {absent['edge']}"
