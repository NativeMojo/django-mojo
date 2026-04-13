"""
Tests for GitHubInstall model — CRUD, secrets, REST permissions.
"""
from testit import helpers as th

ADMIN_USER = "gh_admin"
ADMIN_PWORD = "ghadmin##secret99"

REGULAR_USER = "gh_regular"
REGULAR_PWORD = "ghregular##secret99"


@th.django_unit_setup()
def setup_github_env(opts):
    from mojo.apps.account.models import User
    from mojo.apps.github.models import GitHubInstall

    # Clean up any leftover installs
    GitHubInstall.objects.filter(installation_id__in=[900001, 900002]).delete()

    # Admin user with github permission
    admin = User.objects.filter(username=ADMIN_USER).last()
    if admin is None:
        admin = User(username=ADMIN_USER, email=f"{ADMIN_USER}@example.com")
        admin.save()
    admin.is_active = True
    admin.add_permission(["github", "manage_github"])
    admin.save_password(ADMIN_PWORD)
    opts.admin = admin

    # Regular user without github permission
    regular = User.objects.filter(username=REGULAR_USER).last()
    if regular is None:
        regular = User(username=REGULAR_USER, email=f"{REGULAR_USER}@example.com")
        regular.save()
    regular.is_active = True
    regular.save_password(REGULAR_PWORD)
    opts.regular = regular


@th.django_unit_test("github install: create and read back model")
def test_github_install_create(opts):
    from mojo.apps.github.models import GitHubInstall

    GitHubInstall.objects.filter(installation_id=900001).delete()

    install = GitHubInstall.objects.create(
        installation_id=900001,
        account_name="test-org",
        permissions={"contents": "read"},
        metadata={"app_slug": "test-app"},
    )
    opts.install = install

    assert install.pk is not None, "Install should have a pk after save"
    assert install.installation_id == 900001, "installation_id should be 900001"
    assert install.account_name == "test-org", "account_name should be 'test-org'"
    assert install.permissions == {"contents": "read"}, "permissions should match"
    assert install.group is None, "group should be None for global install"


@th.django_unit_test("github install: secrets roundtrip")
def test_github_install_secrets(opts):
    from mojo.apps.github.models import GitHubInstall

    install = GitHubInstall.objects.filter(installation_id=900001).first()
    assert install is not None, "Install should exist from previous test"

    # Set a secret token
    install.set_secret("token", "ghs_test_token_12345")
    install.save()

    # Refresh from DB and verify decryption
    install.refresh_from_db()
    token = install.get_secret("token")
    assert token == "ghs_test_token_12345", (
        f"Secret token should roundtrip through encryption, got: {token}"
    )


@th.django_unit_test("github install: mojo_secrets excluded from REST output")
def test_github_install_secrets_not_in_rest(opts):
    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "admin authentication failed"

    resp = opts.client.get("/api/github/github_install")
    assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.response}"

    # Check that mojo_secrets is not in any result
    for item in resp.response.data:
        assert "mojo_secrets" not in item, "mojo_secrets should not appear in REST output"


@th.django_unit_test("github install: admin with manage_github can list")
def test_github_install_admin_list(opts):
    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "admin authentication failed"

    resp = opts.client.get("/api/github/github_install")
    assert resp.status_code == 200, f"Admin should be able to list, got {resp.status_code}"
    assert resp.response.count >= 1, "Should have at least one install"


@th.django_unit_test("github install: user without github permission gets 403")
def test_github_install_no_permission(opts):
    resp = opts.client.login(REGULAR_USER, REGULAR_PWORD)
    assert opts.client.is_authenticated, "regular user authentication failed"

    resp = opts.client.get("/api/github/github_install")
    assert resp.status_code == 403, (
        f"User without github permission should get 403, got {resp.status_code}"
    )


@th.django_unit_test("github install: admin can create via REST")
def test_github_install_admin_create(opts):
    from mojo.apps.github.models import GitHubInstall

    GitHubInstall.objects.filter(installation_id=900002).delete()

    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "admin authentication failed"

    resp = opts.client.post("/api/github/github_install", {
        "installation_id": 900002,
        "account_name": "another-org",
    })
    assert resp.status_code == 200, (
        f"Admin should be able to create, got {resp.status_code}: {resp.response}"
    )

    install = GitHubInstall.objects.filter(installation_id=900002).first()
    assert install is not None, "Install should exist after REST create"
    assert install.account_name == "another-org", "account_name should match"


@th.django_unit_test("github install: admin can delete via REST")
def test_github_install_admin_delete(opts):
    from mojo.apps.github.models import GitHubInstall

    install = GitHubInstall.objects.filter(installation_id=900002).first()
    assert install is not None, "Install should exist from previous test"

    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "admin authentication failed"

    resp = opts.client.delete(f"/api/github/github_install/{install.pk}")
    assert resp.status_code == 200, (
        f"Admin should be able to delete, got {resp.status_code}: {resp.response}"
    )
    assert not GitHubInstall.objects.filter(installation_id=900002).exists(), (
        "Install should be deleted"
    )
