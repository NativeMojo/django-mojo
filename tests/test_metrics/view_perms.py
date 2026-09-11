"""Metrics read-path defects found on the #1587 sweep (wmx_api security item).

Three framework bugs on the anonymous metrics read surface, each pinned here:

  * DM-1 ``GET /api/metrics/value/get`` with no ``slug``/``slugs``/``category``
    bound its slug list in an if/elif with no else and raised
    ``UnboundLocalError`` -> 500, filing one level-12 ``rest_error`` incident
    with request data and a stack trace per anonymous request.
  * DM-2 ``check_view_permissions`` ended its chain at
    ``elif account != "public"``, so ``metrics.get_view_perms("public")`` was
    never consulted. An operator locking down public reads via
    ``POST /api/metrics/permissions`` got ``{"status": true}`` and zero
    enforcement. Separately, a one-sided permissions POST overwrote the list the
    caller did not send.
  * DM-3 ``get_date_range`` clamped nothing when both bounds were supplied, so
    one request could materialise a key string per bucket without bound.

Public view perms are global Redis state, so every test restores them.
"""

TESTIT_TIER = "core"

import datetime

from testit import helpers as th

SLUG = "t1587_view_probe"
PERM_ACCOUNT = "t1587_perm_probe"
ADMIN_USER = "t1587_metrics_admin"
ADMIN_PWORD = "metrics##mojo99"


def _reset_public_view_perms():
    """Unset is the open default this module starts and ends from."""
    from mojo.apps import metrics
    metrics.set_view_perms("public", None)


@th.django_unit_setup()
def setup_view_perms(opts):
    from mojo.apps import metrics
    _reset_public_view_perms()
    metrics.set_view_perms(PERM_ACCOUNT, None)
    metrics.set_write_perms(PERM_ACCOUNT, None)
    metrics.delete_metrics_slug(SLUG, account="public")


@th.django_unit_test("anonymous public read stays open when no perm is configured")
def test_anon_public_read_open_by_default(opts):
    _reset_public_view_perms()
    opts.client.logout()
    resp = opts.client.get("/api/metrics/fetch", params=dict(slugs=SLUG, with_labels=True))
    assert resp.status_code == 200, (
        f"an unconfigured public account must stay readable, "
        f"got {resp.status_code}: {resp.body}"
    )


@th.django_unit_test("a configured public view perm is actually enforced")
def test_public_view_perm_is_enforced(opts):
    """THE DM-2 regression: before the fix this returned 200 — the configured
    permission was never read, so the documented lock-down control did
    nothing."""
    from mojo.apps import metrics

    opts.client.logout()
    metrics.set_view_perms("public", "view_metrics")
    try:
        resp = opts.client.get("/api/metrics/fetch", params=dict(slugs=SLUG))
        assert resp.status_code == 403, (
            f"a configured public view perm must deny an anonymous read, "
            f"got {resp.status_code}: {resp.body}"
        )
    finally:
        _reset_public_view_perms()

    resp = opts.client.get("/api/metrics/fetch", params=dict(slugs=SLUG))
    assert resp.status_code == 200, (
        f"clearing the perm must reopen the public account, "
        f"got {resp.status_code}: {resp.body}"
    )


@th.django_unit_test("set_view_perms('public', 'public') keeps anonymous reads open")
def test_public_view_perm_public_keyword(opts):
    from mojo.apps import metrics

    opts.client.logout()
    metrics.set_view_perms("public", "public")
    try:
        resp = opts.client.get("/api/metrics/fetch", params=dict(slugs=SLUG))
        assert resp.status_code == 200, (
            f"the explicit 'public' policy must stay open, "
            f"got {resp.status_code}: {resp.body}"
        )
    finally:
        _reset_public_view_perms()


@th.django_unit_test("value/get with no slug, slugs or category is 400")
def test_value_get_missing_params_is_400(opts):
    """THE DM-1 regression: before the fix this was an UnboundLocalError -> 500
    plus one level-12 incident, reachable with no credentials at all."""
    opts.client.logout()
    resp = opts.client.get("/api/metrics/value/get")
    assert resp.status_code == 400, (
        f"value/get with no parameters must be a 400 value error, "
        f"got {resp.status_code}: {resp.body}"
    )


