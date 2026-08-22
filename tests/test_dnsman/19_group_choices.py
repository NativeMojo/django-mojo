"""Permission-safe Group choices for DNS credential assignment.

The public endpoint tests run against the dev server. Query-count and malformed
Python-shape tests call the handler/parser in-process; neither path performs a
provider operation.
"""

from testit import helpers as th


PATH = "/api/dnsman/credential/group-choice"
PREFIX = "gc_"


def _make_user(label, perms=None, is_superuser=False):
    from mojo.apps.account.models import User

    email = f"gc_{label}@dnsman.test"
    password = "Gc##choices99"
    user = User.objects.create_user(username=email, email=email, password=password)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.is_superuser = is_superuser
    user.save()
    if perms:
        user.add_permission(perms)
        user.save()
    return user, email, password


def _login(opts, email, password):
    from mojo.decorators.limits import clear_rate_limits

    opts.client.logout()
    clear_rate_limits(ip="127.0.0.1", key="login")
    assert opts.client.login(email, password), f"login failed for {email}"


def _use_apikey(opts, token):
    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = token
    opts.client.is_authenticated = True


def _chain(Group, stem, inactive_hop):
    """Return a leaf whose inactive ancestor is exactly ``inactive_hop`` away."""
    parent = Group.objects.create(
        name=f"gc_chain_{stem}_ancestor_{inactive_hop}",
        kind="organization", is_active=False)
    for hop in range(inactive_hop - 1, 0, -1):
        parent = Group.objects.create(
            name=f"gc_chain_{stem}_ancestor_{hop}",
            kind="organization", parent=parent)
    return Group.objects.create(
        name=f"gc_chain_leaf_{stem}", kind="organization", parent=parent)


@th.django_unit_setup()
def setup_group_choices(opts):
    from mojo.apps.account.models import ApiKey, Group, User
    from mojo.apps.account.services import group_token

    # Long-lived test DB: remove this module's rows before rebuilding fixtures.
    Group.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(username__startswith=PREFIX).delete()

    opts.alpha = Group.objects.create(name="gc_choice_Alpha", kind="organization")
    opts.alpha_lower = Group.objects.create(name="gc_choice_alpha", kind="organization")
    opts.beta = Group.objects.create(name="gc_choice_Beta", kind="organization")
    opts.inactive = Group.objects.create(
        name="gc_choice_Dormant", kind="organization", is_active=False)
    opts.page_ids = []
    for index in range(55):
        group = Group.objects.create(
            name=f"gc_page_{index:03d}", kind="organization")
        opts.page_ids.append(group.pk)

    opts.hop1 = _chain(Group, "hop1", 1)
    opts.hop8 = _chain(Group, "hop8", 8)
    opts.hop9 = _chain(Group, "hop9", 9)

    opts.manager, opts.manager_email, opts.manager_pw = _make_user(
        "manager", ["manage_dns"])
    opts.security, opts.security_email, opts.security_pw = _make_user(
        "security", ["security"])
    opts.viewer, opts.viewer_email, opts.viewer_pw = _make_user(
        "viewer", ["view_dns"])
    opts.nobody, opts.nobody_email, opts.nobody_pw = _make_user("nobody")
    opts.superuser, opts.super_email, opts.super_pw = _make_user(
        "super", is_superuser=True)

    auth_group = Group.objects.create(name="gc_auth_group", kind="organization")
    opts.member, opts.member_email, opts.member_pw = _make_user("member")
    membership = auth_group.add_member(opts.member)
    membership.add_permission(["manage_dns", "security"])

    _, opts.reference_key = ApiKey.create_for_group(
        auth_group, "gc_reference_key",
        permissions={"manage_dns": True, "security": True})

    opts.override_user, _, _ = _make_user("override", ["manage_dns"])
    auth_group.add_member(opts.override_user)
    _, opts.override_key = ApiKey.create_for_group(
        auth_group, "gc_override_key", permissions={},
        user=opts.override_user, override_user=True)

    opts.token_user, _, _ = _make_user("token", ["security"])
    auth_group.add_member(opts.token_user)
    opts.group_token = group_token.mint(opts.token_user, auth_group)


