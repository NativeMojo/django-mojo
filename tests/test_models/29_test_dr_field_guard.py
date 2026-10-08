"""dr_field must name a local date column of the listed model.

The bug: on_rest_list_date_range_filter spliced the client's ``dr_field``
straight into ``queryset.filter(**{f"{dr_field}__gte": ...})``. That made it an
arbitrary filter path with none of build_rest_filters' guards, so a caller
with list permission could narrow ``count`` on a related row it cannot read:

    ?dr_field=user__last_login&dr_start=2026-01-01   -> count changes

Now only a LOCAL concrete DateField / DateTimeField of the model is accepted
(no ``__``, no relation, no JSON path, not SENSITIVE_FIELDS); anything else is
a 400. Every refusal test below fails on the pre-fix code.
"""
from datetime import datetime
import pytz
import objict
from testit import helpers as th

TESTIT_TIER = "core"

CODE_PREFIX = "drfg"
ADMIN_EMAIL = "drfield_admin@test.com"
OLD_EMAIL = "drfield_old@test.com"
NEW_EMAIL = "drfield_new@test.com"


def _build_request(user, data):
    req = objict.objict()
    req.user = user
    req.DATA = objict.objict(data)
    req.QUERY_PARAMS = objict.objict()
    req.method = "GET"
    req.group = None
    req.bearer = None
    req.ip = "127.0.0.1"
    req.path = "/api/shortlink/shortlink"
    req.META = {}
    req.api_key = None
    return req


@th.django_unit_setup()
def setup_dr_field_guard(opts):
    from mojo.apps.account.models import User
    from mojo.apps.shortlink.models import ShortLink

    ShortLink.objects.filter(code__startswith=CODE_PREFIX).delete()
    User.objects.filter(email__in=[ADMIN_EMAIL, OLD_EMAIL, NEW_EMAIL]).delete()

    opts.admin = User.objects.create_user(
        username=ADMIN_EMAIL, email=ADMIN_EMAIL, password="pass123")
    opts.admin.add_permission(["view_admin", "manage_shortlinks"])

    UTC = pytz.UTC
    # Two links created the same day; their owners' last_login differ, so a
    # dr_field walking into the owner would split them.
    for code, email, login in (("old", OLD_EMAIL, datetime(2020, 1, 1, tzinfo=UTC)),
                               ("new", NEW_EMAIL, datetime(2030, 1, 1, tzinfo=UTC))):
        owner = User.objects.create_user(username=email, email=email, password="pass123")
        User.objects.filter(pk=owner.pk).update(last_login=login)
        link = ShortLink.objects.create(
            code=f"{CODE_PREFIX}{code}", url=f"https://example.com/{code}", user=owner,
            metadata={"secret": code})
        ShortLink.objects.filter(pk=link.pk).update(
            created=datetime(2026, 4, 2, 12, 0, tzinfo=UTC),
            modified=datetime(2026, 4, 2, 12, 0, tzinfo=UTC))


def _base_qs():
    from mojo.apps.shortlink.models import ShortLink
    return ShortLink.objects.filter(code__startswith=CODE_PREFIX)


def _filter(opts, data):
    from mojo.apps.shortlink.models import ShortLink
    return ShortLink.on_rest_list_date_range_filter(_build_request(opts.admin, data), _base_qs())


@th.django_unit_test()
def test_dr_field_relation_path_refused(opts):
    """dr_field walking into a related row is a 400, never a narrowed set."""
    from mojo import errors as me
    for dr_field in ("user__last_login", "user__date_joined", "group__created",
                     "user.last_login", "metadata__secret", "user", "code", "nope", "pk"):
        raised = None
        count = None
        try:
            count = _filter(opts, {"dr_field": dr_field, "dr_start": "2025-01-01"}).count()
        except me.ValueException as err:
            raised = err
        assert raised is not None, \
            f"dr_field={dr_field!r} must be refused, got a queryset of {count}"
        assert raised.code == 400, f"dr_field={dr_field!r}: code {raised.code}, expected 400"


@th.django_unit_test()
def test_dr_field_local_dates_still_work(opts):
    """created (default), modified and an empty dr_field keep filtering."""
    for data, expected in (
            ({"dr_start": "2026-04-02", "dr_end": "2026-04-02"}, 2),
            ({"dr_field": "", "dr_start": "2026-04-02"}, 2),
            ({"dr_field": "modified", "dr_start": "2026-04-02", "dr_end": "2026-04-02"}, 2),
            ({"dr_field": "created", "dr_start": "2026-04-03"}, 0)):
        got = _filter(opts, data).count()
        assert got == expected, f"{data}: count {got}, expected {expected}"


@th.django_unit_test()
def test_dr_field_ignored_without_range(opts):
    """No dr_start / dr_end: dr_field is unused and never a 400."""
    got = _filter(opts, {"dr_field": "user__last_login"}).count()
    assert got == 2, f"dr_field without a range must not filter, got {got}"
