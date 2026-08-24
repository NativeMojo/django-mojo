"""Sensitive-field guard on the list/filter/search/sort surface.

The bug: build_rest_filters applied queryset.filter(**f) over ANY model field,
and nothing guarded the COUNT the filter produced. A caller holding only list
permission could recover a secret column one character at a time by watching
the row count change:

    ?auth_key__startswith=a   -> count: 1
    ?auth_key__startswith=ab  -> count: 1     (still 1 -> prefix confirmed)

Every test below fails on the pre-fix code and passes after.
"""
from testit import helpers as th

TESTIT_TIER = "core"  # #2792 tier curation

ADMIN = "senfilt_admin"
ADMIN_PWORD = "senfilt##mojo99"
T1 = "senfilt_t1"
T2 = "senfilt_t2"

# Distinct, known auth_key values on the two target users. The probes below
# match T1's prefix and never T2's, so a working oracle shows up as count 1
# where the guard should give 2.
T1_KEY = "ZZZAAA1111111111"
T2_KEY = "ZZZBBB2222222222"


@th.django_unit_setup()
def setup_sensitive_filters(opts):
    """Two target users with known auth_keys, plus an admin who can list them.

    Deletes before creating — these run against a long-lived database.
    """
    from mojo.apps.account.models import User

    User.objects.filter(username__in=[ADMIN, T1, T2]).delete()

    admin = User(username=ADMIN, email=f"{ADMIN}@test.com")
    admin.save()
    admin.is_active = True
    admin.is_email_verified = True
    admin.save_password(ADMIN_PWORD)
    admin.add_permission(["manage_users", "users"])
    admin.save()

    for uname, key in ((T1, T1_KEY), (T2, T2_KEY)):
        u = User(username=uname, email=f"{uname}@test.com")
        u.save()
        u.is_active = True
        u.save()
        # Set auth_key AFTER save so nothing in the save path rotates it.
        User.objects.filter(pk=u.pk).update(auth_key=key)

    opts.t1_id = User.objects.get(username=T1).id
    opts.admin_id = admin.id


def _login(opts):
    opts.client.logout()
    opts.client.login(username=ADMIN, password=ADMIN_PWORD)


def _count(opts, params):
    resp = opts.client.get("/api/user", params=params)
    assert resp.status_code == 200, f"list failed: {resp.status_code} {resp.response}"
    return resp.response.count


@th.unit_test("sensitive_filter_dropped_on_list")
def test_sensitive_filter_dropped_on_list(opts):
    """THE regression: adding an auth_key prefix filter must not change the count."""
    _login(opts)
    base = _count(opts, {"username__startswith": "senfilt_t"})
    assert base == 2, f"fixture is wrong: expected 2 target users, got {base}"

    probed = _count(opts, {"username__startswith": "senfilt_t",
                           "auth_key__startswith": "ZZZA"})
    assert probed == base, (
        f"auth_key filter changed the count ({base} -> {probed}) — this is the "
        f"value-probing oracle: the caller just learned T1's auth_key starts 'ZZZA'")


@th.unit_test("sensitive_filter_dropped_on_count_mode")
def test_sensitive_filter_dropped_on_count_mode(opts):
    """_mode=count shares the same parser and must not leak either."""
    _login(opts)
    resp = opts.client.get("/api/user", params={
        "username__startswith": "senfilt_t", "_mode": "count"})
    assert resp.status_code == 200, f"count mode failed: {resp.response}"
    base = resp.response["count"]
    assert base == 2, f"fixture is wrong: expected 2 target users, got {base}"

    resp = opts.client.get("/api/user", params={
        "username__startswith": "senfilt_t", "auth_key__startswith": "ZZZA",
        "_mode": "count"})
    assert resp.status_code == 200, f"count mode failed: {resp.response}"
    assert resp.response["count"] == base, (
        f"_mode=count leaked the oracle: {base} -> {resp.response['count']}")


