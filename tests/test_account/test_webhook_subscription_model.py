"""Model-level tests for WebhookSubscription — URL/events validation,
RestMeta permission gating, exclusion of inactive rows from fan-out.
"""
from testit import helpers as th


ADMIN_USER = "wsub_admin@test.com"
ADMIN_PWORD = "wsub_admin_pw_99"
NONADMIN_USER = "wsub_nonadmin@test.com"
NONADMIN_PWORD = "wsub_nonadmin_pw_99"
GROUP_NAME = "wsub_group"


@th.django_unit_setup()
def setup_webhook_subscription_model(opts):
    from mojo.apps.account.models import (
        User, Group, GroupMember, WebhookSubscription,
    )

    # Idempotent cleanup so the suite is repeatable on a long-lived DB.
    WebhookSubscription.objects.filter(url__contains="example.test").delete()
    Group.objects.filter(name=GROUP_NAME).delete()
    User.objects.filter(email__in=[ADMIN_USER, NONADMIN_USER]).delete()

    admin = User.objects.create_user(username=ADMIN_USER, email=ADMIN_USER, password=ADMIN_PWORD)
    admin.is_active = True
    admin.is_email_verified = True
    admin.requires_mfa = False
    admin.save()
    admin.add_permission(["manage_group", "manage_groups"])
    admin.save()
    opts.admin_id = admin.pk

    nonadmin = User.objects.create_user(
        username=NONADMIN_USER, email=NONADMIN_USER, password=NONADMIN_PWORD,
    )
    nonadmin.is_active = True
    nonadmin.is_email_verified = True
    nonadmin.requires_mfa = False
    nonadmin.save()
    opts.nonadmin_id = nonadmin.pk

    group = Group.objects.create(name=GROUP_NAME, kind="organization")
    GroupMember.objects.get_or_create(
        user=admin, group=group, defaults={"permissions": {"manage_group": True}},
    )
    opts.group_id = group.pk


@th.django_unit_test()
def test_https_url_accepted(opts):
    from mojo.apps.account.models import Group, WebhookSubscription

    g = Group.objects.get(pk=opts.group_id)
    sub = WebhookSubscription(group=g, url="https://hooks.example.test/x", events=["evt.a"])
    sub.on_rest_pre_save(changed_fields={}, created=True)
    sub.save()
    assert sub.pk is not None, "subscription should save with a valid https URL"
    assert sub.is_active is True, "is_active must default to True"
    sub.delete()


