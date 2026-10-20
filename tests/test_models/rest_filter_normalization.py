"""Boolean list and relation-path REST filter normalization (#8556).

Before the fix, ``build_rest_filters`` turned a whole ``__in`` list into one
bool (``?is_unread__in=true,false`` -> TypeError -> 500), passed relation-path
values to the ORM raw (``?user__is_active=true`` -> ValidationError -> 500),
and read any unknown single Boolean value as False.

The contract now:
  * every filter value is normalized per element, and a list stays a list;
  * a relation path normalizes through the field it ends on, only after the
    sensitive and viewer-restricted guards have passed the key;
  * one strict boolean parser: a malformed value is a 400 naming the key on
    list and count, and a null chip inside ``_stats``.

Host model: ``account.Notification`` (owner-scoped list, ``is_unread``
Boolean with a LIST_DEFAULT_FILTERS default, ``expires_at`` DateTime, and a
``user`` FK into User). End-to-end tests use ``opts.client``; the parser
matrix calls ``build_rest_filters`` directly.
"""
import json

import objict
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


KIND = "rfn8556"
OWNER = "rfn_owner"
OTHER = "rfn_other"
PWORD = "rfnfilt##mojo99"
URL = "/api/account/notification"
EXPIRES_A = "2031-01-02T03:04:05+00:00"
EXPIRES_B = "2031-02-03T04:05:06+00:00"


def _notification():
    from mojo.apps.account.models.notification import Notification
    return Notification


def _reset_user(username):
    from mojo.apps.account.models import User
    user = User.objects.filter(username=username).last()
    if user is None:
        user = User(username=username, email=f"{username}@example.com")
        user.save()
    user.is_email_verified = True
    user.is_active = True
    user.save_password(PWORD)
    user.remove_all_permissions()
    user.is_staff = False
    user.is_superuser = False
    user.save()
    return user


def _build_request(user):
    req = objict.objict()
    req.user = user
    req.DATA = objict.objict()
    req.QUERY_PARAMS = objict.objict()
    req.method = "GET"
    req.group = None
    req.bearer = None
    req.ip = "127.0.0.1"
    req.path = URL
    req.META = {}
    req.api_key = None
    req.group_token = None
    return req


def _parse(opts, params):
    return _notification().build_rest_filters(_build_request(opts.owner), params)


def _titles(resp):
    return sorted(row["title"] for row in resp.response["data"])


def _get(opts, params):
    assert_true(opts.client.login(OWNER, PWORD), "owner login failed")
    return opts.client.get(URL, params=params)


def _expect_400(opts, params, needle):
    resp = _get(opts, params)
    assert_eq(resp.status_code, 400,
              f"{params} must be a client error, got {resp.status_code}: {resp.response}")
    assert_true(needle in str(resp.response),
                f"the 400 for {params} should name {needle!r}, got {resp.response}")


@th.django_unit_setup()
def setup_rest_filter_normalization(opts):
    """Three owner rows and one row for another user.

    | title     | user      | is_unread | expires_at |
    |-----------|-----------|-----------|------------|
    | rfn-a     | rfn_owner | True      | EXPIRES_A  |
    | rfn-b     | rfn_owner | False     | EXPIRES_B  |
    | rfn-c     | rfn_owner | True      | None       |
    | rfn-other | rfn_other | True      | None       |
    """
    from mojo.helpers import dates
    Notification = _notification()
    owner = _reset_user(OWNER)
    other = _reset_user(OTHER)
    Notification.objects.filter(kind=KIND).delete()
    Notification.objects.filter(user__in=[owner, other]).delete()

    def mk(title, user, is_unread, expires_at):
        return Notification.objects.create(
            title=title, body="", kind=KIND, user=user, is_unread=is_unread,
            expires_at=dates.parse_datetime(expires_at) if expires_at else None)

    mk("rfn-a", owner, True, EXPIRES_A)
    mk("rfn-b", owner, False, EXPIRES_B)
    mk("rfn-c", owner, True, None)
    mk("rfn-other", other, True, None)
    opts.owner = owner
    opts.other = other


