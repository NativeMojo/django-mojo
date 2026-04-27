"""
Tests for /api/incident/health/summary — latest event per system:health:* category.
"""
from testit import helpers as th


@th.django_unit_setup()
def setup_health_summary(opts):
    from mojo.apps.incident.models import Event
    from mojo.apps.account.models import User

    # Clear prior runs.
    Event.objects.filter(category__startswith="system:health:").delete()
    Event.objects.filter(category__startswith="custom:health:").delete()

    pword = "health##mojo99"

    admin = User.objects.filter(username="health_admin").last()
    if admin is None:
        admin = User(username="health_admin", email="health_admin@example.com")
        admin.save()
    admin.is_email_verified = True
    admin.save_password(pword)
    admin.remove_all_permissions()
    admin.add_permission("view_security")

    outsider = User.objects.filter(username="health_outsider").last()
    if outsider is None:
        outsider = User(username="health_outsider", email="health_outsider@example.com")
        outsider.save()
    outsider.is_email_verified = True
    outsider.save_password(pword)
    outsider.remove_all_permissions()

    opts.admin_name = "health_admin"
    opts.outsider_name = "health_outsider"
    opts.pword = pword


@th.django_unit_test()
def test_health_summary_one_row_per_category(opts):
    """Endpoint returns latest event per distinct category, sorted by category name."""
    from mojo.apps.incident.models import Event

    # Two events for runner — only the most recent should appear.
    Event.objects.create(category="system:health:runner", level=4, title="runner old", details="old")
    latest_runner = Event.objects.create(category="system:health:runner", level=10, title="runner latest", details="critical")
    # One event for scheduler.
    latest_scheduler = Event.objects.create(category="system:health:scheduler", level=7, title="scheduler", details="warn")
    # Non-health event must be filtered out.
    Event.objects.create(category="login", level=1, title="ignored", details="login")

    assert opts.client.login(opts.admin_name, opts.pword), "admin login failed"
    resp = opts.client.get("/api/incident/health/summary")
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"

    body = resp.response
    assert body.status is True, f"status should be True: {body}"
    rows = body["data"]
    assert isinstance(rows, list), f"data should be a list, got {type(rows).__name__}"

    # We should see exactly two health categories (runner, scheduler), neither login.
    cats = [row["category"] for row in rows]
    assert "system:health:runner" in cats, f"missing runner: {cats}"
    assert "system:health:scheduler" in cats, f"missing scheduler: {cats}"
    assert "login" not in cats, f"login must be filtered out: {cats}"

    # Sorted by category name.
    assert cats == sorted(cats), f"rows should be sorted by category, got {cats}"

    # Latest event for runner (level=10) wins over the older one (level=4).
    runner = next(r for r in rows if r["category"] == "system:health:runner")
    assert runner["level"] == 10, f"expected latest runner level=10, got {runner['level']}"
    assert runner["title"] == "runner latest", f"expected latest title, got {runner['title']}"
    assert runner["last_seen"], f"last_seen must be present: {runner}"


@th.django_unit_test()
def test_health_summary_empty_returns_empty_list(opts):
    """No matching events → empty list, not an error."""
    from mojo.apps.incident.models import Event

    Event.objects.filter(category__startswith="system:health:").delete()

    assert opts.client.login(opts.admin_name, opts.pword), "admin login failed"
    resp = opts.client.get("/api/incident/health/summary")
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"
    assert resp.response["data"] == [], f"expected empty list, got {resp.response['data']}"


@th.django_unit_test()
def test_health_summary_custom_prefix(opts):
    """The optional ?prefix= param redirects the scan to a different category root."""
    from mojo.apps.incident.models import Event

    Event.objects.filter(category__startswith="system:health:").delete()
    Event.objects.filter(category__startswith="custom:health:").delete()

    Event.objects.create(category="system:health:runner", level=4, title="runner", details="…")
    Event.objects.create(category="custom:health:gateway", level=8, title="gateway", details="custom")

    assert opts.client.login(opts.admin_name, opts.pword), "admin login failed"
    resp = opts.client.get("/api/incident/health/summary", params=dict(prefix="custom:health:"))
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"

    rows = resp.response["data"]
    cats = [row["category"] for row in rows]
    assert cats == ["custom:health:gateway"], f"prefix filter wrong: {cats}"


@th.django_unit_test()
def test_health_summary_requires_view_security(opts):
    """Users without view_security must be denied."""
    from mojo.apps.incident.models import Event

    Event.objects.create(category="system:health:runner", level=4, title="runner", details="…")

    assert opts.client.login(opts.outsider_name, opts.pword), "outsider login failed"
    resp = opts.client.get("/api/incident/health/summary")
    assert resp.status_code in (401, 403), f"expected auth failure, got {resp.status_code}: {resp.body}"


@th.django_unit_test()
def test_health_summary_rejects_non_namespace_prefix(opts):
    """An empty or non-colon-suffixed prefix must be rejected as a 400.

    Without this guard, a `view_security` user could pass `prefix=` or
    `prefix=invalid_password` and use the endpoint as an open-ended
    category discovery oracle — broader than the namespace-strip intent.
    """
    from mojo.apps.incident.models import Event

    Event.objects.create(category="invalid_password", level=8, title="ip", details="…")

    assert opts.client.login(opts.admin_name, opts.pword), "admin login failed"

    # Empty prefix → 400
    resp = opts.client.get("/api/incident/health/summary", params=dict(prefix=""))
    assert resp.status_code == 400, f"empty prefix expected 400, got {resp.status_code}: {resp.body}"

    # Non-namespace prefix (no trailing colon) → 400
    resp = opts.client.get("/api/incident/health/summary", params=dict(prefix="invalid_password"))
    assert resp.status_code == 400, f"non-namespace prefix expected 400, got {resp.status_code}: {resp.body}"

    # Valid namespace prefix → 200 (sanity check that the guard isn't over-strict)
    resp = opts.client.get("/api/incident/health/summary", params=dict(prefix="system:health:"))
    assert resp.status_code == 200, f"valid prefix expected 200, got {resp.status_code}: {resp.body}"
