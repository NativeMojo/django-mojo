"""Built-in Admin WebApp reveal-once key lifecycle smoke coverage."""

import uuid
from unittest import mock

from testit import helpers as th


ADMIN_EMAIL = "admin_portal_webapps@test.com"
ADMIN_PASSWORD = "Admin_portal_webapps_pw_99"


@th.django_unit_setup()
def setup_admin_portal_webapps(opts):
    from mojo.apps.account.models import User

    User.objects.filter(email=ADMIN_EMAIL).delete()
    user = User.objects.create_user(username=ADMIN_EMAIL, email=ADMIN_EMAIL,
                                    password=ADMIN_PASSWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.is_superuser = True
    user.save()


@th.django_unit_test("authenticated portal covers missing active rotated and revoked deploy keys")
def test_webapp_key_portal_smoke(opts):
    from mojo.apps.account.models import ApiKey, Group, Setting
    from mojo.apps.edge.models import WebApp

    group_name = "admin_portal_key_lifecycle"
    Group.objects.filter(name=group_name).delete()
    old_buckets, had_old_buckets = Setting.get_from_db("EDGE_RELEASE_BUCKETS")
    Setting.set("EDGE_RELEASE_BUCKETS", ["portal-test"], group=None)
    group = Group.objects.create(name=group_name, kind="organization")
    site = WebApp(group=group, slug="portal-key-smoke", bucket="portal-test", prefix="pending")
    try:
        with mock.patch("mojo.apps.edge.validators.validate_web_app"):
            site.save(); site.prefix = site.storage_prefix(); site.save()
        assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD)
        missing = opts.client.get(f"/api/edge/webapp/key_status?webapp={site.pk}")
        missing_data = (missing.json.get("data") or {}).get("status") or {}
        assert missing.status_code == 200 and missing_data.get("linked") is False
        assert "token" not in missing_data
        minted = opts.client.post("/api/edge/webapp/link_key", json={
            "webapp": site.pk, "action": "mint", "operation_id": str(uuid.uuid4())})
        assert minted.status_code == 200 and (minted.json.get("data") or {}).get("token")
        active = (opts.client.get(f"/api/edge/webapp/key_status?webapp={site.pk}").json.get("data") or {}).get("status") or {}
        assert active.get("linked") is True and active.get("active") is True and "token" not in active
        first_key = active.get("api_key")
        rotated = opts.client.post("/api/edge/webapp/link_key", json={
            "webapp": site.pk, "action": "rotate", "operation_id": str(uuid.uuid4())})
        assert rotated.status_code == 200
        rotated_status = (opts.client.get(f"/api/edge/webapp/key_status?webapp={site.pk}").json.get("data") or {}).get("status") or {}
        assert rotated_status.get("last_action") == "rotate" and rotated_status.get("api_key") != first_key
        assert "token" not in rotated_status
        revoked = opts.client.post("/api/edge/webapp/revoke_key", json={
            "webapp": site.pk, "operation_id": str(uuid.uuid4())})
        assert revoked.status_code == 200
        revoked_status = (opts.client.get(f"/api/edge/webapp/key_status?webapp={site.pk}").json.get("data") or {}).get("status") or {}
        assert revoked_status.get("linked") is False and revoked_status.get("last_action") == "revoke"
        assert "token" not in revoked_status
    finally:
        with mock.patch("mojo.apps.edge.validators.validate_web_app"):
            WebApp.objects.filter(pk=site.pk).delete()
        ApiKey.objects.filter(name="webapp:portal-key-smoke").delete()
        Group.objects.filter(pk=group.pk).delete()
        if had_old_buckets: Setting.set("EDGE_RELEASE_BUCKETS", old_buckets, group=None)
        else: Setting.remove("EDGE_RELEASE_BUCKETS", group=None)