# ---------------------------------------------------------------------------
# Boolean lists on a top-level field (red before #8556: 500)
# ---------------------------------------------------------------------------

@th.django_unit_test("REST: ?is_unread__in=true,false returns both states")
def test_bool_in_both(opts):
    resp = _get(opts, {"is_unread__in": "true,false"})
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.response}")
    titles = _titles(resp)
    assert_eq(titles, ["rfn-a", "rfn-b", "rfn-c"],
              f"both states, own rows only, default suppressed; got {titles}")


@th.django_unit_test("REST: ?is_unread__not_in=true excludes unread rows")
def test_bool_not_in(opts):
    resp = _get(opts, {"is_unread__not_in": "true"})
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.response}")
    titles = _titles(resp)
    assert_eq(titles, ["rfn-b"], f"only the read row should remain, got {titles}")


@th.django_unit_test("REST: ?is_unread__in=false is not inverted")
def test_bool_in_false(opts):
    resp = _get(opts, {"is_unread__in": "false"})
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.response}")
    titles = _titles(resp)
    assert_eq(titles, ["rfn-b"], f"__in=false must select read rows only, got {titles}")


@th.django_unit_test("REST: a malformed single Boolean is a 400, not a silent False")
def test_bool_malformed_single(opts):
    _expect_400(opts, {"is_unread": "maybe"}, "is_unread")


# ---------------------------------------------------------------------------
# Relation paths (red before #8556: 500)
# ---------------------------------------------------------------------------

@th.django_unit_test("REST: ?user__is_active=true normalizes through User.is_active")
def test_relation_bool(opts):
    resp = _get(opts, {"user__is_active": "true"})
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.response}")
    titles = _titles(resp)
    assert_eq(titles, ["rfn-a", "rfn-c"],
              f"own unread rows (is_unread default still applies), got {titles}")


@th.django_unit_test("REST: ?user__is_active__in=true,false normalizes per element")
def test_relation_bool_in(opts):
    resp = _get(opts, {"user__is_active__in": "true,false"})
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.response}")
    titles = _titles(resp)
    assert_eq(titles, ["rfn-a", "rfn-c"], f"own unread rows, got {titles}")


@th.django_unit_test("REST: a malformed relation Boolean is a 400")
def test_relation_bool_malformed(opts):
    _expect_400(opts, {"user__is_active": "maybe"}, "user__is_active")


@th.django_unit_test("REST: a malformed relation date is a 400")
def test_relation_date_malformed(opts):
    _expect_400(opts, {"user__last_login__gte": "garbage"}, "user__last_login__gte")


@th.django_unit_test("REST: ?expires_at__in=<iso>,<iso> parses each date")
def test_datetime_in(opts):
    resp = _get(opts, {"expires_at__in": f"{EXPIRES_A},{EXPIRES_B}", "_no_defaults": "1"})
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.response}")
    titles = _titles(resp)
    assert_eq(titles, ["rfn-a", "rfn-b"], f"both dated rows, got {titles}")


@th.django_unit_test("REST: _stats counts a Boolean list chip and nulls a malformed one")
def test_stats_bundles(opts):
    stats = {"both": {"is_unread__in": [True, False]}, "bad": {"is_unread": "maybe"}}
    resp = _get(opts, {"_mode": "count", "_stats": json.dumps(stats), "_no_defaults": "1"})
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.response}")
    got = resp.response.get("stats") or {}
    assert_eq(got.get("both"), 3, f"the Boolean list chip must count own rows, got {got}")
    assert_true("bad" in got and got["bad"] is None,
                f"a malformed chip must be null, not fail the strip; got {got}")


@th.django_unit_test("REST: a non-numeric integer __in element is a 400")
def test_integer_in_malformed(opts):
    resp = _get(opts, {"id__in": "1,x"})
    assert_eq(resp.status_code, 400,
              f"id__in=1,x must be a client error, got {resp.status_code}: {resp.response}")


