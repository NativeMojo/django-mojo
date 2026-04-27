"""
Tests for the `_mode` aggregation surface against /api/incident/incident.

Verifies the Security Dashboard distribution donuts (top by status,
top by priority) and the KPI count tile (count by status=new) all
work end-to-end.
"""
from testit import helpers as th


TEST_USER = "incagg_admin"
TEST_PWORD = "incagg##mojo99"


def _reset_admin(username, password):
    from mojo.apps.account.models import User
    user = User.objects.filter(username=username).last()
    if user is None:
        user = User(username=username, email=f"{username}@example.com")
        user.save()
    user.is_email_verified = True
    user.save_password(password)
    user.remove_all_permissions()
    user.add_permission("view_security")
    user.save()
    return user


@th.django_unit_setup()
def setup_incident_aggregation(opts):
    from mojo.apps.incident.models import Incident
    Incident.objects.filter(category__startswith="inc_agg:").delete()

    _reset_admin(TEST_USER, TEST_PWORD)
    opts.user_name = TEST_USER
    opts.pword = TEST_PWORD


def _seed_incidents():
    from mojo.apps.incident.models import Incident
    Incident.objects.filter(category__startswith="inc_agg:").delete()
    # status: 4 new, 2 open, 1 closed.
    for _ in range(4):
        Incident.objects.create(category="inc_agg:auth", priority=8, status="new", state="new")
    for _ in range(2):
        Incident.objects.create(category="inc_agg:auth", priority=4, status="open", state="open")
    Incident.objects.create(category="inc_agg:net", priority=2, status="closed", state="closed")


@th.django_unit_test()
def test_mode_top_status(opts):
    _seed_incidents()
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    resp = opts.client.get(
        "/api/incident/incident",
        params={
            "_mode": "top",
            "_field": "status",
            "_size": 10,
            "category__in": "inc_agg:auth,inc_agg:net",
        },
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"
    rows = resp.response["data"]
    by_key = {row["key"]: row["value"] for row in rows}
    assert by_key.get("new") == 4, f"expected 4 new, got {by_key}"
    assert by_key.get("open") == 2, f"expected 2 open, got {by_key}"
    assert by_key.get("closed") == 1, f"expected 1 closed, got {by_key}"
    # Sorted desc by value.
    values = [row["value"] for row in rows]
    assert values == sorted(values, reverse=True), (
        f"top must be sorted desc by value, got {values}"
    )


@th.django_unit_test()
def test_mode_top_priority(opts):
    _seed_incidents()
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    resp = opts.client.get(
        "/api/incident/incident",
        params={
            "_mode": "top",
            "_field": "priority",
            "_size": 20,
            "category__in": "inc_agg:auth,inc_agg:net",
        },
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"
    by_key = {row["key"]: row["value"] for row in resp.response["data"]}
    assert by_key.get("8") == 4, f"expected 4 priority=8, got {by_key}"
    assert by_key.get("4") == 2, f"expected 2 priority=4, got {by_key}"
    assert by_key.get("2") == 1, f"expected 1 priority=2, got {by_key}"


@th.django_unit_test()
def test_mode_count_status_new(opts):
    _seed_incidents()
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    resp = opts.client.get(
        "/api/incident/incident",
        params={
            "_mode": "count",
            "status": "new",
            "category__in": "inc_agg:auth,inc_agg:net",
        },
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"
    assert resp.response["count"] == 4, (
        f"status=new seeded 4x, got {resp.response['count']}"
    )
