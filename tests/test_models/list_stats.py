"""Tests for `_mode=count` `_stats` batched filter-bundle counts (DM-051).

`?_mode=count&_stats={"open":{...},"high":{...}}` returns one count per named
bundle, each AND-ed onto the request's already-scoped + already-filtered
queryset — so a stat chip's count always equals what the caller would see after
clicking it (web-mojo WM-037 stat strips).

Two surfaces are exercised:
  * Direct calls to ``on_rest_list_aggregate`` with a synthetic request and a
    prefix-scoped base queryset (fast + precise, mirroring
    ``tests/test_models/date_filtering.py``) for the parse / count / cap /
    error mechanics and operator coverage.
  * End-to-end through ``/api/shortlink/link`` with ``opts.client`` for the
    wire contract (query-param JSON-string parsing, response envelope, the
    capability-by-key-absence rule) and the permission-scoping proof (owner
    fallback), mirroring ``tests/test_account/test_aggregation_permissions.py``.

Host model: ``shortlink.ShortLink`` — it has ``source`` (CharField),
``is_active`` (BooleanField) and ``hit_count`` (IntegerField) to build bundles
on, plus ``user`` / ``group`` FKs so owner scoping applies. ``code`` is a
unique CharField (max_length=10) used as a per-test isolation prefix.
"""
import json
import objict
from testit import helpers as th


CODE_PREFIX = "sl051"          # <= keeps generated codes within max_length=10
S_EMAIL = "dm051_email"
S_SMS = "dm051_sms"
S_PUSH = "dm051_push"
PWORD = "liststat##mojo99"


def _shortlink():
    from mojo.apps.shortlink.models import ShortLink
    return ShortLink


def _base_qs():
    return _shortlink().objects.filter(code__startswith=CODE_PREFIX)


def _get_admin():
    from mojo.apps.account.models import User
    return User.objects.filter(username="liststat_admin").last()


def _build_request(user, data=None, query=None, group=None):
    """Synthetic request rich enough for on_rest_list_aggregate / build_rest_filters."""
    req = objict.objict()
    req.user = user
    req.DATA = objict.objict(data or {})
    req.QUERY_PARAMS = objict.objict(query or {})
    req.method = "GET"
    req.group = group
    req.bearer = None
    req.ip = "127.0.0.1"
    req.path = "/api/shortlink/link"
    req.META = {}
    req.api_key = None
    return req


def _agg(user, stats, base=None):
    """Call the count-mode aggregation directly, return the parsed body dict.

    ``stats`` may be a JSON string (query-param form) or a dict (JSON-body
    form) or None (no _stats at all). Raises whatever on_rest_list_aggregate
    raises (used by the loud-error tests).
    """
    from mojo.models.rest_aggregation import on_rest_list_aggregate
    data = {"_mode": "count"}
    if stats is not None:
        data["_stats"] = stats
    req = _build_request(user, data=data)
    resp = on_rest_list_aggregate(_shortlink(), req, _base_qs() if base is None else base)
    return json.loads(resp.content)


def _reset_user(username, password=PWORD):
    from mojo.apps.account.models import User
    user = User.objects.filter(username=username).last()
    if user is None:
        user = User(username=username, email=f"{username}@example.com")
        user.save()
    user.is_email_verified = True
    user.save_password(password)
    user.remove_all_permissions()
    user.is_staff = False
    user.is_superuser = False
    user.save()
    return user


