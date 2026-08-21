"""
OAuth tests that mutate django.conf.settings in the test process — moved out
of tests/test_oauth/oauth.py into this opt-in serial package (maestro item
#1839). Process-global settings mutation races parallel test threads even
with a try/finally restore.
"""
from testit import helpers as th

PROVIDER = "google"


@th.django_unit_test("oauth: OAUTH_ALLOW_REGISTRATION=False blocks new user creation")
def test_oauth_registration_gate(opts):
    from django.conf import settings as django_settings
    from mojo.apps.account.models import User
    from mojo.apps.account.rest.oauth import _find_or_create_user
    from mojo import errors as merrors

    gated_email = "blocked_registration@example.com"
    User.objects.filter(email=gated_email).delete()

    original = getattr(django_settings, "OAUTH_ALLOW_REGISTRATION", True)
    django_settings.OAUTH_ALLOW_REGISTRATION = False
    try:
        profile = {"uid": "google_uid_gated", "email": gated_email, "display_name": "Blocked"}
        raised = False
        try:
            _find_or_create_user(PROVIDER, profile)
        except merrors.PermissionDeniedException:
            raised = True
        assert raised, "Should raise PermissionDeniedException when registration is disabled"
        assert not User.objects.filter(email=gated_email).exists(), "User should not have been created"
    finally:
        django_settings.OAUTH_ALLOW_REGISTRATION = original
