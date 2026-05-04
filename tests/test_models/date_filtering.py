"""Tests for date-component and partial-date list filtering in mojo/models/rest.py.

Covers three surfaces:
  1. Standard Django component lookups (``__year``, ``__month``, ``__quarter``…)
  2. Partial-date field shorthand (``?created=2026-04`` → tz-anchored UTC range)
  3. Partial dates in ``dr_start`` / ``dr_end``

Uses ``shortlink.ShortLink`` as the unit-under-test host because it has a
``created`` ``DateTimeField`` with ``auto_now_add`` (which we override via
``QuerySet.update`` to backdate fixture rows) and a ``code`` ``CharField`` for
the partial-date-on-CharField regression case. Permissive ``manage_shortlinks``
perms keep the request setup small.
"""
from datetime import datetime
import pytz
import objict
from testit import helpers as th


CODE_PREFIX = "dft"


def _build_request(user, query=None, data=None):
    """Synthetic request rich enough for on_rest_list_filter / on_rest_list_date_range_filter."""
    req = objict.objict()
    req.user = user
    req.DATA = objict.objict(data or {})
    req.QUERY_PARAMS = objict.objict(query or {})
    req.method = "GET"
    req.group = None
    req.bearer = None
    req.ip = "127.0.0.1"
    req.path = "/api/shortlink/shortlink"
    req.META = {}
    req.api_key = None
    return req


@th.django_unit_setup()
def setup_date_filtering(opts):
    from mojo.apps.account.models import User
    from mojo.apps.shortlink.models import ShortLink

    # Long-lived test DB — wipe any leftovers first.
    ShortLink.objects.filter(code__startswith=CODE_PREFIX).delete()
    User.objects.filter(email="datefilt_admin@test.com").delete()

    opts.admin = User.objects.create_user(
        username="datefilt_admin@test.com",
        email="datefilt_admin@test.com",
        password="pass123",
    )
    opts.admin.is_email_verified = True
    opts.admin.save()
    for perm in ["view_admin", "manage_shortlinks"]:
        opts.admin.add_permission(perm)

    # Fixture rows. ``code`` makes per-test queryset isolation easy.
    # ``created`` is overridden post-insert because auto_now_add ignores body.
    UTC = pytz.UTC
    rows = [
        ("a1", UTC.localize(datetime(2025, 3, 15, 10, 0, 0))),
        ("a2", UTC.localize(datetime(2026, 1, 5, 8, 30, 0))),
        ("a3", UTC.localize(datetime(2026, 4, 2, 14, 45, 0))),
        ("a4", UTC.localize(datetime(2026, 4, 17, 9, 0, 0))),
        ("a5", UTC.localize(datetime(2026, 5, 10, 18, 15, 0))),
        ("a6", UTC.localize(datetime(2026, 12, 31, 23, 30, 0))),
    ]
    code_to_dt = {}
    for code, dt in rows:
        link = ShortLink.objects.create(
            code=f"{CODE_PREFIX}_{code}",
            url=f"https://example.com/{code}",
        )
        ShortLink.objects.filter(pk=link.pk).update(created=dt)
        code_to_dt[code] = dt
    opts.code_to_dt = code_to_dt
    opts.fixture_count = len(rows)


def _base_qs():
    from mojo.apps.shortlink.models import ShortLink
    return ShortLink.objects.filter(code__startswith=CODE_PREFIX)


def _codes(queryset):
    """Strip the prefix so assertions are readable."""
    return sorted(c.replace(f"{CODE_PREFIX}_", "") for c in queryset.values_list("code", flat=True))


# ---------------------------------------------------------------------------
# Standard Django component lookups
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_year_component_filter(opts):
    from mojo.apps.shortlink.models import ShortLink
    req = _build_request(opts.admin, query={"created__year": "2026"})
    qs = ShortLink.on_rest_list_filter(req, _base_qs())
    got = _codes(qs)
    assert got == ["a2", "a3", "a4", "a5", "a6"], (
        f"created__year=2026 should return only 2026 rows, got {got}"
    )


@th.django_unit_test()
def test_month_component_filter_across_years(opts):
    from mojo.apps.shortlink.models import ShortLink
    req = _build_request(opts.admin, query={"created__month": "4"})
    qs = ShortLink.on_rest_list_filter(req, _base_qs())
    got = _codes(qs)
    # April rows: a3 (2026-04-02), a4 (2026-04-17). March/May/Dec/Jan excluded.
    assert got == ["a3", "a4"], (
        f"created__month=4 should match April rows across years, got {got}"
    )


@th.django_unit_test()
def test_year_and_month_compose_and(opts):
    from mojo.apps.shortlink.models import ShortLink
    req = _build_request(opts.admin, query={"created__year": "2026", "created__month": "4"})
    qs = ShortLink.on_rest_list_filter(req, _base_qs())
    got = _codes(qs)
    assert got == ["a3", "a4"], (
        f"year=2026 AND month=4 should match April 2026 rows, got {got}"
    )