@th.django_unit_setup()
def setup_list_stats(opts):
    """Six prefix-scoped ShortLinks across three sources / two owners.

    | source     | n | is_active | hit_count | owner            |
    |------------|---|-----------|-----------|------------------|
    | dm051_email| 3 | True      | 0         | liststat_owner   |
    | dm051_sms  | 2 | True      | 10        | liststat_other   |
    | dm051_push | 1 | False     | 0         | liststat_other   |
    """
    ShortLink = _shortlink()

    # Long-lived DB — wipe leftovers first.
    ShortLink.objects.filter(code__startswith=CODE_PREFIX).delete()

    admin = _reset_user("liststat_admin")
    admin.add_permission("manage_shortlinks")           # happy-path VIEW_PERMS
    owner = _reset_user("liststat_owner")               # no perms -> owner fallback
    other = _reset_user("liststat_other")

    def mk(tag, n, source, is_active, hit_count, user):
        for i in range(n):
            ShortLink.objects.create(
                code=f"{CODE_PREFIX}{tag}{i}",
                url=f"https://example.com/{source}/{i}",
                source=source,
                is_active=is_active,
                hit_count=hit_count,
                user=user,
            )

    mk("e", 3, S_EMAIL, True, 0, owner)
    mk("s", 2, S_SMS, True, 10, other)
    mk("p", 1, S_PUSH, False, 0, other)

    opts.admin_user = "liststat_admin"
    opts.owner_user = "liststat_owner"
    opts.pword = PWORD
    opts.total = 6


# ---------------------------------------------------------------------------
# Direct: counts, AND-with-base, operator coverage
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_stats_bundles_count_correctly(opts):
    body = _agg(_get_admin(), json.dumps({
        "email": {"source": S_EMAIL},
        "all": {},
        "none": {"source": "does-not-exist"},
    }))
    assert body["count"] == opts.total, f"base count should be {opts.total}, got {body['count']}"
    assert body["stats"]["email"] == 3, f"email bundle should be 3, got {body['stats']['email']}"
    assert body["stats"]["all"] == opts.total, (
        f"empty bundle {{}} should equal base {opts.total}, got {body['stats']['all']}"
    )
    assert body["stats"]["none"] == 0, (
        f"nonexistent-source bundle should be 0, got {body['stats']['none']}"
    )


@th.django_unit_test()
def test_stats_bundles_and_with_scoped_base(opts):
    """Each bundle ANDs onto the base queryset it is given, not the whole table."""
    from mojo.models.rest_aggregation import on_rest_list_aggregate
    base = _base_qs().filter(is_active=True)        # 5 rows: excludes the push row
    req = _build_request(_get_admin(), data={"_mode": "count", "_stats": json.dumps({
        "email": {"source": S_EMAIL},
        "push": {"source": S_PUSH},
    })})
    body = json.loads(on_rest_list_aggregate(_shortlink(), req, base).content)
    assert body["count"] == 5, f"is_active base should be 5, got {body['count']}"
    assert body["stats"]["email"] == 3, f"email under active base = 3, got {body['stats']['email']}"
    assert body["stats"]["push"] == 0, (
        "push row is inactive and excluded by the base filter, so its bundle "
        f"must be 0 (AND-ed), got {body['stats']['push']}"
    )


@th.django_unit_test()
def test_stats_operator_coverage(opts):
    body = _agg(_get_admin(), json.dumps({
        "in_str": {"source__in": f"{S_EMAIL},{S_SMS}"},
        "in_list": {"source__in": [S_EMAIL, S_SMS]},
        "not_push": {"source__not": S_PUSH},
        "active": {"is_active": True},
        "inactive": {"is_active": False},
        "busy": {"hit_count__gt": 5},
        "has_source": {"source__isnull": False},
    }))
    s = body["stats"]
    assert s["in_str"] == 5, f"__in comma-string should be 5, got {s['in_str']}"
    # JSON-native list must NOT be .split() — proves the isinstance(str) guard.
    assert s["in_list"] == 5, f"__in JSON-native list should be 5, got {s['in_list']}"
    assert s["not_push"] == 5, f"__not exclude push should be 5, got {s['not_push']}"
    assert s["active"] == 5, f"is_active=true should be 5, got {s['active']}"
    assert s["inactive"] == 1, f"is_active=false should be 1, got {s['inactive']}"
    assert s["busy"] == 2, f"hit_count__gt=5 should be 2 (sms rows), got {s['busy']}"
    assert s["has_source"] == 6, f"source__isnull=false should be 6, got {s['has_source']}"


# ---------------------------------------------------------------------------
# Direct: presence / absence / shape of the stats key
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_stats_absent_emits_no_stats_key(opts):
    """Plain _mode=count without _stats must not emit a stats key.

    This is the capability-detection contract: WM-037 treats an absent `stats`
    key as "aggregation unsupported" and renders label-only chips.
    """
    body = _agg(_get_admin(), None)
    assert body["count"] == opts.total, f"plain count should be {opts.total}, got {body['count']}"
    assert "stats" not in body, "_mode=count without _stats must not emit a stats key"


