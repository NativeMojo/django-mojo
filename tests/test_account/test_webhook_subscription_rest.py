"""REST CRUD round-trip and auth tests for /api/group/webhook_subscriptions."""
from testit import helpers as th


ADMIN_USER = "wsub_rest_admin@test.com"
ADMIN_PWORD = "wsub_rest_admin_pw_99"
GROUP_NAME = "wsub_rest_group"


@th.django_unit_setup()
def setup_rest(opts):
    from mojo.apps.account.models import (
        ApiKey, User, Group, GroupMember, WebhookSubscription,
    )

    WebhookSubscription.objects.filter(url__contains="rest.example.test").delete()
    ApiKey.objects.filter(name__startswith="wsub_rest_").delete()
    Group.objects.filter(name=GROUP_NAME).delete()
    User.objects.filter(email=ADMIN_USER).delete()

    admin = User.objects.create_user(username=ADMIN_USER, email=ADMIN_USER, password=ADMIN_PWORD)
    admin.is_active = True
    admin.is_email_verified = True
    admin.requires_mfa = False
    admin.save()
    admin.add_permission(["manage_group", "manage_groups"])
    admin.save()

    g = Group.objects.create(name=GROUP_NAME, kind="organization")
    GroupMember.objects.get_or_create(
        user=admin, group=g, defaults={"permissions": {"manage_group": True}},
    )
    opts.admin_id = admin.pk
    opts.group_id = g.pk


@th.django_unit_test()
def test_rest_crud_round_trip(opts):
    """POST → GET list → GET detail → PUT → GET (reflects update) → DELETE → GET 404."""
    from mojo.apps.account.models import WebhookSubscription

    opts.client.login(ADMIN_USER, ADMIN_PWORD)

    # POST — create
    r_create = opts.client.post("/api/group/webhook_subscriptions", {
        "group": opts.group_id,
        "url": "https://rest.example.test/hook",
        "events": ["evt.created"],
    })
    assert r_create.status_code == 200, (
        f"POST create must 200, got {r_create.status_code}: {r_create.response}"
    )
    sub_id = r_create.response.data.id
    assert isinstance(sub_id, int), f"created sub must have an int id, got {sub_id!r}"

    # GET list — must include the created row
    r_list = opts.client.get("/api/group/webhook_subscriptions", params={"group": opts.group_id})
    assert r_list.status_code == 200, f"GET list must 200, got {r_list.status_code}"
    list_ids = [row["id"] for row in r_list.response.data]
    assert sub_id in list_ids, f"new subscription id {sub_id} must appear in list, got {list_ids!r}"

    # GET detail
    r_detail = opts.client.get(
        f"/api/group/webhook_subscriptions/{sub_id}", params={"group": opts.group_id},
    )
    assert r_detail.status_code == 200, f"GET detail must 200, got {r_detail.status_code}"
    assert r_detail.response.data.url == "https://rest.example.test/hook", "url must round-trip on detail"
    assert r_detail.response.data.is_active is True, "is_active must default True on detail"

    # PUT — disable
    r_put = opts.client.post(
        f"/api/group/webhook_subscriptions/{sub_id}",
        {"group": opts.group_id, "is_active": False},
    )
    assert r_put.status_code == 200, (
        f"PUT (POST update) must 200, got {r_put.status_code}: {r_put.response}"
    )
    assert r_put.response.data.is_active is False, "is_active must reflect update"

    # GET — verify the update persisted
    r_after = opts.client.get(
        f"/api/group/webhook_subscriptions/{sub_id}", params={"group": opts.group_id},
    )
    assert r_after.response.data.is_active is False, "GET must reflect the persisted update"

    # DELETE
    r_del = opts.client.delete(
        f"/api/group/webhook_subscriptions/{sub_id}", params={"group": opts.group_id},
    )
    assert r_del.status_code == 200, f"DELETE must 200, got {r_del.status_code}: {r_del.response}"
    assert not WebhookSubscription.objects.filter(pk=sub_id).exists(), (
        "row must be removed from DB after DELETE"
    )

    # GET after DELETE — 404
    r_gone = opts.client.get(
        f"/api/group/webhook_subscriptions/{sub_id}", params={"group": opts.group_id},
    )
    assert r_gone.status_code in (404, 403), (
        f"GET after DELETE must be 404 (or 403 not-found), got {r_gone.status_code}"
    )

    opts.client.logout()


@th.django_unit_test()
def test_rest_under_api_key_auth(opts):
    """A caller authed via 'Authorization: apikey <token>' (key has manage_group)
    can CRUD without passing `group` in the body — request.group is from the key.
    """
    from mojo.apps.account.models import ApiKey, Group, WebhookSubscription

    g = Group.objects.get(pk=opts.group_id)
    api_key, raw_token = ApiKey.create_for_group(
        group=g,
        name="wsub_rest_apikey",
        permissions={"manage_group": True},
    )

    # Switch the testit client to api-key auth.
    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = raw_token
    opts.client.is_authenticated = True

    # Create without `group` in body — should resolve from request.group set by the key.
    r_create = opts.client.post("/api/group/webhook_subscriptions", {
        "url": "https://rest.example.test/apikey-hook",
        "events": ["evt.apikey"],
    })
    assert r_create.status_code == 200, (
        f"API-key auth POST must 200, got {r_create.status_code}: {r_create.response}"
    )
    sub_id = r_create.response.data.id
    assert sub_id, "created sub must have an id under API-key auth"

    # Verify the row was attached to the API key's Group, not some other.
    sub = WebhookSubscription.objects.get(pk=sub_id)
    assert sub.group_id == g.pk, (
        f"API-key auth must scope to the key's group ({g.pk}), got {sub.group_id}"
    )

    # Cleanup
    sub.delete()
    api_key.delete()
    opts.client.logout()
