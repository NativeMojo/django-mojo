"""Permission-equivalent metrics account/category/slug discovery."""

from testit import helpers as th


PATH = "/api/metrics/discover"
PASSWORD = "T1438##metrics99"

CUSTOM_ALLOWED = "t1438_custom_allowed"
CUSTOM_HIDDEN = "t1438_custom_hidden"
CUSTOM_PUBLIC = "t1438_custom_public"
DATA_ONLY = "t1438_data_only"
CUSTOM_PERMISSION = "t1438_view_custom"
HIDDEN_PERMISSION = "t1438_hidden_custom"

CATEGORY_ALPHA = "t1438_alpha"
CATEGORY_BETA = "t1438_Beta"
CATEGORY_STATUS = "t1438_status"
CATEGORIES = (CATEGORY_ALPHA, CATEGORY_BETA, CATEGORY_STATUS)

SLUG_ALPHA = "t1438_alpha_metric"
SLUG_BETA = "t1438_beta_metric"
SLUG_API = "t1438:api:status:200"
SLUG_JOB = "t1438:job:status:200"
SLUGS = (SLUG_ALPHA, SLUG_BETA, SLUG_API, SLUG_JOB)


def _clear_metrics_account(account):
    from mojo.apps import metrics

    for category in CATEGORIES:
        metrics.delete_category(category, account=account)
    for slug in SLUGS:
        metrics.delete_metrics_slug(slug, account=account)
    if account not in ("public", "global"):
        metrics.delete_account(account)


