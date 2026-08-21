"""
GitHub OAuth provider tests that mutate django.conf.settings in the test
process — moved out of tests/test_oauth/oauth_github.py into this opt-in
serial package (maestro item #1839). Process-global settings mutation races
parallel test threads even with a try/finally restore.
"""
from testit import helpers as th


@th.django_unit_test("github oauth: get_auth_url returns correct GitHub authorize URL")
def test_github_get_auth_url(opts):
    from django.conf import settings as django_settings
    from mojo.apps.account.services.oauth import get_provider

    original = getattr(django_settings, "GITHUB_CLIENT_ID", None)
    django_settings.GITHUB_CLIENT_ID = "test-client-id-123"
    try:
        svc = get_provider("github")
        url = svc.get_auth_url(state="teststate123", redirect_uri="https://example.com/callback")
    finally:
        if original is None:
            try:
                delattr(django_settings, "GITHUB_CLIENT_ID")
            except AttributeError:
                pass
        else:
            django_settings.GITHUB_CLIENT_ID = original

    assert "github.com/login/oauth/authorize" in url, f"URL should point to GitHub, got: {url}"
    assert "test-client-id-123" in url, f"URL should contain client_id, got: {url}"
    assert "teststate123" in url, f"URL should contain state, got: {url}"
    assert "user%3Aemail" in url or "user:email" in url, f"URL should contain user:email scope, got: {url}"