@th.django_unit_test("group-choice permits either global grant and superuser")
def test_allowed_identities(opts):
    for email, password in [
            (opts.manager_email, opts.manager_pw),
            (opts.security_email, opts.security_pw),
            (opts.super_email, opts.super_pw)]:
        _login(opts, email, password)
        response = opts.client.get(PATH, params={"id": opts.alpha.pk})
        assert response.status_code == 200, (
            f"{email} should pass the global OR gate, got {response.status_code}: "
            f"{response.response}")


@th.django_unit_test("group-choice rejects anonymous, unprivileged, and view-only users")
def test_basic_denials(opts):
    opts.client.logout()
    anonymous = opts.client.get(PATH)
    assert anonymous.status_code in (401, 403), \
        f"anonymous group choices returned {anonymous.status_code}"

    for email, password in [
            (opts.nobody_email, opts.nobody_pw),
            (opts.viewer_email, opts.viewer_pw)]:
        _login(opts, email, password)
        response = opts.client.get(PATH)
        assert response.status_code in (401, 403), (
            f"{email} must not enumerate groups, got {response.status_code}")


@th.django_unit_test("member-only DNS grants cannot enumerate global choices")
def test_member_grants_denied(opts):
    _login(opts, opts.member_email, opts.member_pw)
    response = opts.client.get(PATH)
    assert response.status_code in (401, 403), (
        "GroupMember manage_dns/security grants must not satisfy the global gate; "
        f"got {response.status_code}: {response.response}")


@th.django_unit_test("all confined bearer identities are rejected")
def test_confined_credentials_denied(opts):
    for token in (opts.reference_key, opts.override_key):
        _use_apikey(opts, token)
        response = opts.client.get(PATH)
        assert response.status_code in (401, 403), (
            "reference and acting-user ApiKeys must both fail the global gate; "
            f"got {response.status_code}: {response.response}")

    opts.client.logout()
    response = opts.client.get(
        PATH, headers={"Authorization": f"grouptoken {opts.group_token}"})
    assert response.status_code in (401, 403), (
        "a GroupScopedToken must fail even when its real User has a global grant; "
        f"got {response.status_code}: {response.response}")


@th.django_unit_test("active, inactive, and missing ids are indistinguishable to a member")
def test_member_probe_is_uniform(opts):
    _login(opts, opts.member_email, opts.member_pw)
    responses = []
    for group_id in (opts.alpha.pk, opts.inactive.pk, 9223372036854775807):
        response = opts.client.get(PATH, params={"id": group_id})
        responses.append((response.status_code, dict(response.response)))
    for search in ("gc_choice_Alpha", "gc_choice_Dormant", "not-present"):
        response = opts.client.get(PATH, params={"search": search})
        responses.append((response.status_code, dict(response.response)))
    assert all(item == responses[0] for item in responses), (
        "the global gate must run before route lookup and disclose no group state: "
        f"{responses}")


@th.django_unit_test("manage_dns does not widen ordinary Group list visibility")
def test_endpoint_does_not_widen_group_permissions(opts):
    from mojo.apps.account.models import Group
    from mojo.helpers.perms import DOMAIN_CATEGORIES

    assert "dns" not in DOMAIN_CATEGORIES, \
        "adding dns as a category would silently widen permission implication"
    assert not {"view_dns", "manage_dns", "security"}.intersection(
        Group.RestMeta.VIEW_PERMS), \
        "the choice route must not mutate Group.RestMeta permissions"

    _login(opts, opts.manager_email, opts.manager_pw)
    groups = opts.client.get("/api/group")
    assert groups.status_code == 200, \
        f"the existing caller-confined Group list should remain 200, got {groups.status_code}"
    visible_ids = {row["id"] for row in groups.response.data}
    unrelated_ids = {opts.alpha.pk, opts.alpha_lower.pk, opts.beta.pk,
                     *opts.page_ids}
    assert visible_ids.isdisjoint(unrelated_ids), (
        "manage_dns must not widen the ordinary Group list to unrelated choice "
        f"fixtures; visible overlap: {visible_ids.intersection(unrelated_ids)}")
    assert opts.client.get(PATH).status_code == 200, \
        "manage_dns should still open the minimal credential choice route"


