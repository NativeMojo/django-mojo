"""
Example module demonstrating TestIt patterns:
- use numbered filenames to control ordering
- share state via opts
- keep Django imports inside decorated helpers
- write actionable assertion messages
"""

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


@th.unit_setup()
def setup_shared(opts):
    """Prepare data that does not require Django."""
    opts.account_payload = {"name": "Acme Co", "slug": "acme-co"}
    opts.expected_slug = "acme-co"


@th.django_unit_setup()
def setup_admin(opts):
    """Create reusable Django objects (imports stay local to the function)."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    opts.admin = User.objects.create_superuser(
        username="admin@example.com",
        email="admin@example.com",
        password="secret123",
    )
    opts.credentials = {
        "username": "admin@example.com",
        "password": "secret123",
    }


@th.unit_test("slug is normalized")
def test_slug_generation(opts):
    """Example pure python assertion using shared setup."""
    result = opts.account_payload["name"].lower().replace(" ", "-")
    assert_eq(result, opts.expected_slug, "slugify should normalize account names")


@th.django_unit_test()
def test_admin_can_login(opts):
    """Exercise the real login endpoint rather than mocking business logic."""
    response = opts.client.post("/api/login", json=opts.credentials)
    assert_eq(response.status_code, 200, "admin login must return 200")
    assert_true(response.response.data.access_token, "login response should include JWT")