# ---------------------------------------------------------------------------
# Parser matrix (in-process)
# ---------------------------------------------------------------------------

@th.django_unit_test("parser: every accepted boolean token, any case")
def test_parser_accepts(opts):
    cases = {
        "true": True, "TRUE": True, " 1 ": True, "t": True, "Y": True, "yes": True, "On": True,
        "false": False, "False": False, "0": False, "f": False, "n": False, "NO": False, "off": False,
    }
    for token, want in cases.items():
        filters, _ = _parse(opts, {"is_unread": token})
        assert_eq(filters.get("is_unread"), want, f"{token!r} should parse as {want}, got {filters}")
    for raw, want in ((True, True), (False, False), (1, True), (0, False)):
        filters, _ = _parse(opts, {"is_unread": raw})
        assert_eq(filters.get("is_unread"), want, f"{raw!r} should parse as {want}, got {filters}")
    filters, _ = _parse(opts, {"is_unread__in": "true,FALSE"})
    assert_eq(filters.get("is_unread__in"), [True, False],
              f"__in must stay a list of bools, got {filters}")
    _, excludes = _parse(opts, {"is_unread__not_in": [True, "no"]})
    assert_eq(excludes.get("is_unread__in"), [True, False],
              f"__not_in must stay a list of bools, got {excludes}")


@th.django_unit_test("parser: malformed booleans raise a 400 ValueException")
def test_parser_refuses(opts):
    from mojo import errors as me
    for raw in ("", "2", 2, "maybe", ["true"]):
        params = {"is_unread__in": [raw]} if isinstance(raw, list) else {"is_unread": raw}
        try:
            out = _parse(opts, params)
        except me.ValueException as err:
            assert_eq(getattr(err, "status", None), 400, f"{raw!r} should be a 400, got {err!r}")
            continue
        assert_true(False, f"{raw!r} must be refused, got {out}")


@th.django_unit_test("parser: integer __in elements become ints")
def test_parser_integer_in(opts):
    filters, _ = _parse(opts, {"id__in": "1,2"})
    assert_eq(filters.get("id__in"), [1, 2], f"numeric ids should become ints, got {filters}")


# ---------------------------------------------------------------------------
# Unchanged behaviour (green before and after)
# ---------------------------------------------------------------------------

@th.django_unit_test("parser: relation pk filters are left alone")
def test_relation_pk_unchanged(opts):
    pk = str(opts.owner.pk)
    filters, _ = _parse(opts, {"user": pk})
    assert_eq(filters.get("user"), pk, f"user=<pk> must reach the ORM as sent, got {filters}")
    filters, _ = _parse(opts, {"user__in": f"{pk},{opts.other.pk}"})
    assert_eq(filters.get("user__in"), [pk, str(opts.other.pk)],
              f"user__in must be a list of the sent pks, got {filters}")
    filters, _ = _parse(opts, {"user": "null"})
    assert_true("user" in filters and filters["user"] is None,
                f"user=null must map to None, got {filters}")
    filters, _ = _parse(opts, {"user__isnull": "true"})
    assert_eq(filters.get("user__isnull"), True, f"user__isnull=true must be True, got {filters}")


@th.django_unit_test("REST: sensitive and viewer-restricted paths stay silently dropped")
def test_guarded_paths_still_dropped(opts):
    for params in ({"user__auth_key__in": "a,b"}, {"user__requires_mfa": "maybe"}):
        resp = _get(opts, params)
        assert_eq(resp.status_code, 200,
                  f"{params} must be dropped silently (200), never a 400 oracle; "
                  f"got {resp.status_code}: {resp.response}")
        titles = _titles(resp)
        assert_eq(titles, ["rfn-a", "rfn-c"], f"{params} must not filter anything, got {titles}")
    filters, _ = _parse(opts, {"user__requires_mfa": "maybe", "user__auth_key": "x"})
    assert_eq(filters, {}, f"guarded keys must not reach the filters, got {filters}")