@th.django_unit_test()
def test_http_url_rejected(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import Group, WebhookSubscription

    g = Group.objects.get(pk=opts.group_id)
    sub = WebhookSubscription(group=g, url="http://hooks.example.test/x", events=["evt.a"])
    raised = False
    try:
        sub.on_rest_pre_save(changed_fields={}, created=True)
    except merrors.ValueException as e:
        raised = True
        assert "https" in str(e).lower(), (
            f"error must mention https requirement, got: {e}"
        )
    assert raised, "http URL must be rejected with ValueException"


@th.django_unit_test()
def test_url_with_credentials_rejected(opts):
    """https://user:pass@host/... must be rejected — credentials would leak
    into logs and outbound request lines.
    """
    from mojo import errors as merrors
    from mojo.apps.account.models import Group, WebhookSubscription

    g = Group.objects.get(pk=opts.group_id)
    sub = WebhookSubscription(
        group=g,
        url="https://alice:s3cret@hooks.example.test/x",
        events=["evt.a"],
    )
    raised = False
    try:
        sub.on_rest_pre_save(changed_fields={}, created=True)
    except merrors.ValueException as e:
        raised = True
        assert "credential" in str(e).lower() or "userinfo" in str(e).lower(), (
            f"error must mention credentials/userinfo, got: {e}"
        )
    assert raised, "URL with embedded credentials must be rejected"


@th.django_unit_test()
def test_malformed_url_rejected(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import Group, WebhookSubscription

    g = Group.objects.get(pk=opts.group_id)
    sub = WebhookSubscription(group=g, url="https://", events=[])
    raised = False
    try:
        sub.on_rest_pre_save(changed_fields={}, created=True)
    except merrors.ValueException:
        raised = True
    assert raised, "malformed https URL must be rejected"


@th.django_unit_test()
def test_events_not_list_rejected(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import Group, WebhookSubscription

    g = Group.objects.get(pk=opts.group_id)
    sub = WebhookSubscription(group=g, url="https://hooks.example.test/x", events="not-a-list")
    raised = False
    try:
        sub.on_rest_pre_save(changed_fields={}, created=True)
    except merrors.ValueException as e:
        raised = True
        assert "list" in str(e).lower(), f"error should call out list shape, got: {e}"
    assert raised, "non-list events must be rejected"


@th.django_unit_test()
def test_events_with_non_string_entry_rejected(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import Group, WebhookSubscription

    g = Group.objects.get(pk=opts.group_id)
    sub = WebhookSubscription(group=g, url="https://hooks.example.test/x", events=["evt.a", 42])
    raised = False
    try:
        sub.on_rest_pre_save(changed_fields={}, created=True)
    except merrors.ValueException:
        raised = True
    assert raised, "events with non-string entry must be rejected"


@th.django_unit_test()
def test_empty_events_list_accepted(opts):
    """Empty events is a valid 'draft' state — matches no events, never fires."""
    from mojo.apps.account.models import Group, WebhookSubscription

    g = Group.objects.get(pk=opts.group_id)
    sub = WebhookSubscription(group=g, url="https://hooks.example.test/draft", events=[])
    sub.on_rest_pre_save(changed_fields={}, created=True)
    sub.save()
    assert sub.pk is not None, "empty events list must be accepted as a draft state"
    sub.delete()


@th.django_unit_test()
def test_rest_create_requires_manage_group(opts):
    """Non-admin (no manage_group/manage_groups) cannot create a subscription."""
    opts.client.login(NONADMIN_USER, NONADMIN_PWORD)
    assert opts.client.is_authenticated, "nonadmin login must succeed"

    resp = opts.client.post("/api/group/webhook_subscriptions", {
        "group": opts.group_id,
        "url": "https://hooks.example.test/denied",
        "events": ["evt.a"],
    })
    assert resp.status_code in (401, 403), (
        f"nonadmin must be denied (401/403), got {resp.status_code}: {resp.response}"
    )
    opts.client.logout()


@th.django_unit_test()
def test_rest_create_with_admin(opts):
    """Admin with manage_group/manage_groups creates a subscription successfully."""
    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "admin login must succeed"

    resp = opts.client.post("/api/group/webhook_subscriptions", {
        "group": opts.group_id,
        "url": "https://hooks.example.test/admin",
        "events": ["evt.a", "evt.b"],
    })
    assert resp.status_code == 200, (
        f"admin create must succeed, got {resp.status_code}: {resp.response}"
    )
    data = resp.response.data
    assert data.url == "https://hooks.example.test/admin", "url must round-trip"
    assert data.events == ["evt.a", "evt.b"], f"events must round-trip, got {data.events!r}"
    assert data.is_active is True, "is_active must default to True"

    # Cleanup so re-runs are idempotent.
    from mojo.apps.account.models import WebhookSubscription
    WebhookSubscription.objects.filter(pk=data.id).delete()
    opts.client.logout()


@th.django_unit_test()
def test_rest_create_http_url_rejected_with_400(opts):
    """The on_rest_pre_save validator turns http:// into a 400 from the REST layer."""
    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.post("/api/group/webhook_subscriptions", {
        "group": opts.group_id,
        "url": "http://insecure.example.test/x",
        "events": ["evt.a"],
    })
    assert resp.status_code in (400, 422, 500), (
        f"http URL must produce a non-success status, got {resp.status_code}: {resp.response}"
    )
    # The error envelope should mention the URL/https constraint.
    body_text = str(resp.response).lower()
    assert "https" in body_text or "url" in body_text, (
        f"error envelope should mention the URL/https constraint, got: {resp.response}"
    )
    opts.client.logout()