@th.django_unit_test("value/get accepts the singular slug parameter")
def test_value_get_accepts_singular_slug(opts):
    opts.client.logout()
    resp = opts.client.get("/api/metrics/value/get", params=dict(slug=SLUG))
    assert resp.status_code == 200, (
        f"value/get should accept singular slug like fetch does, "
        f"got {resp.status_code}: {resp.body}"
    )


@th.django_unit_test("a date range wider than METRICS_MAX_RANGE_BUCKETS is refused")
def test_wide_range_is_refused(opts):
    """THE DM-3 regression: before the fix this served the request, building one
    key string per bucket first. The window here is over the cap but small in
    absolute terms on purpose — the assertion is the 400, and an unfixed
    checkout should merely be slow, never fall over."""
    opts.client.logout()
    dt_end = datetime.datetime.now()
    dt_start = dt_end - datetime.timedelta(days=60)
    resp = opts.client.get("/api/metrics/fetch", params=dict(
        slugs=SLUG, granularity="minutes",
        dt_start=dt_start.date().isoformat(), dt_end=dt_end.date().isoformat()))
    assert resp.status_code == 400, (
        f"a ~86k-bucket range must exceed the cap and return 400, "
        f"got {resp.status_code}: {resp.body}"
    )


@th.django_unit_test("a normal dashboard range is still served")
def test_range_within_cap_is_served(opts):
    opts.client.logout()
    dt_end = datetime.datetime.now()
    dt_start = dt_end - datetime.timedelta(days=30)
    resp = opts.client.get("/api/metrics/fetch", params=dict(
        slugs=SLUG, granularity="days", with_labels=True,
        dt_start=dt_start.date().isoformat(), dt_end=dt_end.date().isoformat()))
    assert resp.status_code == 200, (
        f"a 30-day daily range is well inside the cap and must be served, "
        f"got {resp.status_code}: {resp.body}"
    )


@th.django_unit_test("a one-sided permissions POST leaves the other list alone")
def test_one_sided_permissions_post(opts):
    """THE DM-2b regression: an absent key used to become [""] — truthy — so
    setting only the write list wiped the view list, replacing it with a
    permission string no user can ever hold."""
    from mojo.apps import metrics
    from mojo.apps.account.models import User

    user = User.objects.filter(username=ADMIN_USER).last()
    if user is None:
        user = User(username=ADMIN_USER, email=f"{ADMIN_USER}@example.com")
        user.save()
    user.is_email_verified = True
    user.save_password(ADMIN_PWORD)
    user.remove_all_permissions()
    user.add_permission("manage_metrics")

    assert opts.client.login(ADMIN_USER, ADMIN_PWORD), "metrics admin login failed"
    try:
        resp = opts.client.post(f"/api/metrics/permissions/{PERM_ACCOUNT}",
                                dict(view_permissions="view_a", write_permissions="write_a"))
        assert resp.status_code == 200, (
            f"setting both lists should succeed, got {resp.status_code}: {resp.body}"
        )

        resp = opts.client.post(f"/api/metrics/permissions/{PERM_ACCOUNT}",
                                dict(write_permissions="write_b"))
        assert resp.status_code == 200, (
            f"setting only the write list should succeed, "
            f"got {resp.status_code}: {resp.body}"
        )

        assert metrics.get_view_perms(PERM_ACCOUNT) == "view_a", (
            f"a write-only POST must not touch the view list, got "
            f"{metrics.get_view_perms(PERM_ACCOUNT)!r}"
        )
        assert metrics.get_write_perms(PERM_ACCOUNT) == "write_b", (
            f"the write list should have been updated, got "
            f"{metrics.get_write_perms(PERM_ACCOUNT)!r}"
        )
    finally:
        metrics.set_view_perms(PERM_ACCOUNT, None)
        metrics.set_write_perms(PERM_ACCOUNT, None)
        user.remove_all_permissions()
        opts.client.logout()
