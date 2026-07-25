"""Tests for `RestMeta.LIST_DEFAULT_FILTERS` on the list query path (item 389).

The property is a **baseline** filter applied to list endpoints, not an
unconditional one. Precedence:

  * declared default applies when the request says nothing about that field
  * any request param naming the same BASE FIELD replaces the default for that
    field — including the exclusion forms (`__not` / `__not_in`) that land in
    ``excludes`` rather than ``filters``
  * ``?_no_defaults=1`` drops every default for the request

Two surfaces are exercised, mirroring ``tests/test_models/list_stats.py``:

  * Direct calls to ``on_rest_list_default_filters`` with a synthetic request
    for the suppression mechanics. These assert **whether the default was
    applied**, not the final list — the request's own filters are applied
    later, by ``on_rest_list_filter``.
  * End-to-end through ``/api/account/notification`` with ``opts.client`` for
    the composed wire contract, the owner-scoping composition, and the
    aggregation path.

Host model: ``account.Notification`` — the one surviving declaration
(``{"is_unread": True}``) and the model the original bug was reported against.
Its ``VIEW_PERMS = ["owner"]`` routes the list through the owner-scoped branch
in ``on_rest_handle_list``, so "defaults compose with a permission-scoped
queryset" is proven by the real path rather than staged.

No ``RestMeta`` monkeypatching anywhere: ``opts.client`` calls a separate
server process where in-process patches have no effect, so every test runs
against the model's real declaration.
"""
import objict
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


KIND = "ldf389"                 # per-module row isolation via Notification.kind
OWNER = "ldf_owner"
OTHER = "ldf_other"
PWORD = "ldffilt##mojo99"


def _notification():
    from mojo.apps.account.models.notification import Notification
    return Notification


def _base_qs():
    """Every fixture row, both users, read and unread."""
    return _notification().objects.filter(kind=KIND)


def _build_request(user, data=None, query=None):
    """Synthetic request rich enough for on_rest_list_default_filters."""
    req = objict.objict()
    req.user = user
    req.DATA = objict.objict(data or {})
    req.QUERY_PARAMS = objict.objict(query or {})
    req.method = "GET"
    req.group = None
    req.bearer = None
    req.ip = "127.0.0.1"
    req.path = "/api/account/notification"
    req.META = {}
    req.api_key = None
    return req


def _defaults_applied(user, filters=None, excludes=None, data=None, queryset=None):
    """Run the engine and return the titles it left in the queryset."""
    qs = _base_qs() if queryset is None else queryset
    out = _notification().on_rest_list_default_filters(
        _build_request(user, data=data), qs, filters or {}, excludes or {},
    )
    return sorted(out.values_list("title", flat=True))


def _reset_user(username):
    from mojo.apps.account.models import User
    user = User.objects.filter(username=username).last()
    if user is None:
        user = User(username=username, email=f"{username}@example.com")
        user.save()
    user.is_email_verified = True
    user.save_password(PWORD)
    user.remove_all_permissions()
    user.is_staff = False
    user.is_superuser = False
    user.save()
    return user


@th.django_unit_setup()
def setup_list_default_filters(opts):
    """Four fixture rows — three for the owner (2 unread, 1 read), one for another user.

    | title      | user      | is_unread |
    |------------|-----------|-----------|
    | ldf-unread-a | ldf_owner | True      |
    | ldf-unread-b | ldf_owner | True      |
    | ldf-read-c   | ldf_owner | False     |
    | ldf-other-d  | ldf_other | True      |
    """
    Notification = _notification()

    owner = _reset_user(OWNER)
    other = _reset_user(OTHER)

    # Long-lived test DB — wipe leftovers before creating.
    Notification.objects.filter(kind=KIND).delete()
    Notification.objects.filter(user__in=[owner, other]).delete()

    def mk(title, user, is_unread):
        return Notification.objects.create(
            title=title, body="", kind=KIND, user=user,
            is_unread=is_unread, expires_at=None,
        )

    opts.unread_a = mk("ldf-unread-a", owner, True)
    opts.unread_b = mk("ldf-unread-b", owner, True)
    opts.read_c = mk("ldf-read-c", owner, False)
    opts.other_d = mk("ldf-other-d", other, True)

    opts.owner_user = OWNER
    opts.other_user = OTHER
    opts.pword = PWORD
    opts.owner = owner
    opts.other = other

    opts.all_titles = ["ldf-other-d", "ldf-read-c", "ldf-unread-a", "ldf-unread-b"]
    opts.unread_titles = ["ldf-other-d", "ldf-unread-a", "ldf-unread-b"]


# ---------------------------------------------------------------------------
# Direct calls: suppression mechanics
# ---------------------------------------------------------------------------

@th.django_unit_test("default filter applies when the request names no field")
def test_baseline_applied(opts):
    titles = _defaults_applied(opts.owner)
    assert_eq(
        titles, opts.unread_titles,
        f"declared LIST_DEFAULT_FILTERS should hide the read row, got {titles}",
    )


@th.django_unit_test("same-field request param replaces the default")
def test_same_field_param_replaces(opts):
    titles = _defaults_applied(opts.owner, filters={"is_unread": False})
    assert_eq(
        titles, opts.all_titles,
        f"?is_unread=false must suppress the is_unread default, got {titles}",
    )


@th.django_unit_test("exclusion form (__not) replaces the default")
def test_exclusion_form_replaces(opts):
    """`?is_unread__not=true` parses into `excludes`, not `filters`.

    A key-level check that only looked at `filters` would leave the
    `is_unread=True` default in place and AND it against the exclusion,
    producing a guaranteed-empty list. This is the trap the field-level
    suppression exists to avoid.
    """
    titles = _defaults_applied(opts.owner, excludes={"is_unread": True})
    assert_eq(
        titles, opts.all_titles,
        f"an exclusion on is_unread must suppress the default, got {titles}",
    )