@th.django_unit_test()
def test_month_in_multi_value(opts):
    from mojo.apps.shortlink.models import ShortLink
    req = _build_request(opts.admin, query={"created__month__in": "4,5"})
    qs = ShortLink.on_rest_list_filter(req, _base_qs())
    got = _codes(qs)
    assert got == ["a3", "a4", "a5"], (
        f"created__month__in=4,5 should match April + May rows, got {got}"
    )


@th.django_unit_test()
def test_month_not_excludes_value(opts):
    from mojo.apps.shortlink.models import ShortLink
    req = _build_request(opts.admin, query={"created__month__not": "12"})
    qs = ShortLink.on_rest_list_filter(req, _base_qs())
    got = _codes(qs)
    # All except a6 (Dec 2026)
    assert got == ["a1", "a2", "a3", "a4", "a5"], (
        f"created__month__not=12 should exclude December rows, got {got}"
    )


@th.django_unit_test()
def test_quarter_component_filter(opts):
    from mojo.apps.shortlink.models import ShortLink
    req = _build_request(opts.admin, query={"created__quarter": "2", "created__year": "2026"})
    qs = ShortLink.on_rest_list_filter(req, _base_qs())
    got = _codes(qs)
    # Q2 2026 = Apr/May/Jun → a3, a4, a5
    assert got == ["a3", "a4", "a5"], (
        f"created__quarter=2 AND year=2026 should match Q2 2026 rows, got {got}"
    )


@th.django_unit_test()
def test_day_component_filter(opts):
    from mojo.apps.shortlink.models import ShortLink
    req = _build_request(opts.admin, query={"created__day": "2"})
    qs = ShortLink.on_rest_list_filter(req, _base_qs())
    got = _codes(qs)
    # Day-of-month=2 → only a3 (2026-04-02)
    assert got == ["a3"], (
        f"created__day=2 should match day-of-month=2 rows, got {got}"
    )


@th.django_unit_test()
def test_invalid_component_value_raises_400(opts):
    from mojo.apps.shortlink.models import ShortLink
    from mojo import errors as me
    req = _build_request(opts.admin, query={"created__month": "foo"})
    raised = None
    try:
        ShortLink.on_rest_list_filter(req, _base_qs())
    except me.ValueException as e:
        raised = e
    assert raised is not None, "Invalid component value should raise ValueException"
    assert raised.code == 400, (
        f"Invalid component value should raise with code=400, got code={raised.code}"
    )


# ---------------------------------------------------------------------------
# Partial-date field shorthand
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_partial_date_shorthand_year_month(opts):
    from mojo.apps.shortlink.models import ShortLink
    req = _build_request(opts.admin, query={"created": "2026-04"})
    qs = ShortLink.on_rest_list_filter(req, _base_qs())
    got = _codes(qs)
    assert got == ["a3", "a4"], (
        f"created=2026-04 shorthand should match April 2026 rows, got {got}"
    )


@th.django_unit_test()
def test_partial_date_shorthand_year_only(opts):
    from mojo.apps.shortlink.models import ShortLink
    req = _build_request(opts.admin, query={"created": "2026"})
    qs = ShortLink.on_rest_list_filter(req, _base_qs())
    got = _codes(qs)
    assert got == ["a2", "a3", "a4", "a5", "a6"], (
        f"created=2026 shorthand should match full year, got {got}"
    )


@th.django_unit_test()
def test_partial_date_shorthand_full_date(opts):
    from mojo.apps.shortlink.models import ShortLink
    req = _build_request(opts.admin, query={"created": "2026-04-02"})
    qs = ShortLink.on_rest_list_filter(req, _base_qs())
    got = _codes(qs)
    assert got == ["a3"], (
        f"created=2026-04-02 shorthand should match that single day, got {got}"
    )


@th.django_unit_test()
def test_partial_date_shorthand_with_timezone_shifts_bounds(opts):
    """A user in PT asking for April expects PT-April, not UTC-April.

    a6 is UTC 2026-12-31 23:30 — in PT that's still 2026-12-31, so under
    timezone=America/Los_Angeles asking for created=2026 still matches it.
    The interesting cross-check: shift a3 to a UTC-April-1 row that lands
    in PT-March — that row should NOT match created=2026-04 under PT.
    """
    from mojo.apps.shortlink.models import ShortLink
    UTC = pytz.UTC
    # 2026-04-01 03:00 UTC = 2026-03-31 20:00 PT
    pt_march_row_pk = ShortLink.objects.get(code=f"{CODE_PREFIX}_a3").pk
    original = opts.code_to_dt["a3"]
    ShortLink.objects.filter(pk=pt_march_row_pk).update(
        created=UTC.localize(datetime(2026, 4, 1, 3, 0, 0))
    )
    try:
        req = _build_request(
            opts.admin,
            query={"created": "2026-04"},
            data={"timezone": "America/Los_Angeles"},
        )
        qs = ShortLink.on_rest_list_filter(req, _base_qs())
        got = _codes(qs)
        assert "a3" not in got, (
            f"In PT, 2026-04-01 03:00 UTC is 2026-03-31 — should NOT match April. got {got}"
        )
        assert "a4" in got, (
            f"PT-April should still include 2026-04-17 14:00 UTC, got {got}"
        )
    finally:
        ShortLink.objects.filter(pk=pt_march_row_pk).update(created=original)


