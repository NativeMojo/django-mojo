"""Live wiring smoke test for the settings-injected database reader.

The reader points at the same per-checkout PostgreSQL database as ``default``.
That proves a real server can boot and serve authenticated writes and reads with
the injected alias/router/middleware, but it cannot distinguish correct routing
from inverted routing. The in-process router tests own those semantics.

If unrelated modules fail only during this module's brief ``server_settings``
window, suspect the reader router first because the live server temporarily
runs every request through it.
"""

from testit import helpers as th


USERNAME = "db_reader_admin"
PASSWORD = "db_reader_admin##99"
GROUP_NAME = "db-reader-live-smoke"


@th.django_unit_setup()
def setup_live_reader(opts):
    from mojo.apps.account.models import Group, User

    Group.objects.filter(name=GROUP_NAME).delete()
    User.objects.filter(username=USERNAME).delete()

    admin = User(
        username=USERNAME,
        display_name="DB Reader Admin",
        email="db-reader-admin@example.com",
        is_staff=True,
        is_superuser=True,
    )
    admin.save()
    admin.is_email_verified = True
    admin.save_password(PASSWORD)
    admin.add_permission(["manage_groups", "manage_users", "view_global", "view_admin"])


@th.django_unit_test("reader wiring: live server boots and serves write/read flow")
def test_live_reader_wiring(opts):
    with th.server_settings(DATABASE_READER_HOST="localhost"):
        assert opts.client.login(USERNAME, PASSWORD), \
            "the reader-enabled server must authenticate against primary"

        created = opts.client.post("/api/group", {"name": GROUP_NAME})
        assert created.status_code == 200, \
            f"reader-enabled POST must create a group, got {created.status_code}: {created.text}"
        group_id = created.response.data.id

        detail = opts.client.get(f"/api/group/{group_id}")
        assert detail.status_code == 200, \
            f"an immediate detail GET must succeed, got {detail.status_code}: {detail.text}"
        assert detail.response.data.id == group_id, \
            f"the immediate GET returned the wrong group: {detail.response!r}"

        listing = opts.client.get("/api/group", params={"id": group_id})
        assert listing.status_code == 200, \
            f"a plain reader-backed list GET must succeed, got {listing.status_code}: {listing.text}"
        assert listing.response.count == 1, \
            f"the reader-backed list must include the created group, got {listing.response!r}"

        opts.client.logout()