def _make_user(label, permissions=None):
    from mojo.apps.account.models import User

    username = f"t1438_{label}"
    user = User.objects.create_user(
        username=username, email=f"{username}@metrics.test", password=PASSWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    if permissions:
        user.add_permission(permissions)
        user.save()
    return user


def _login(opts, user):
    from mojo.decorators.limits import clear_rate_limits

    opts.client.logout()
    clear_rate_limits(ip="127.0.0.1", key="login")
    ok = opts.client.login(user.username, PASSWORD)
    assert ok, f"metrics discovery login failed for {user.username}"


def _use_apikey(opts, token):
    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = token
    opts.client.is_authenticated = True


@th.django_unit_setup()
def setup_metrics_discovery(opts):
    from mojo.apps import metrics
    from mojo.apps.account.models import ApiKey, Group, GroupMember, User
    from mojo.apps.account.services import group_token

    old_groups = list(Group.objects.filter(
        name__startswith="t1438_").values_list("id", flat=True))
    old_users = list(User.objects.filter(
        username__startswith="t1438_").values_list("id", flat=True))
    cleanup_accounts = {
        CUSTOM_ALLOWED, CUSTOM_HIDDEN, CUSTOM_PUBLIC, DATA_ONLY,
        "public", "global",
        *(f"group-{group_id}" for group_id in old_groups),
        *(f"user-{user_id}" for user_id in old_users),
    }
    for account in cleanup_accounts:
        _clear_metrics_account(account)

    ApiKey.objects.filter(name__startswith="t1438_").delete()
    GroupMember.objects.filter(group_id__in=old_groups).delete()
    GroupMember.objects.filter(user_id__in=old_users).delete()
    Group.objects.filter(id__in=old_groups).delete()
    User.objects.filter(id__in=old_users).delete()

    opts.ordinary = _make_user("ordinary")
    opts.global_view = _make_user("global_view", ["view_metrics"])
    opts.global_metrics = _make_user("global_metrics", ["metrics"])
    opts.custom_reader = _make_user("custom_reader", [CUSTOM_PERMISSION])
    opts.override_user = _make_user("override_user", ["view_metrics"])
    opts.token_user = _make_user("token_user", ["view_metrics"])
    opts.account_user = _make_user("account_user")

    opts.group_a = Group.objects.create(name="t1438_group_a", kind="organization")
    opts.group_b = Group.objects.create(name="t1438_group_b", kind="organization")
    opts.account_group_a = f"group-{opts.group_a.pk}"
    opts.account_group_b = f"group-{opts.group_b.pk}"
    opts.account_user_name = f"user-{opts.account_user.pk}"

    member = opts.group_a.add_member(opts.ordinary)
    member.add_permission("view_metrics")
    override_member = opts.group_a.add_member(opts.override_user)
    override_member.add_permission("view_metrics")
    token_member = opts.group_a.add_member(opts.token_user)
    token_member.add_permission("view_metrics")

    _, opts.reference_key = ApiKey.create_for_group(
        opts.group_a, "t1438_reference_key",
        permissions={"view_metrics": True})
    _, opts.override_key = ApiKey.create_for_group(
        opts.group_a, "t1438_override_key", permissions={},
        user=opts.override_user, override_user=True)
    opts.group_token = group_token.mint(opts.token_user, opts.group_a)

    metrics.set_view_perms(CUSTOM_ALLOWED, CUSTOM_PERMISSION)
    metrics.set_view_perms(CUSTOM_HIDDEN, HIDDEN_PERMISSION)
    metrics.set_view_perms(CUSTOM_PUBLIC, "public")

    metrics.record(SLUG_ALPHA, category=CATEGORY_ALPHA, account=CUSTOM_ALLOWED)
    metrics.record(SLUG_BETA, category=CATEGORY_BETA, account=CUSTOM_ALLOWED)
    metrics.record(SLUG_API, category=CATEGORY_STATUS, account=CUSTOM_ALLOWED)
    metrics.record(SLUG_JOB, category=CATEGORY_STATUS, account=CUSTOM_ALLOWED)
    metrics.record(SLUG_ALPHA, category=CATEGORY_ALPHA, account=CUSTOM_HIDDEN)
    metrics.record(SLUG_ALPHA, category=CATEGORY_ALPHA, account=CUSTOM_PUBLIC)
    metrics.record(SLUG_ALPHA, category=CATEGORY_ALPHA, account=DATA_ONLY)
    metrics.record(SLUG_ALPHA, category=CATEGORY_ALPHA, account=opts.account_group_a)
    metrics.record(SLUG_BETA, category=CATEGORY_BETA, account=opts.account_group_b)
    metrics.record(SLUG_ALPHA, category=CATEGORY_ALPHA, account=opts.account_user_name)
    metrics.record(SLUG_ALPHA, category=CATEGORY_ALPHA, account="public")
    metrics.record(SLUG_BETA, category=CATEGORY_BETA, account="global")


@th.django_unit_test("discovery rejects missing, unknown, duplicate, and shaped grammar")
def test_strict_wire_grammar(opts):
    cases = [
        PATH,
        f"{PATH}?resource=unknown",
        f"{PATH}?resource=accounts&account=x",
        f"{PATH}?resource=accounts&category=x",
        f"{PATH}?resource=categories",
        f"{PATH}?resource=categories&account=x&category=y",
        f"{PATH}?resource=slugs",
        f"{PATH}?resource=accounts&resource=slugs",
        f"{PATH}?resource=accounts&search=x&search=y",
        f"{PATH}?resource=accounts&search%5B0%5D=x",
        f"{PATH}?resource=accounts&search.term=x",
        f"{PATH}?resource=accounts&sort=name",
        f"{PATH}?resource=accounts&start=nope",
        f"{PATH}?resource=accounts&start=-1",
        f"{PATH}?resource=accounts&size=0",
        f"{PATH}?resource=accounts&size=501",
    ]
    opts.client.logout()
    for url in cases:
        response = opts.client.get(url)
        assert response.status_code == 400, (
            f"strict discovery grammar accepted {url!r}: "
            f"{response.status_code} {response.response}")

    for params in [
            {"resource": "categories", "account": "x" * 257},
            {"resource": "slugs", "account": "public", "category": "x" * 257},
            {"resource": "accounts", "search": "x" * 129}]:
        response = opts.client.get(PATH, params=params)
        assert response.status_code == 400, (
            f"overlong discovery filter was accepted: {params}")


@th.django_unit_test("direct parser rejects non-string and non-scalar shapes")
def test_direct_parser_shapes(opts):
    from mojo import errors as me
    from mojo.apps.metrics.rest.discovery import _parse_query

    class FakeRequest:
        pass

    cases = [
        {"resource": "accounts", "search": True},
        {"resource": "categories", "account": ["public"]},
        {"resource": "accounts", "start": False},
        {"resource": "accounts", "size": {}},
        {"resource": "accounts", "search": ["a", "b"]},
        {"resource": "accounts", "search": {"term": "x"}},
        {"resource": "accounts", "unknown": "x"},
    ]
    for data in cases:
        request = FakeRequest()
        request.DATA = data
        with th.assert_raises(me.ValueException):
            _parse_query(request)


@th.django_unit_test("pagination sorts, counts before slicing, and reports next_start")
def test_pagination_helper(opts):
    from mojo.apps.metrics.rest.discovery import _paginate

    page = _paginate(["zeta", "alpha", "Beta", "alpha"], 1, 2)
    assert page == {
        "data": ["alpha", "zeta"],
        "start": 1,
        "size": 2,
        "count": 3,
        "page_count": 2,
        "next_start": None,
    }, f"unexpected deterministic discovery page: {page}"

    first = _paginate(["c", "a", "b"], 0, 2)
    assert first["next_start"] == 2 and first["page_count"] == 2, (
        f"first page should advertise start 2: {first}")
    beyond = _paginate(["a", "b"], 10, 2)
    assert beyond["data"] == [] and beyond["count"] == 2, (
        f"out-of-range page must retain total count: {beyond}")
    assert beyond["page_count"] == 0 and beyond["next_start"] is None, (
        f"out-of-range page metadata is wrong: {beyond}")


@th.django_unit_test("metric slug writes maintain one idempotent account membership")
def test_add_metrics_slug_indexes_account(opts):
    from mojo.apps import metrics
    from mojo.apps.metrics import redis_metrics

    account = "t1438_index_probe"
    _clear_metrics_account(account)
    try:
        assert DATA_ONLY in metrics.list_accounts(), (
            "record() must index a previously data-only account")
        redis_metrics.add_metrics_slug(SLUG_ALPHA, account=account)
        redis_metrics.add_metrics_slug(SLUG_ALPHA, account=account)
        accounts = metrics.list_accounts()
        assert accounts.count(account) == 1, (
            f"account index must contain one idempotent membership: {accounts}")
        assert SLUG_ALPHA in metrics.get_account_slugs(account), (
            "direct add_metrics_slug must still maintain the slug registry")
    finally:
        _clear_metrics_account(account)


@th.django_unit_test("candidate inventory is cardinality-capped and never scans gauges")
def test_candidate_account_bound_and_sources(opts):
    from unittest.mock import patch
    from mojo import errors as me
    from mojo.apps.metrics.rest import discovery

    class FakeRedis:
        def __init__(self, cardinality, members=None):
            self.cardinality = cardinality
            self.members = members or set()
            self.materialized = False
            self.calls = []

        def eval(self, script, key_count, key, limit):
            self.calls.append((script, key_count, key, limit))
            if self.cardinality > limit:
                return [self.cardinality]
            self.materialized = True
            return [self.cardinality, list(self.members)]

        def scard(self, key):
            raise AssertionError("non-atomic SCARD used")

        def smembers(self, key):
            raise AssertionError("non-atomic SMEMBERS used")

    over = FakeRedis(100000)
    with patch.object(discovery.redis, "get_connection", return_value=over):
        with th.assert_raises(me.ValueException):
            discovery._candidate_accounts()
    assert over.materialized is False, (
        "atomic account cap must reject without materializing an oversized set")
    assert len(over.calls) == 1 and over.calls[0][1:] == (
        1, discovery.utils.generate_accounts_key(),
        discovery.DISCOVERY_MAX_ACCOUNTS), (
        f"account inventory must use one keyed bounded Lua call: {over.calls}")
    assert "SCARD" in over.calls[0][0] and "SMEMBERS" in over.calls[0][0], (
        "bounded Lua call must own both cardinality and member reads")

    bounded = FakeRedis(1, {b"t1438_from_index"})
    with patch.object(discovery.redis, "get_connection", return_value=bounded), \
            patch.object(discovery.metrics, "list_accounts_with_data",
                         side_effect=AssertionError("historical scan used")), \
            patch.object(discovery.metrics, "list_gauge_slugs",
                         side_effect=AssertionError("gauge scan used")):
        candidates = discovery._candidate_accounts()
    assert candidates == {"t1438_from_index", "public", "global"}, (
        f"candidate inventory must use only the maintained index: {candidates}")


@th.django_unit_test("account catalog requires the global gate and hides policy failures")
def test_account_catalog_permissions_and_count(opts):
    opts.client.logout()
    anonymous = opts.client.get(PATH, params={"resource": "accounts"})
    assert anonymous.status_code == 403, (
        f"anonymous account enumeration must be denied: {anonymous.response}")

    _login(opts, opts.ordinary)
    member_only = opts.client.get(PATH, params={"resource": "accounts"})
    assert member_only.status_code == 403, (
        f"group-only permission must not enumerate accounts: {member_only.response}")

    _login(opts, opts.custom_reader)
    custom_only = opts.client.get(PATH, params={"resource": "accounts"})
    assert custom_only.status_code == 403, (
        f"custom-account permission must not open the global catalog: {custom_only.response}")

    for user in (opts.global_view, opts.global_metrics):
        _login(opts, user)
        response = opts.client.get(PATH, params={
            "resource": "accounts", "search": "T1438_CUSTOM_"})
        assert response.status_code == 200, (
            f"global metrics reader should enumerate filtered accounts: {response.response}")
        assert response.response.data == [CUSTOM_PUBLIC], (
            "global view_metrics/metrics must not bypass custom policy; "
            f"got {response.response.data}")
        assert response.response.count == 1 and response.response.page_count == 1, (
            f"hidden candidates must not affect counts: {response.response}")
        assert set(response.response.filters.keys()) == {"search"}, (
            f"account filters shape drifted: {response.response.filters}")


@th.django_unit_test("reference key catalog is tenant-filtered; assumed identities cannot enumerate")
def test_restricted_account_catalog(opts):
    _use_apikey(opts, opts.reference_key)
    reference = opts.client.get(PATH, params={
        "resource": "accounts", "search": "group-", "size": 500})
    assert reference.status_code == 200, (
        f"reference key's own global grant should pass the catalog gate: {reference.response}")
    assert opts.account_group_a in reference.response.data, (
        f"reference key must see its own group account: {reference.response}")
    assert opts.account_group_b not in reference.response.data, (
        f"reference key must not see another group: {reference.response}")

    _use_apikey(opts, opts.override_key)
    override = opts.client.get(PATH, params={"resource": "accounts"})
    assert override.status_code == 403, (
        "override key must not use its acting user's global grant to enumerate; "
        f"got {override.response}")

    opts.client.logout()
    token = opts.client.get(
        PATH, params={"resource": "accounts"},
        headers={"Authorization": f"grouptoken {opts.group_token}"})
    assert token.status_code == 403, (
        f"group token must not enumerate global accounts: {token.response}")


@th.django_unit_test("categories use live custom/public policy and stable pages")
def test_category_discovery(opts):
    opts.client.logout()
    public = opts.client.get(PATH, params={
        "resource": "categories", "account": CUSTOM_PUBLIC})
    assert public.status_code == 200, (
        f"custom-public categories should remain anonymous: {public.response}")
    assert public.response.data == [CATEGORY_ALPHA], (
        f"unexpected custom-public categories: {public.response}")
    assert public.response.start == 0 and public.response.size == 50, (
        f"default discovery page should be start=0,size=50: {public.response}")

    public_account = opts.client.get(PATH, params={
        "resource": "categories", "account": "public", "size": 500})
    assert public_account.status_code == 200, (
        f"literal public categories should remain anonymous: {public_account.response}")
    assert public_account.response.size == 500, (
        f"maximum page size 500 should be accepted: {public_account.response}")

    public_slugs = opts.client.get(PATH, params={
        "resource": "slugs", "account": CUSTOM_PUBLIC})
    assert public_slugs.status_code == 200 and SLUG_ALPHA in public_slugs.response.data, (
        f"custom-public slugs should remain anonymous: {public_slugs.response}")

    literal_public_slugs = opts.client.get(PATH, params={
        "resource": "slugs", "account": "public"})
    assert literal_public_slugs.status_code == 200, (
        f"literal public slugs should remain anonymous: {literal_public_slugs.response}")

    _login(opts, opts.global_view)
    denied = opts.client.get(PATH, params={
        "resource": "categories", "account": CUSTOM_ALLOWED})
    assert denied.status_code == 403, (
        f"global grant must not bypass a custom policy: {denied.response}")

    _login(opts, opts.custom_reader)
    page = opts.client.get(PATH, params={
        "resource": "categories", "account": CUSTOM_ALLOWED,
        "start": 1, "size": 1})
    expected = sorted(CATEGORIES)
    assert page.status_code == 200 and page.response.data == expected[1:2], (
        f"categories must sort before paging: {page.response}")
    assert page.response.count == 3 and page.response.page_count == 1, (
        f"category count/page_count mismatch: {page.response}")
    assert page.response.next_start == 2, (
        f"middle page should advertise next start 2: {page.response}")
    assert set(page.response.filters.keys()) == {"account", "search"}, (
        f"category filters shape drifted: {page.response.filters}")

    searched = opts.client.get(PATH, params={
        "resource": "categories", "account": CUSTOM_ALLOWED,
        "search": "ALP"})
    assert searched.response.data == [CATEGORY_ALPHA] and searched.response.count == 1, (
        f"case-insensitive search must run before count: {searched.response}")

    beyond = opts.client.get(PATH, params={
        "resource": "categories", "account": CUSTOM_ALLOWED, "start": 100})
    assert beyond.response.data == [] and beyond.response.count == 3, (
        f"out-of-range page must retain visible count: {beyond.response}")
    assert beyond.response.page_count == 0 and beyond.response.next_start is None, (
        f"out-of-range category metadata is wrong: {beyond.response}")


@th.django_unit_test("slug discovery preserves full strings, category, search, and series fidelity")
def test_slug_discovery_and_series_fidelity(opts):
    _login(opts, opts.custom_reader)
    response = opts.client.get(PATH, params={
        "resource": "slugs", "account": CUSTOM_ALLOWED,
        "category": CATEGORY_STATUS, "search": "STATUS:200"})
    assert response.status_code == 200, (
        f"full slug discovery failed: {response.response}")
    assert response.response.data == sorted([SLUG_API, SLUG_JOB]), (
        f"discovery must preserve distinct full registry strings: {response.response}")
    assert response.response.count == 2 and response.response.page_count == 2, (
        f"slug search/category counts are wrong: {response.response}")
    assert set(response.response.filters.keys()) == {"account", "category", "search"}, (
        f"slug filters shape drifted: {response.response.filters}")
    required = {
        "status", "resource", "filters", "data", "start", "size", "count",
        "page_count", "next_start",
    }
    assert required.issubset(response.response.keys()), (
        f"discovery envelope is missing required fields: {response.response}")

    all_slugs = opts.client.get(PATH, params={
        "resource": "slugs", "account": CUSTOM_ALLOWED, "size": 500})
    assert SLUG_API in all_slugs.response.data and SLUG_JOB in all_slugs.response.data, (
        f"account registry lost full slug canaries: {all_slugs.response}")
    assert all_slugs.response.filters["category"] is None, (
        f"unfiltered slug page should report category null: {all_slugs.response}")

    missing = opts.client.get(PATH, params={
        "resource": "slugs", "account": CUSTOM_ALLOWED,
        "category": "t1438_missing"})
    assert missing.status_code == 200 and missing.response.data == [], (
        f"missing category must be an authorized empty page: {missing.response}")

    unmatched = opts.client.get(PATH, params={
        "resource": "slugs", "account": CUSTOM_ALLOWED,
        "search": "not-present"})
    assert unmatched.status_code == 200 and unmatched.response.count == 0, (
        f"unmatched slug search must be an empty success: {unmatched.response}")

    series = opts.client.get("/api/metrics/series", params={
        "account": CUSTOM_ALLOWED,
        "slugs": f"{SLUG_API},{SLUG_JOB}",
        "granularity": "hours"})
    assert series.status_code == 200, (
        f"series fetch of discovered full slugs failed: {series.response}")
    assert set(series.response["data"].keys()) == {SLUG_API, SLUG_JOB}, (
        f"series must preserve distinct full slug keys: {series.response}")


@th.django_unit_test("reference, override, and group tokens keep direct tenant bounds")
def test_restricted_direct_discovery(opts):
    for token in (opts.reference_key, opts.override_key):
        _use_apikey(opts, token)
        own = opts.client.get(PATH, params={
            "resource": "categories", "account": opts.account_group_a})
        assert own.status_code == 200, (
            f"confined key should read its own group categories: {own.response}")
        other = opts.client.get(PATH, params={
            "resource": "categories", "account": opts.account_group_b})
        assert other.status_code == 403, (
            f"confined key must not read another group: {other.response}")

    opts.client.logout()
    headers = {"Authorization": f"grouptoken {opts.group_token}"}
    own = opts.client.get(PATH, params={
        "resource": "slugs", "account": opts.account_group_a}, headers=headers)
    assert own.status_code == 200, (
        f"group token should read its signed group: {own.response}")
    other = opts.client.get(PATH, params={
        "resource": "slugs", "account": opts.account_group_b}, headers=headers)
    assert other.status_code == 403, (
        f"group token must not read another group: {other.response}")


@th.django_unit_test("reserved account syntax and nonexistent-group behavior match the helper")
def test_reserved_account_semantics(opts):
    _login(opts, opts.global_view)
    global_user = opts.client.get(PATH, params={
        "resource": "categories", "account": opts.account_user_name})
    assert global_user.status_code == 200, (
        f"global reader should retain user-account access: {global_user.response}")

    _login(opts, opts.account_user)
    own_user = opts.client.get(PATH, params={
        "resource": "categories", "account": opts.account_user_name})
    assert own_user.status_code == 200, (
        f"user should retain own-account discovery: {own_user.response}")

    _login(opts, opts.ordinary)
    other_user = opts.client.get(PATH, params={
        "resource": "categories", "account": opts.account_user_name})
    assert other_user.status_code == 403, (
        f"ordinary user must not discover another user account: {other_user.response}")

    _login(opts, opts.global_view)
    missing = opts.client.get(PATH, params={
        "resource": "categories", "account": "group-9223372036854775807"})
    assert missing.status_code == 200 and missing.response.data == [], (
        "global reader should retain helper-authorized empty behavior for a "
        f"valid missing group: {missing.response}")

    malformed = opts.client.get(PATH, params={
        "resource": "categories", "account": "user-not-an-id"})
    assert malformed.status_code == 403, (
        f"malformed reserved account must fail generically: {malformed.response}")

    _use_apikey(opts, opts.reference_key)
    confined_missing = opts.client.get(PATH, params={
        "resource": "categories", "account": "group-9223372036854775807"})
    assert confined_missing.status_code == 403, (
        "confined identity must deny a nonexistent group without an inventory "
        f"probe: {confined_missing.response}")


@th.django_unit_test("authorization and grammar complete before registry reads")
def test_authorization_precedes_registry_reads(opts):
    from unittest.mock import patch
    from django.test import RequestFactory
    from mojo import errors as me
    from mojo.apps.metrics.rest import discovery
    from mojo.helpers.request_parser import parse_request_data

    factory = RequestFactory()

    def make_request(url, user):
        request = factory.get(url)
        request.DATA = parse_request_data(request)
        request.user = user
        return request

    account_request = make_request(
        f"{PATH}?resource=accounts", opts.ordinary)
    with patch.object(
            discovery, "_candidate_accounts",
            side_effect=AssertionError("candidate index preceded global gate")) as candidates:
        with th.assert_raises(me.PermissionDeniedException):
            discovery.on_metrics_discover(account_request)
    assert candidates.call_count == 0, (
        "account catalog must pass the global gate before reading its index")

    cases = [
        (f"{PATH}?resource=categories&account={CUSTOM_HIDDEN}",
         me.PermissionDeniedException),
        (f"{PATH}?resource=categories&account=user-bad", me.PermissionDeniedException),
        (f"{PATH}?resource=categories", me.ValueException),
    ]
    for url, error_type in cases:
        request = make_request(url, opts.global_view)
        with patch.object(
                discovery.metrics, "get_categories",
                side_effect=AssertionError("registry read preceded authorization")) as read:
            with th.assert_raises(error_type):
                discovery.on_metrics_discover(request)
        assert read.call_count == 0, (
            f"denied/malformed request reached the category registry: {url}")

    request = make_request(
        f"{PATH}?resource=slugs&account={CUSTOM_HIDDEN}", opts.global_view)
    with patch.object(
            discovery.metrics, "get_account_slugs",
            side_effect=AssertionError("slug read preceded authorization")) as read:
        with th.assert_raises(me.PermissionDeniedException):
            discovery.on_metrics_discover(request)
    assert read.call_count == 0, (
        "denied account reached the all-slug registry")

    request = make_request(
        f"{PATH}?resource=slugs&account={CUSTOM_HIDDEN}&category={CATEGORY_ALPHA}",
        opts.global_view)
    with patch.object(
            discovery.metrics, "get_category_slugs",
            side_effect=AssertionError("category-slug read preceded authorization")) as read:
        with th.assert_raises(me.PermissionDeniedException):
            discovery.on_metrics_discover(request)
    assert read.call_count == 0, (
        "denied account reached the category-slug registry")


@th.django_unit_test("unexpected account-filter failures abort instead of returning partial data")
def test_visible_accounts_propagates_backend_errors(opts):
    from unittest.mock import patch
    from mojo import errors as me
    from mojo.apps.metrics.rest import discovery

    def check(request, account):
        if account == "t1438_hidden":
            raise me.PermissionDeniedException()
        if account == "t1438_broken":
            raise RuntimeError("redis unavailable")

    with patch.object(discovery, "check_view_permissions", side_effect=check):
        with th.assert_raises(RuntimeError):
            discovery._visible_accounts(
                object(), {"t1438_visible", "t1438_hidden", "t1438_broken"})