@th.django_unit_test()
def test_stats_empty_object_emits_empty_map(opts):
    body = _agg(_get_admin(), json.dumps({}))
    assert "stats" in body, "_stats={} should still emit a stats key"
    assert body["stats"] == {}, f"_stats={{}} should give an empty stats map, got {body['stats']}"
    assert body["count"] == opts.total, "count is unaffected by an empty _stats"


@th.django_unit_test()
def test_stats_accepts_dict_form(opts):
    """A JSON request body delivers _stats as a dict (not a string)."""
    body = _agg(_get_admin(), {"email": {"source": S_EMAIL}})
    assert body["stats"]["email"] == 3, f"dict-form _stats should parse, got {body['stats']}"


# ---------------------------------------------------------------------------
# Direct: per-bundle soft failure (null, never a 500)
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_stats_bad_bundle_is_null_not_error(opts):
    """A bundle that fails to build/evaluate yields null; siblings still count."""
    body = _agg(_get_admin(), json.dumps({
        "good": {"source": S_EMAIL},
        "bad_value": {"created__month": "abc"},     # int("abc") -> ValueException
        "bad_field": {"user__nope": 1},             # FieldError at query time
    }))
    assert body["stats"]["good"] == 3, f"good bundle must still count, got {body['stats']['good']}"
    assert body["stats"]["bad_value"] is None, (
        f"bad-value bundle must be null, got {body['stats']['bad_value']}"
    )
    assert body["stats"]["bad_field"] is None, (
        f"bad-field bundle must be null, got {body['stats']['bad_field']}"
    )


# ---------------------------------------------------------------------------
# Direct: structural errors are loud 400s
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_stats_structural_errors_are_400(opts):
    from mojo import errors as me
    user = _get_admin()

    def expect_400(stats, label):
        raised = None
        try:
            _agg(user, stats)
        except me.ValueException as e:
            raised = e
        assert raised is not None, f"{label} should raise ValueException"
        assert raised.code == 400, f"{label} should be a 400, got code={raised.code}"

    expect_400("not-json", "non-JSON _stats string")
    expect_400(json.dumps(["a", "b"]), "JSON-array _stats (not an object)")
    expect_400(json.dumps({"open": "active"}), "bundle value that is not an object")
    expect_400(json.dumps({"x" * 65: {}}), "bundle name longer than 64 chars")
    expect_400(json.dumps({"": {}}), "empty bundle name")


@th.django_unit_test()
def test_stats_cap_exceeded_is_400(opts):
    """More than the default cap (12) bundles is rejected loud."""
    from mojo import errors as me
    bundles = {f"b{i}": {"source": S_EMAIL} for i in range(13)}
    raised = None
    try:
        _agg(_get_admin(), json.dumps(bundles))
    except me.ValueException as e:
        raised = e
    assert raised is not None, "13 bundles should exceed the default cap of 12 and raise"
    assert raised.code == 400, f"cap-exceeded should be a 400, got code={raised.code}"


@th.django_unit_test()
def test_stats_cap_setting_boundary(opts):
    """MOJO_REST_AGG_STATS_CAP governs the boundary exactly (cap N: N ok, N+1 400)."""
    from mojo import errors as me
    from mojo.models import rest_aggregation as agg
    user = _get_admin()
    original = agg.STATS_CAP
    agg.STATS_CAP = 2
    try:
        body = _agg(user, json.dumps({"a": {"source": S_EMAIL}, "b": {"source": S_SMS}}))
        assert body["stats"]["a"] == 3 and body["stats"]["b"] == 2, (
            f"2 bundles at cap=2 should pass, got {body['stats']}"
        )
        raised = None
        try:
            _agg(user, json.dumps({"a": {}, "b": {}, "c": {}}))
        except me.ValueException as e:
            raised = e
        assert raised is not None, "3 bundles over cap=2 should raise"
        assert raised.code == 400, f"3 bundles over cap=2 should be a 400, got code={raised.code}"
    finally:
        agg.STATS_CAP = original