@th.django_unit_test("choice rows expose exactly id and name")
def test_minimal_row_shape(opts):
    _login(opts, opts.manager_email, opts.manager_pw)
    response = opts.client.get(PATH, params={"id": opts.alpha.pk})
    assert response.status_code == 200, response.response
    assert response.response.status is True
    assert response.response.start == 0
    assert response.response.size == 1
    assert response.response.count == 1
    assert len(response.response.data) == 1
    assert set(response.response.data[0].keys()) == {"id", "name"}, (
        f"choice rows must be minimal, got {response.response.data[0]}")


@th.django_unit_test("search is trimmed, case-insensitive, and counted before paging")
def test_search_contract(opts):
    _login(opts, opts.manager_email, opts.manager_pw)
    response = opts.client.get(
        PATH, params={"search": "  CHOICE_a  ", "start": 1, "size": 1})
    assert response.status_code == 200, response.response
    assert response.response.count == 2, \
        f"count must describe both search hits, got {response.response}"
    assert len(response.response.data) == 1, \
        f"the requested page should contain one row, got {response.response.data}"
    assert response.response.start == 1 and response.response.size == 1

    too_long = opts.client.get(PATH, params={"search": "x" * 101})
    assert too_long.status_code == 400, \
        f"search over 100 characters must be rejected, got {too_long.status_code}"


@th.django_unit_test("paging defaults, bounds, count, and ordering are fixed")
def test_paging_and_ordering(opts):
    _login(opts, opts.manager_email, opts.manager_pw)

    default = opts.client.get(PATH, params={"search": "gc_page_"})
    assert default.status_code == 200, default.response
    assert default.response.start == 0 and default.response.size == 25
    assert default.response.count == 55 and len(default.response.data) == 25

    maximum = opts.client.get(
        PATH, params={"search": "gc_page_", "start": 50, "size": 50})
    assert maximum.status_code == 200, maximum.response
    assert maximum.response.count == 55 and len(maximum.response.data) == 5

    ordered = opts.client.get(PATH, params={"search": "gc_choice_", "size": 50})
    rows = [(row["name"], row["id"]) for row in ordered.response.data]
    expected = sorted(rows, key=lambda row: (row[0].lower(), row[1]))
    assert rows == expected, f"expected Lower(name), id ordering, got {rows}"
    assert opts.inactive.pk not in {row["id"] for row in ordered.response.data}

    for params in [
            {"start": -1}, {"start": 100001}, {"size": 0}, {"size": 51}]:
        response = opts.client.get(PATH, params=params)
        assert response.status_code == 400, \
            f"out-of-range paging {params} returned {response.status_code}"


@th.django_unit_test("exact lookup is exclusive and inactive/missing ids are empty successes")
def test_exact_lookup_contract(opts):
    _login(opts, opts.manager_email, opts.manager_pw)
    for group_id, expected_count in [
            (opts.alpha.pk, 1), (opts.inactive.pk, 0),
            (9223372036854775807, 0)]:
        response = opts.client.get(PATH, params={"id": group_id})
        assert response.status_code == 200, response.response
        assert response.response.start == 0 and response.response.size == 1
        assert response.response.count == expected_count
        assert len(response.response.data) == expected_count

    for extra in ("search", "start", "size"):
        response = opts.client.get(
            PATH, params={"id": opts.alpha.pk, extra: 1})
        assert response.status_code == 400, \
            f"id mixed with {extra} returned {response.status_code}"
    for invalid_id in (0, -1, 9223372036854775808):
        response = opts.client.get(PATH, params={"id": invalid_id})
        assert response.status_code == 400, \
            f"out-of-range id {invalid_id} returned {response.status_code}"


