"""The Admin asset catch-all must never shadow source-session revocation."""

from testit import helpers as th


TESTIT_TIER = "admin"


@th.django_unit_test("Admin source-session revocation resolves before private assets")
def test_admin_session_literal_route(opts):
    from django.urls import resolve
    from mojo.apps.account.services.admin_portal import ADMIN_PATH

    root = f"/{ADMIN_PATH}"
    match = resolve(root + "/_session")
    assert match.kwargs == {"__mojo_rest_root_key__": f"__absolute__{ADMIN_PATH}/_session"}, (
        f"source-session revocation was shadowed by another route: {match.kwargs}")
    asset = resolve(root + "/assets/app.js")
    assert asset.kwargs.get("asset") == "assets/app.js", "private asset routing changed"