@th.unit_test("sensitive_filter_all_operator_forms_dropped")
def test_sensitive_filter_all_operator_forms_dropped(opts):
    """Every operator form is dropped, not just the bare key.

    The guard runs BEFORE the __not/__not_in rewrites, so negation cannot slip
    a sensitive key into the excludes dict — a negated probe is the same
    oracle inverted (the count goes DOWN by the match count).
    """
    from mojo.apps.account.models import User
    from testit.helpers import get_mock_request

    request = get_mock_request()
    probes = {
        "auth_key": "x",
        "auth_key__not": "x",
        "auth_key__in": "a,b",
        "auth_key__isnull": "false",
        "auth_key__startswith": "ZZZ",
        "password__startswith": "pbkdf2",
        "onetime_code": "123456",
    }
    filters, excludes = User.build_rest_filters(request, probes)
    leaked = [k for k in list(filters) + list(excludes)
              if k.split("__")[0] in ("auth_key", "password", "onetime_code")]
    assert not leaked, f"sensitive keys reached the queryset: {leaked}"


@th.unit_test("sensitive_filter_via_relation_traversal")
def test_sensitive_filter_via_relation_traversal(opts):
    """The bypass a top-level-only guard would miss.

    The filter path passes relation lookups through untouched, so a model that
    declares nothing itself still reaches User.auth_key through its user FK.
    The guard has to WALK the path and check each hop's own declaration.
    """
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import File
    from testit.helpers import get_mock_request

    request = get_mock_request()
    filters, excludes = File.build_rest_filters(
        request, {"user__auth_key__startswith": "ZZZA"})
    combined = list(filters) + list(excludes)
    assert not combined, (
        f"relation traversal reached a sensitive field on the related model: {combined}")


@th.unit_test("sensitive_filter_json_drilling_blocked")
def test_sensitive_filter_json_drilling_blocked(opts):
    """__ drilling into a JSON column is refused, matching the aggregation layer."""
    from mojo.apps.account.models import User
    from testit.helpers import get_mock_request

    request = get_mock_request()
    filters, excludes = User.build_rest_filters(
        request, {"metadata__client_secret__startswith": "sk_"})
    combined = list(filters) + list(excludes)
    assert not combined, f"JSON-path drilling was applied verbatim: {combined}"


@th.unit_test("non_sensitive_filters_unaffected")
def test_non_sensitive_filters_unaffected(opts):
    """Guard against an over-broad walk — ordinary filters must still narrow."""
    _login(opts)
    one = _count(opts, {"username": T1})
    assert one == 1, f"an ordinary filter must still narrow, got {one}"

    both = _count(opts, {"username__startswith": "senfilt_t", "is_active": True})
    assert both == 2, f"is_active filter broke: got {both}"


@th.unit_test("sensitive_sort_is_ignored")
def test_sensitive_sort_is_ignored(opts):
    """Sorting by a secret column is a weaker oracle and is ignored, not 400'd."""
    _login(opts)
    resp = opts.client.get("/api/user", params={
        "username__startswith": "senfilt_t", "sort": "auth_key"})
    assert resp.status_code == 200, \
        f"a sensitive sort must be ignored, not rejected: {resp.status_code}"
    assert resp.response.count == 2, \
        f"sensitive sort changed the result set: {resp.response.count}"


@th.unit_test("empty_search_fields_returns_none")
def test_empty_search_fields_returns_none(opts):
    """A search whose every candidate field is sensitive returns NOTHING.

    Regression for a bug the fix itself could have introduced: filtering
    search_fields down to empty leaves Q() per term, and `Q() & Q()` is a
    no-op, so queryset.filter(Q()) would have returned the ENTIRE unfiltered
    list for a search that can match nothing.
    """
    from mojo.apps.account.models import ApiKey
    from testit.helpers import get_mock_request

    request = get_mock_request()
    request.DATA = {"search": "ZZZ"}
    qs = ApiKey.objects.all()
    result = ApiKey.on_rest_list_search(request, qs)
    assert result.count() == 0, (
        f"a search with no usable fields must match nothing, got {result.count()} "
        f"rows — returning the full list here would be worse than the oracle")


@th.unit_test("sensitive_filters_cleanup")
def test_sensitive_filters_cleanup(opts):
    """Remove the fixture users."""
    from mojo.apps.account.models import User

    opts.client.logout()
    User.objects.filter(username__in=[ADMIN, T1, T2]).delete()
