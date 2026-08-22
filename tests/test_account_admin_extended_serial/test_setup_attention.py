"""System Setup attention badge — capability matrix on the Admin bootstrap.

Moved from tests/test_account/test_admin_portal_integration.py (maestro item
#2558): the matrix mock.patches system_settings.get_value, a process-global
attribute on the shared mojo.apps.account.services.system_settings module, so
it runs only in this opt-in serial tier. The patch is deliberate — driving the
matrix through the shared BASE_URL Setting row instead races the parallel
modules (test_system_setup, aws_check) that create and delete that global row
at will.
"""
import inspect
from unittest import mock

from testit import helpers as th


ADMIN_EMAIL = "setup_attention_admin@test.com"
ADMIN_PASSWORD = "Setup_attention_admin_pw_99"
ATTENTION_EMAIL = "setup_attention_reader@test.com"
ATTENTION_PASSWORD = "Setup_attention_reader_pw_99"


@th.django_unit_setup()
def setup_setup_attention(opts):
    from mojo.apps.account.models import User

    User.objects.filter(email__in=(ADMIN_EMAIL, ATTENTION_EMAIL)).delete()
    admin = User.objects.create_user(
        username=ADMIN_EMAIL, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)
    admin.is_active = True
    admin.is_email_verified = True
    admin.requires_mfa = False
    admin.is_superuser = True
    admin.save()
    opts.attention_admin = admin.pk


@th.django_unit_test("the System Setup badge tracks the one thing Setup is still needed for")
def test_setup_attention_matches_dashboard_link(opts):
    """System Setup left the page grid, so the sidebar entry has to carry the
    reason to open it: a superuser looking at an installation with no public
    address. Nobody else is ever badged.

    The BASE_URL read is patched in-process rather than driven through the
    shared Setting row: parallel modules (test_system_setup, aws_check) create
    and delete that global row at will, so an HTTP-level assertion on its
    absence fails or passes by scheduling. The capability computation is what
    this test owns; HTTP delivery of bootstrap is covered elsewhere."""
    from mojo.apps.account.models import User
    from mojo.apps.account.rest import admin_portal as views
    from mojo.apps.account.services import system_settings

    admin = User.objects.get(pk=opts.attention_admin)
    User.objects.filter(email=ATTENTION_EMAIL).delete()
    reader = User.objects.create_user(
        username=ATTENTION_EMAIL, email=ATTENTION_EMAIL, password=ATTENTION_PASSWORD)
    reader.is_active = True
    reader.is_email_verified = True
    reader.requires_mfa = False
    reader.save()
    reader.add_permission("view_admin")

    bootstrap = inspect.unwrap(views.on_admin_bootstrap)
    real_get_value = system_settings.get_value

    def _with_base_url(value, user):
        def fake(key, *args, **kwargs):
            if key == system_settings.BASE_URL:
                return value
            return real_get_value(key, *args, **kwargs)
        with mock.patch.object(system_settings, "get_value", side_effect=fake):
            return bootstrap(mock.Mock(user=user))["capabilities"]

    try:
        # No public address yet: the badge is on for the superuser.
        unset = _with_base_url(None, admin)
        th.assert_eq(unset["setup_attention"], True,
                     f"an installation with no BASE_URL is not badged: {unset!r}")
        th.assert_eq(unset["setup"], True,
                     "a superuser lost the System Setup destination itself")

        # Same installation, a non-superuser: no destination, so no badge.
        plain = _with_base_url(None, reader)
        th.assert_eq(plain["setup_attention"], False,
                     f"a non-superuser was badged for Setup: {plain!r}")
        th.assert_eq(plain["setup"], False,
                     f"a non-superuser was offered System Setup: {plain!r}")

        # Configured installation: the badge goes away for everyone.
        configured = _with_base_url("https://admin-attention.example.com", admin)
        th.assert_eq(configured["setup_attention"], False,
                     f"a configured installation is still badged: {configured!r}")
        th.assert_eq(configured["setup"], True,
                     "a superuser lost System Setup once BASE_URL was set")
    finally:
        User.objects.filter(email=ATTENTION_EMAIL).delete()