@th.django_unit_test("__in form replaces the default")
def test_in_form_replaces(opts):
    titles = _defaults_applied(opts.owner, filters={"is_unread__in": [True, False]})
    assert_eq(
        titles, opts.all_titles,
        f"?is_unread__in=true,false must suppress the default, got {titles}",
    )


@th.django_unit_test("a param on a different field leaves the default in force")
def test_other_field_does_not_suppress(opts):
    titles = _defaults_applied(opts.owner, filters={"user": opts.owner})
    assert_eq(
        titles, opts.unread_titles,
        f"filtering on user must not suppress the is_unread default, got {titles}",
    )


@th.django_unit_test("_no_defaults=1 drops every default")
def test_no_defaults_switch(opts):
    titles = _defaults_applied(opts.owner, data={"_no_defaults": "1"})
    assert_eq(
        titles, opts.all_titles,
        f"_no_defaults=1 must drop the default entirely, got {titles}",
    )


@th.django_unit_test("_no_defaults=0 is parsed as a boolean, not a truthy string")
def test_no_defaults_off_is_false(opts):
    """`request.DATA.get("_no_defaults")` returns the string "0", which is truthy.

    Reading it with `get_typed(..., bool)` is what keeps `?_no_defaults=0` from
    inverting the caller's intent.
    """
    titles = _defaults_applied(opts.owner, data={"_no_defaults": "0"})
    assert_eq(
        titles, opts.unread_titles,
        f"_no_defaults=0 must leave the default in force, got {titles}",
    )


@th.django_unit_test("a model with no declaration is untouched")
def test_model_without_declaration_unchanged(opts):
    """The engine must be a provable no-op for the ~every model with no prop."""
    from mojo.apps.shortlink.models import ShortLink

    assert_true(
        ShortLink.get_rest_meta_prop("LIST_DEFAULT_FILTERS", None) is None,
        "ShortLink must not declare LIST_DEFAULT_FILTERS for this test to mean anything",
    )
    base = ShortLink.objects.all()
    out = ShortLink.on_rest_list_default_filters(_build_request(opts.owner), base, {}, {})
    assert_eq(
        out.count(), base.count(),
        "a model without LIST_DEFAULT_FILTERS must get its queryset back unchanged",
    )


# ---------------------------------------------------------------------------
# End-to-end (opts.client): composed wire contract
# ---------------------------------------------------------------------------

def _titles(body):
    return sorted(row["title"] for row in body["data"])


@th.django_unit_test("REST: list applies the declared default (regression)")
def test_rest_list_applies_default(opts):
    """The reported bug: /api/account/notification returned read rows too.

    Fails before the fix (all three of the owner's rows come back), passes
    after (only the two unread ones).
    """
    assert_true(opts.client.login(opts.owner_user, opts.pword), "owner login failed")
    resp = opts.client.get("/api/account/notification")
    assert_eq(resp.status_code, 200, f"owner should list own notifications, got {resp.status_code}")
    titles = _titles(resp.response)
    assert_eq(
        titles, ["ldf-unread-a", "ldf-unread-b"],
        f"list must return only unread rows (the declared default), got {titles}",
    )


@th.django_unit_test("REST: ?is_unread=false overrides the default")
def test_rest_list_param_overrides_default(opts):
    assert_true(opts.client.login(opts.owner_user, opts.pword), "owner login failed")
    resp = opts.client.get("/api/account/notification", params={"is_unread": "false"})
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.body}")
    titles = _titles(resp.response)
    assert_eq(
        titles, ["ldf-read-c"],
        f"?is_unread=false must return the read row, got {titles}",
    )


@th.django_unit_test("REST: ?_no_defaults=1 returns read and unread")
def test_rest_list_no_defaults(opts):
    assert_true(opts.client.login(opts.owner_user, opts.pword), "owner login failed")
    resp = opts.client.get("/api/account/notification", params={"_no_defaults": "1"})
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.body}")
    titles = _titles(resp.response)
    assert_eq(
        titles, ["ldf-read-c", "ldf-unread-a", "ldf-unread-b"],
        f"_no_defaults=1 must return every row the owner can see, got {titles}",
    )


@th.django_unit_test("REST: defaults compose with owner scoping, never widen it")
def test_rest_list_defaults_respect_owner_scope(opts):
    """Another user's unread row must not appear — not even with the default off.

    Proves the default ANDs onto the permission-scoped queryset that
    on_rest_handle_list passes in, rather than replacing it.
    """
    assert_true(opts.client.login(opts.owner_user, opts.pword), "owner login failed")
    for params in ({}, {"_no_defaults": "1"}, {"is_unread": "true"}):
        resp = opts.client.get("/api/account/notification", params=params)
        assert_eq(resp.status_code, 200, f"expected 200 for {params}, got {resp.status_code}")
        titles = _titles(resp.response)
        assert_true(
            "ldf-other-d" not in titles,
            f"another user's notification leaked with params={params}: {titles}",
        )


@th.django_unit_test("REST: _mode=count inherits the default")
def test_rest_count_inherits_default(opts):
    """A stat count must equal the list it describes."""
    assert_true(opts.client.login(opts.owner_user, opts.pword), "owner login failed")
    resp = opts.client.get("/api/account/notification", params={"_mode": "count"})
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.body}")
    assert_eq(
        resp.response["count"], 2,
        f"count mode must apply the default like the list does, got {resp.response['count']}",
    )