@th.django_unit_test("raw query parsing rejects duplicate, shaped, and generic controls")
def test_strict_raw_query_shapes(opts):
    _login(opts, opts.manager_email, opts.manager_pw)
    cases = [
        f"{PATH}?id={opts.alpha.pk}&id={opts.beta.pk}",
        f"{PATH}?search=gc&search=choice",
        f"{PATH}?id%5B0%5D={opts.alpha.pk}",
        f"{PATH}?id=true",
        f"{PATH}?id=1.0",
        f"{PATH}?search.term=private-marker",
        f"{PATH}?unknown=private-marker",
        f"{PATH}?graph=private-marker",
        f"{PATH}?sort=private-marker",
        f"{PATH}?format=private-marker",
        f"{PATH}?_mode=private-marker",
        f"{PATH}?group={opts.alpha.pk}",
    ]
    for url in cases:
        response = opts.client.get(url)
        assert response.status_code == 400, \
            f"strict query parser accepted {url!r}: {response.response}"
        body = str(response.response)
        assert "private-marker" not in body and len(body) < 300, (
            f"the bounded error must not reflect query input: {body}")


@th.django_unit_test("integer parser rejects malformed and non-scalar Python shapes")
def test_integer_parser_shapes(opts):
    from mojo import errors as me
    from mojo.apps.dnsman.rest.credential import _group_choice_integer

    for value in (True, False, [], {}, None, "", " 1", "1 ", "1.0", "x"):
        with th.assert_raises(me.ValueException):
            _group_choice_integer(value, 0, 50)


