"""Public-account write permissions (board item 937).

The bug this module pins down: `POST /api/metrics/record` accepted writes to
the "public" account from ANYONE — no auth at all — and every distinct slug
becomes a permanent member of the `mets:public:slugs` registry SET, so an
anonymous caller could grow Redis without bound. Reads of public metrics stay
open; writes now require write_metrics/metrics, a configured per-account
perm, or the explicit opt-in `set_write_perms("public", "public")`.

Test order matters within this module: the anonymous tests run before any
login so the shared client carries no session.
"""

TESTIT_TIER = "core"

from testit import helpers as th

SLUG = "t937_pub_probe"
WRITER_USER = "t937_metrics_writer"
WRITER_PWORD = "metrics##mojo99"


def _reset_public_perms():
    """Public write perms are global Redis state — leave them unset, which is
    the locked-down default this module asserts."""
    from mojo.apps import metrics
    metrics.set_write_perms("public", None)


@th.django_unit_setup()
def setup_write_perms(opts):
    from mojo.apps import metrics
    _reset_public_perms()
    metrics.delete_metrics_slug(SLUG, account="public")


@th.django_unit_test("anonymous write to the public account is denied")
def test_anon_public_record_denied(opts):
    """THE regression: before the fix this returned 200 and registered the
    slug permanently."""
    from mojo.apps import metrics
    _reset_public_perms()

    resp = opts.client.post("/api/metrics/record", dict(slug=SLUG))
    assert resp.status_code == 403, (
        f"anonymous public record must be denied, got {resp.status_code}: {resp.body}"
    )
    assert SLUG not in metrics.get_account_slugs("public"), (
        "a denied write must not register the slug in the public registry"
    )


@th.django_unit_test("anonymous gauge set on the public account is denied")
def test_anon_public_value_set_denied(opts):
    _reset_public_perms()
    resp = opts.client.post("/api/metrics/value/set", dict(slug=SLUG, value="1"))
    assert resp.status_code == 403, (
        f"anonymous public value/set must be denied, got {resp.status_code}: {resp.body}"
    )


@th.django_unit_test("set_write_perms('public', 'public') restores anonymous writes")
def test_public_write_optin(opts):
    """The escape hatch: a deployment that truly wants anonymous counters opts
    in explicitly, per account — an open door is no longer the default."""
    from mojo.apps import metrics

    metrics.set_write_perms("public", "public")
    try:
        resp = opts.client.post("/api/metrics/record", dict(slug=SLUG))
        assert resp.status_code == 200, (
            f"opted-in anonymous public record should succeed, "
            f"got {resp.status_code}: {resp.body}"
        )
        assert SLUG in metrics.get_account_slugs("public"), (
            "an accepted write should register the slug"
        )
    finally:
        _reset_public_perms()
        metrics.delete_metrics_slug(SLUG, account="public")


@th.django_unit_test("anonymous read of public metrics stays open")
def test_anon_public_read_still_open(opts):
    resp = opts.client.get("/api/metrics/fetch",
                           params=dict(slugs=SLUG, with_labels=True))
    assert resp.status_code == 200, (
        f"public metric reads must remain anonymous, got {resp.status_code}: {resp.body}"
    )


@th.django_unit_test("a user with the metrics permission can write to public")
def test_metrics_perm_user_can_write_public(opts):
    from mojo.apps import metrics
    from mojo.apps.account.models import User

    _reset_public_perms()

    user = User.objects.filter(username=WRITER_USER).last()
    if user is None:
        user = User(username=WRITER_USER, email=f"{WRITER_USER}@example.com")
        user.save()
    user.is_email_verified = True
    user.save_password(WRITER_PWORD)
    user.remove_all_permissions()
    user.add_permission("metrics")

    assert opts.client.login(WRITER_USER, WRITER_PWORD), "writer login failed"
    try:
        resp = opts.client.post("/api/metrics/record", dict(slug=SLUG))
        assert resp.status_code == 200, (
            f"a metrics-perm holder should be able to record to public, "
            f"got {resp.status_code}: {resp.body}"
        )
    finally:
        metrics.delete_metrics_slug(SLUG, account="public")
        user.remove_all_permissions()
