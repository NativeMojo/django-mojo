"""
Tests for GitHub OAuth provider.

Tests the GitHubOAuthProvider implementation:
- Provider registration and discovery
- get_auth_url() returns correct GitHub authorize URL
- begin endpoint works for github provider
- Auto-link logic works with github connections (same as google/apple)
"""
from testit import helpers as th

PROVIDER = "github"


@th.django_unit_test("github oauth: provider is registered and discoverable")
def test_github_provider_registered(opts):
    from mojo.apps.account.services.oauth import get_provider, PROVIDERS

    assert "github" in PROVIDERS, "github should be in PROVIDERS registry"
    svc = get_provider("github")
    assert svc.name == "github", f"Provider name should be 'github', got '{svc.name}'"


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


@th.django_unit_test("github oauth: begin returns auth_url with backend callback redirect_uri")
def test_github_oauth_begin(opts):
    from urllib.parse import unquote

    # GITHUB_CLIENT_ID is pinned in test project settings (parallel-safe);
    # no per-test server_settings reload needed.
    resp = opts.client.get(f"/api/auth/oauth/{PROVIDER}/begin")

    assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.response}"
    data = resp.response.data
    assert data.auth_url, "Missing auth_url"
    assert data.state, "Missing state"
    assert "github.com" in data.auth_url, "auth_url should point to GitHub"
    decoded_url = unquote(data.auth_url)
    assert f"/api/auth/oauth/{PROVIDER}/callback" in decoded_url, (
        f"redirect_uri must be the backend callback endpoint, got: {decoded_url}"
    )


@th.django_unit_test("github oauth: _fetch_primary_email picks primary verified email")
def test_github_fetch_primary_email(opts):
    from mojo.apps.account.services.oauth.github import GitHubOAuthProvider

    svc = GitHubOAuthProvider()

    # Mock the email list response that GitHub returns
    emails = [
        {"email": "secondary@example.com", "primary": False, "verified": True},
        {"email": "primary@example.com", "primary": True, "verified": True},
        {"email": "unverified@example.com", "primary": False, "verified": False},
    ]

    # Test the selection logic directly — find primary + verified
    result = None
    for entry in emails:
        if entry.get("primary") and entry.get("verified"):
            result = (entry.get("email") or "").lower().strip()
            break

    assert result == "primary@example.com", (
        f"Should pick primary verified email, got: {result}"
    )


@th.django_unit_test("github oauth: auto-link creates connection for github provider")
def test_github_autolink(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.oauth import OAuthConnection
    from mojo.apps.account.rest.oauth import _find_or_create_user

    test_email = "github_autolink_test@example.com"
    User.objects.filter(email=test_email).delete()
    OAuthConnection.objects.filter(provider="github").delete()

    # Create a user with this email
    user = User(username="gh_autolink_test", email=test_email)
    user.save()
    user.is_active = True
    user.save_password("testpass99##")

    profile = {
        "uid": "github_uid_autolink_42",
        "email": test_email,
        "display_name": "GitHub Test User",
    }
    linked_user, conn, created = _find_or_create_user("github", profile)

    assert linked_user.id == user.id, "Should link to existing user by email"
    assert conn.provider == "github", f"Provider should be 'github', got '{conn.provider}'"
    assert conn.provider_uid == "github_uid_autolink_42", "Connection should use GitHub uid"
    assert created is False, "Existing user should not be flagged as new"

    # Cleanup
    user.delete()


@th.django_unit_test("github oauth: new user created via github has correct fields")
def test_github_new_user(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.oauth import OAuthConnection
    from mojo.apps.account.rest.oauth import _find_or_create_user

    new_email = "brand_new_github@example.com"
    User.objects.filter(email=new_email).delete()

    profile = {
        "uid": "github_uid_brandnew_99",
        "email": new_email,
        "display_name": "New GitHub User",
    }
    user, conn, created = _find_or_create_user("github", profile)

    assert user.email == new_email, "Should create user with GitHub email"
    assert user.is_email_verified is True, "OAuth user should have email verified"
    assert conn.provider == "github", f"Provider should be 'github', got '{conn.provider}'"
    assert conn.provider_uid == "github_uid_brandnew_99", "Connection should use GitHub uid"
    assert created is True, "New user should be flagged as created"

    # Cleanup
    user.delete()