@th.django_unit_test("eligible queryset matches Group.get_active through hops 1, 8, and 9")
def test_effective_active_depth_parity(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.dnsman.rest.credential import _credential_group_choices

    expected = {
        opts.hop1.pk: Group.get_active(opts.hop1.pk) is not None,
        opts.hop8.pk: Group.get_active(opts.hop8.pk) is not None,
        opts.hop9.pk: Group.get_active(opts.hop9.pk) is not None,
    }
    actual = set(_credential_group_choices().filter(
        pk__in=expected).values_list("pk", flat=True))
    assert expected == {
        opts.hop1.pk: False, opts.hop8.pk: False, opts.hop9.pk: False}, expected
    assert actual == set(), \
        f"inactive or over-depth ancestry must fail closed; got {actual}"

    _login(opts, opts.manager_email, opts.manager_pw)
    response = opts.client.get(PATH, params={"search": "gc_chain_leaf_", "size": 50})
    assert {row["id"] for row in response.response.data} == set(), \
        f"the HTTP list must use the same eligibility policy: {response.response}"


@th.django_unit_test("exact and list modes keep fixed Group query counts")
def test_query_counts(opts):
    from django.db import connection
    from django.test import RequestFactory
    from django.test.utils import CaptureQueriesContext
    from mojo import errors as me
    from mojo.apps.dnsman.rest.credential import on_credential_group_choice

    factory = RequestFactory()

    def run(params=None, path=PATH, expect_error=False):
        request = factory.get(path) if params is None else factory.get(path, data=params)
        request.user = opts.manager
        # CaptureQueriesContext records start/end indexes into Django's bounded
        # query deque.  Once a long suite fills that deque both indexes remain
        # at its max length while new queries rotate through, yielding an empty
        # capture.  Query-count assertions must start with their own clean log.
        connection.queries_log.clear()
        with CaptureQueriesContext(connection) as captured:
            try:
                result = on_credential_group_choice(request)
            except me.ValueException:
                if not expect_error:
                    raise
                result = None
        group_queries = [entry["sql"] for entry in captured.captured_queries
                         if "account_group" in entry["sql"]]
        return result, group_queries

    def assert_minimal_projection(queries):
        data_query = next(sql for sql in queries if "COUNT(" not in sql.upper())
        select_clause = data_query.partition(" FROM ")[0]
        assert '"account_group"."id"' in select_clause, select_clause
        assert '"account_group"."name"' in select_clause, select_clause
        assert "is_active" not in select_clause and "parent_id" not in select_clause, (
            f"choice query selected fields beyond id/name: {select_clause}")

    # Reproduce the full-suite condition locally so this test cannot regress
    # to depending on how many database queries earlier modules happened to
    # execute.
    query_limit = connection.queries_log.maxlen
    connection.queries_log.extend(
        [{"sql": "-- earlier suite query", "time": "0"}] * query_limit)
    assert len(connection.queries_log) == query_limit, \
        "the regression setup must saturate Django's bounded query log"

    exact, exact_queries = run({"id": opts.alpha.pk})
    assert exact["count"] == 1 and len(exact_queries) == 1, exact_queries
    assert_minimal_projection(exact_queries)

    exact_miss, exact_miss_queries = run({"id": 9223372036854775807})
    assert exact_miss["count"] == 0 and len(exact_miss_queries) == 1, \
        exact_miss_queries
    assert_minimal_projection(exact_miss_queries)

    listed, list_queries = run({"size": 10})
    assert listed["data"] and len(list_queries) == 2, list_queries
    assert_minimal_projection(list_queries)

    searched, search_queries = run({"search": "gc_page_", "size": 10})
    assert searched["count"] == 55 and len(search_queries) == 2, search_queries

    empty_page, empty_queries = run(
        {"search": "gc_page_", "start": 100000, "size": 10})
    assert empty_page["data"] == [] and len(empty_queries) == 2, empty_queries

    for params, path in [
            ({"id": 9223372036854775808}, PATH),
            ({"start": 100001}, PATH),
            ({"size": 51}, PATH),
            (None, f"{PATH}?id=1&id=2")]:
        result, invalid_queries = run(
            params=params, path=path, expect_error=True)
        assert result is None and invalid_queries == [], (
            f"invalid query input must fail before touching Group: {invalid_queries}")


@th.django_unit_test("deactivated choice cannot be linked and creates no credential")
def test_deactivated_choice_link_fails_before_persistence(opts):
    from unittest.mock import patch
    from django.test import RequestFactory
    from mojo import errors as me
    from mojo.apps.account.models import Group
    from mojo.apps.dnsman.models import DnsCredential
    from mojo.apps.dnsman.rest import credential as credential_rest

    group = Group.objects.create(name="gc_link_target", kind="organization")
    _login(opts, opts.manager_email, opts.manager_pw)
    discover = opts.client.get(PATH, params={"id": group.pk})
    assert discover.status_code == 200 and discover.response.count == 1

    group.is_active = False
    group.save(update_fields=["is_active", "modified"])
    hidden = opts.client.get(PATH, params={"id": group.pk})
    assert hidden.status_code == 200 and hidden.response.count == 0

    before = DnsCredential.objects.count()
    linked = opts.client.post("/api/dnsman/credential/link", json={
        "group": group.pk,
        "provider": "godaddy",
        "api_key": "must-not-reach-provider",
        "api_secret": "must-not-persist",
    })
    assert linked.status_code == 400, \
        f"an inactive selected group must be refused, got {linked.status_code}"
    assert DnsCredential.objects.count() == before, \
        "a refused link must persist no credential"

    # The live-server assertion above proves dispatch re-resolves the stale id.
    # This in-process handler probe instruments the provider boundary itself:
    # an unresolved request.group must refuse before onboarding is called.
    request = RequestFactory().post("/api/dnsman/credential/link")
    request.user = opts.manager
    request.group = None
    request.DATA = {
        "group": group.pk,
        "provider": "godaddy",
        "api_key": "must-not-reach-provider",
        "api_secret": "must-not-persist",
    }
    with patch.object(
            credential_rest.onboarding, "link_credential",
            side_effect=AssertionError("stale group reached provider onboarding")) as link:
        with th.assert_raises(me.ValueException):
            credential_rest.on_credential_link(request)
    assert link.call_count == 0, \
        "stale choice must be rejected before onboarding/provider verification"