# ---------------------------------------------------------------------------
# Partial dates in dr_start / dr_end
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_dr_partial_year_month_inclusive(opts):
    from mojo.apps.shortlink.models import ShortLink
    req = _build_request(opts.admin, data={"dr_start": "2026-04", "dr_end": "2026-04"})
    qs = ShortLink.on_rest_list_date_range_filter(req, _base_qs())
    got = _codes(qs)
    assert got == ["a3", "a4"], (
        f"dr_start=2026-04 dr_end=2026-04 should cover April 2026 inclusive, got {got}"
    )


@th.django_unit_test()
def test_dr_partial_year_only(opts):
    from mojo.apps.shortlink.models import ShortLink
    req = _build_request(opts.admin, data={"dr_start": "2026", "dr_end": "2026"})
    qs = ShortLink.on_rest_list_date_range_filter(req, _base_qs())
    got = _codes(qs)
    assert got == ["a2", "a3", "a4", "a5", "a6"], (
        f"dr_start=2026 dr_end=2026 should cover all of 2026 inclusive, got {got}"
    )


@th.django_unit_test()
def test_dr_partial_full_date_inclusive(opts):
    from mojo.apps.shortlink.models import ShortLink
    req = _build_request(opts.admin, data={"dr_start": "2026-04-02", "dr_end": "2026-04-02"})
    qs = ShortLink.on_rest_list_date_range_filter(req, _base_qs())
    got = _codes(qs)
    assert got == ["a3"], (
        f"dr_start=2026-04-02 dr_end=2026-04-02 should match only that day, got {got}"
    )


@th.django_unit_test()
def test_dr_full_iso_still_works(opts):
    from mojo.apps.shortlink.models import ShortLink
    req = _build_request(
        opts.admin,
        data={"dr_start": "2026-04-15T00:00:00Z", "dr_end": "2026-04-30T23:59:59Z"},
    )
    qs = ShortLink.on_rest_list_date_range_filter(req, _base_qs())
    got = _codes(qs)
    # Only a4 (2026-04-17) falls in that window.
    assert got == ["a4"], (
        f"Full ISO dr_start/dr_end should keep working unchanged, got {got}"
    )


@th.django_unit_test()
def test_dr_invalid_partial_raises_400(opts):
    from mojo.apps.shortlink.models import ShortLink
    from mojo import errors as me
    req = _build_request(opts.admin, data={"dr_start": "2026-13"})
    raised = None
    try:
        ShortLink.on_rest_list_date_range_filter(req, _base_qs())
    except me.ValueException as e:
        raised = e
    assert raised is not None, "Out-of-range month in dr_start should raise"
    assert raised.code == 400, (
        f"Out-of-range month should be a 400, got code={raised.code}"
    )


# ---------------------------------------------------------------------------
# Regressions
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_charfield_with_dateish_value_unchanged(opts):
    """code is a CharField; ?code=2026-04 must remain exact-match, not partial-date."""
    from mojo.apps.shortlink.models import ShortLink
    req = _build_request(opts.admin, query={"code": f"{CODE_PREFIX}_a3"})
    qs = ShortLink.on_rest_list_filter(req, _base_qs())
    got = _codes(qs)
    assert got == ["a3"], (
        f"CharField exact-match must be unchanged by partial-date logic, got {got}"
    )

    # Negative case: a value that LOOKS like a partial date hits CharField exact-match,
    # finds nothing, and does NOT raise or expand to component lookups.
    req2 = _build_request(opts.admin, query={"code": "2026-04"})
    qs2 = ShortLink.on_rest_list_filter(req2, _base_qs())
    got2 = _codes(qs2)
    assert got2 == [], (
        f"CharField with partial-date-shaped value should be exact-match (empty), got {got2}"
    )


@th.django_unit_test()
def test_full_iso_exact_match_field_unchanged(opts):
    """?created=<full-iso> must keep working as exact-match (no partial-date hijack)."""
    from mojo.apps.shortlink.models import ShortLink
    target_dt = opts.code_to_dt["a3"]
    iso = target_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    req = _build_request(opts.admin, query={"created": iso})
    qs = ShortLink.on_rest_list_filter(req, _base_qs())
    got = _codes(qs)
    assert got == ["a3"], (
        f"Full ISO datetime exact-match should still hit the single row, got {got} (iso={iso})"
    )