# ---------------------------------------------------------------------------
# End-to-end (opts.client): wire contract + scoping
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_stats_over_http_envelope(opts):
    """Query-param JSON string parses; envelope carries count + stats + took_ms."""
    assert opts.client.login(opts.admin_user, opts.pword), "admin login failed"
    resp = opts.client.get("/api/shortlink/link", params={
        "code__startswith": CODE_PREFIX,
        "_mode": "count",
        "_stats": json.dumps({"email": {"source": S_EMAIL}, "active": {"is_active": True}}),
    })
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"
    body = resp.response
    assert body["count"] == opts.total, f"count should be {opts.total}, got {body['count']}"
    assert body["stats"]["email"] == 3, f"email stat should be 3, got {body['stats']}"
    assert body["stats"]["active"] == 5, f"active stat should be 5, got {body['stats']}"
    assert "took_ms" in body, "count response should carry took_ms"


@th.django_unit_test()
def test_stats_ignored_without_count_mode(opts):
    """`_stats` without `_mode=count` is inert — a normal list, no stats key."""
    assert opts.client.login(opts.admin_user, opts.pword), "admin login failed"
    resp = opts.client.get("/api/shortlink/link", params={
        "code__startswith": CODE_PREFIX,
        "_stats": json.dumps({"email": {"source": S_EMAIL}}),
        "size": 0,
    })
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"
    body = resp.response
    assert "data" in body, "plain list should return a data envelope"
    assert "stats" not in body, "_stats without _mode=count must be ignored (no stats key)"
    assert body["count"] == opts.total, f"list count should be {opts.total}, got {body.get('count')}"


@th.django_unit_test()
def test_stats_db_execution_error_is_null_not_500(opts):
    """A bundle that fails at DB-execution time (invalid regex) is soft-nulled.

    `source__regex` builds fine (the parser validates the base field name, not
    the lookup), then Postgres rejects the pattern at .count() with a DataError
    — a django.db.Error subclass that escapes the Python-layer catch. It must
    still yield null (not a 500 + level-12 incident), honoring the fail-soft
    contract for the DB-error class too. Regression for the DM-051
    security-review WARNING.
    """
    assert opts.client.login(opts.admin_user, opts.pword), "admin login failed"
    resp = opts.client.get("/api/shortlink/link", params={
        "code__startswith": CODE_PREFIX,
        "_mode": "count",
        "_stats": json.dumps({"good": {"source": S_EMAIL}, "bad_regex": {"source__regex": "("}}),
    })
    assert resp.status_code == 200, (
        f"an invalid-regex bundle must not 500 the whole strip, got "
        f"{resp.status_code}: {resp.body}"
    )
    body = resp.response
    assert body["stats"]["good"] == 3, f"good bundle must still count, got {body['stats']}"
    assert body["stats"]["bad_regex"] is None, (
        f"invalid-regex bundle must be null (DB error soft-caught), got "
        f"{body['stats']['bad_regex']}"
    )


@th.django_unit_test()
def test_stats_respects_owner_scope(opts):
    """Owner-fallback caller: bundle counts cover only their own rows.

    liststat_owner has no manage_shortlinks, so the list is owner-scoped to
    their 3 email rows. The `theirs` bundle names a source that exists globally
    (2 sms rows) but belongs to another user — it must count 0, proving each
    bundle ANDs onto the permission-scoped queryset, not the whole table.
    """
    assert opts.client.login(opts.owner_user, opts.pword), "owner login failed"
    resp = opts.client.get("/api/shortlink/link", params={
        "code__startswith": CODE_PREFIX,
        "_mode": "count",
        "_stats": json.dumps({"mine": {"source": S_EMAIL}, "theirs": {"source": S_SMS}}),
    })
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"
    body = resp.response
    assert body["count"] == 3, f"owner sees only their 3 email rows, got count={body['count']}"
    assert body["stats"]["mine"] == 3, f"owner's own-source bundle should be 3, got {body['stats']}"
    assert body["stats"]["theirs"] == 0, (
        "sms rows belong to another user; the owner-scoped bundle must be 0, "
        f"got {body['stats']['theirs']}"
    )
