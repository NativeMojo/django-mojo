"""
Unit tests for mojo/apps/dnsman/services/registrar.py — the money path.

Everything runs in-process against the service with the route53 helper
functions patched, so no AWS call is ever made. `opts.client` is deliberately
NOT used here: it reaches a separate server process where these patches would
have no effect, which would silently let a real registrar call through.
"""

import hashlib
from decimal import Decimal
from unittest import mock

from objict import objict

from testit import helpers as th


R53 = "mojo.helpers.aws.route53"

GROUP_NAME = "test_dnsman_registrar_group"
USER_NAME = "test_dnsman_registrar_user"
NAME_PREFIX = "dnsman-test-"

CONTACT = {
    "FirstName": "Ops",
    "LastName": "Team",
    "ContactType": "COMPANY",
    "AddressLine1": "1 Main St",
    "City": "Portland",
    "State": "OR",
    "CountryCode": "US",
    "ZipCode": "97201",
    "PhoneNumber": "+1.5035551212",
    "Email": "ops@example.com",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _settings(**overrides):
    """
    Patch settings.get for the duration of a block.

    The service reads every knob through settings.get at call time, so this is
    the whole configuration surface. th.server_settings is for the separate
    server process and does nothing for in-process service calls.
    """
    from mojo.helpers.settings import settings as settings_obj

    real_get = settings_obj.get

    def patched_get(name, *args, **kwargs):
        if name in overrides:
            return overrides[name]
        return real_get(name, *args, **kwargs)

    return mock.patch.object(settings_obj, "get", side_effect=patched_get)


def _enabled(**extra):
    """Settings for a deployment where purchasing is fully configured."""
    values = {
        "DNSMAN_PURCHASE_ENABLED": True,
        "DNSMAN_REGISTRANT_CONTACT": dict(CONTACT),
        "DNSMAN_MAX_DOMAIN_PRICE": 50.00,
        "DNSMAN_QUOTE_TTL_MINUTES": 15,
    }
    values.update(extra)
    return _settings(**values)


def _avail(name, available=True, status="AVAILABLE", price=13.0,
           tld="com", tld_supported=True, privacy_supported=True):
    """A stand-in for route53.check_availability's objict."""
    return objict(
        name=name,
        status=status,
        available=available,
        price=price,
        currency="USD",
        tld=tld,
        tld_supported=tld_supported,
        privacy_supported=privacy_supported)


def _register_result(name, operation_id="op-1", privacy=True, downgraded=False):
    """A stand-in for route53.register's objict."""
    return objict(
        name=name,
        operation_id=operation_id,
        privacy=privacy,
        privacy_downgraded=downgraded)


def _operation(operation_id="op-1", status="IN_PROGRESS", domain_name=None,
               op_type="REGISTER_DOMAIN", message=None):
    return objict(
        operation_id=operation_id,
        status=status,
        domain_name=domain_name,
        type=op_type,
        message=message,
        submitted_on=None)


def _domain_detail(name, privacy=True):
    from mojo.helpers import dates
    return objict(
        name=name,
        status_list=["ok"],
        nameservers=["ns-1.awsdns.com"],
        auto_renew=True,
        privacy=privacy,
        admin_privacy=privacy,
        registrant_privacy=privacy,
        tech_privacy=privacy,
        registrar="Amazon Registrar",
        registered_on=dates.utcnow(),
        expires=dates.add(days=365),
        abuse_contact_email="abuse@example.com",
        admin_contact=objict(Email="ops@example.com"),
        registrant_contact=objict(Email="ops@example.com"),
        tech_contact=objict(Email="ops@example.com"))


def _make_submitted(opts, name, operation_id=None, with_domain=True):
    """Create a purchase already past its commit point, as the poller sees it."""
    from mojo.apps.dnsman.models import Domain, DomainPurchase

    purchase = DomainPurchase.objects.create(
        group=opts.group, user=opts.user, domain_name=name,
        kind="register", status="submitted",
        price=Decimal("13.00"), cost=Decimal("13.00"), currency="USD",
        years=1, operation_id=operation_id, metadata={})
    domain = None
    if with_domain:
        domain = Domain.objects.create(
            group=opts.group, user=opts.user, name=name,
            provider="route53", status="registering", verified=True,
            metadata={})
    return purchase, domain


def _clean():
    from mojo.apps.dnsman.models import Domain, DomainPurchase

    Domain.objects.filter(name__startswith=NAME_PREFIX).delete()
    DomainPurchase.objects.filter(domain_name__startswith=NAME_PREFIX).delete()


@th.django_unit_setup()
def setup_registrar_testing(opts):
    """Fresh group + user. Tests run on a long-lived DB, so clean up first."""
    from mojo.apps.account.models import Group, User

    _clean()
    Group.objects.filter(name=GROUP_NAME).delete()
    User.objects.filter(username=USER_NAME).delete()

    opts.group = Group.objects.create(name=GROUP_NAME, kind="organization")
    opts.user = User.objects.create_user(
        username=USER_NAME,
        email="test_dnsman_registrar@example.com",
        password="Abcd1234!")


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_search_normalizes_and_creates_nothing(opts):
    from mojo.apps.dnsman.models import Domain, DomainPurchase
    from mojo.apps.dnsman.services import registrar

    name = f"{NAME_PREFIX}search.com"
    _clean()

    with mock.patch(f"{R53}.check_availability",
                    return_value=_avail(name)) as check:
        result = registrar.search("  DNSMAN-Test-Search.COM.  ")

    assert check.call_args.args[0] == name, (
        f"Expected the normalized name to reach the registrar, got {check.call_args.args[0]}")
    assert result.available is True, (
        f"Expected an available name to report True, got {result.available!r}")
    assert result.reason is None, (
        f"Expected no reason on an available name, got {result.reason!r}")
    assert result.price == 13.0, f"Expected the quoted price to pass through, got {result.price}"
    assert DomainPurchase.objects.filter(domain_name=name).count() == 0, (
        "search must never create a purchase row")
    assert Domain.objects.filter(name=name).count() == 0, (
        "search must never create a domain row")


@th.django_unit_test()
def test_search_indeterminate_is_null_with_retry_reason(opts):
    """PENDING/DONT_KNOW means the registry did not answer — never 'taken'."""
    from mojo.apps.dnsman.services import registrar

    name = f"{NAME_PREFIX}maybe.com"

    with mock.patch(f"{R53}.check_availability",
                    return_value=_avail(name, available=None, status="PENDING")):
        result = registrar.search(name)

    assert result.available is None, (
        f"Expected an indeterminate answer to stay None, got {result.available!r}")
    assert result.reason and "again" in result.reason.lower(), (
        f"Expected a retry reason on an indeterminate answer, got {result.reason!r}")


# ---------------------------------------------------------------------------
# batch search
# ---------------------------------------------------------------------------

def _check_by_name(name):
    """A stand-in check_availability that answers for whatever name it is asked."""
    return _avail(name, tld=name.rsplit(".", 1)[1])


@th.django_unit_test()
def test_search_batch_tlds_shape_builds_the_grid(opts):
    """domain+tlds expands to base.tld candidates: cleaned, deduped, in order."""
    from mojo.apps.dnsman.services import registrar

    with mock.patch(f"{R53}.check_availability", side_effect=_check_by_name) as check:
        result = registrar.search_batch(
            domain="dnsman-test-grid", tlds=["com", ".DEV", "com", "io"])

    names = [row.name for row in result.results]
    assert names == ["dnsman-test-grid.com", "dnsman-test-grid.dev", "dnsman-test-grid.io"], (
        f"Expected cleaned, deduped candidates in request order, got {names}")
    assert check.call_count == 3, (
        f"Expected one AWS check per unique candidate, got {check.call_count}")

    with mock.patch(f"{R53}.check_availability",
                    return_value=_avail("dnsman-test-grid.com")):
        single = registrar.search("dnsman-test-grid.com")
    assert set(result.results[0].keys()) == set(single.keys()), (
        "Batch rows must have exactly the single-search row shape, got "
        f"{sorted(result.results[0].keys())} vs {sorted(single.keys())}")


@th.django_unit_test()
def test_search_batch_dotted_base_matches_bare_base(opts):
    """'nativemojo.com'+tlds and 'nativemojo'+tlds must produce the same grid."""
    from mojo.apps.dnsman.services import registrar

    with mock.patch(f"{R53}.check_availability", side_effect=_check_by_name):
        dotted = registrar.search_batch(domain="dnsman-test-base.com", tlds=["com", "dev"])
        bare = registrar.search_batch(domain="dnsman-test-base", tlds=["com", "dev"])

    dotted_names = [row.name for row in dotted.results]
    bare_names = [row.name for row in bare.results]
    assert dotted_names == bare_names == ["dnsman-test-base.com", "dnsman-test-base.dev"], (
        f"Expected identical grids for dotted and bare bases, got {dotted_names} vs {bare_names}")


@th.django_unit_test()
def test_search_batch_domains_shape_normalizes_and_dedupes(opts):
    from mojo.apps.dnsman.services import registrar

    with mock.patch(f"{R53}.check_availability", side_effect=_check_by_name):
        result = registrar.search_batch(domains=[
            "DNSMAN-Test-One.COM.", "dnsman-test-two.dev", "dnsman-test-one.com"])

    names = [row.name for row in result.results]
    assert names == ["dnsman-test-one.com", "dnsman-test-two.dev"], (
        f"Expected normalized, deduped names in request order, got {names}")


@th.django_unit_test()
def test_search_batch_enforces_the_limit_before_aws(opts):
    from mojo import errors as me
    from mojo.apps.dnsman.services import registrar

    names = [f"dnsman-test-{i}.com" for i in range(11)]
    raised = None
    with mock.patch(f"{R53}.check_availability") as check:
        try:
            registrar.search_batch(domains=names)
        except me.ValueException as err:
            raised = err
    assert raised is not None, "Expected an over-limit batch to be refused"
    assert "10" in str(raised) and "11" in str(raised), (
        f"Expected the refusal to name the limit and the given count, got {raised}")
    assert check.call_count == 0, (
        "An over-limit batch must be refused before any AWS call")

    # Dupes collapse before the cap: 11 raw entries, 2 unique -> allowed.
    dupes = ["dnsman-test-a.com"] * 10 + ["dnsman-test-b.com"]
    with mock.patch(f"{R53}.check_availability", side_effect=_check_by_name):
        result = registrar.search_batch(domains=dupes)
    assert len(result.results) == 2, (
        f"Expected dedupe to run before the cap, got {len(result.results)} rows")


@th.django_unit_test()
def test_search_batch_rejects_ambiguous_and_empty_shapes(opts):
    from mojo import errors as me
    from mojo.apps.dnsman.services import registrar

    bad_shapes = [
        dict(domains=["a.com"], tlds=["com"]),          # both list shapes
        dict(domain="a.com", domains=["b.com"]),        # single + list
        dict(tlds=["com"]),                             # tlds without a base
        dict(domains=[]),                               # nothing to check
        dict(domain="a", tlds=[]),                      # no usable tlds
        dict(domains="a.com"),                          # not a list
        dict(),                                         # nothing at all
    ]
    with mock.patch(f"{R53}.check_availability") as check:
        for shape in bad_shapes:
            raised = None
            try:
                registrar.search_batch(**shape)
            except me.ValueException as err:
                raised = err
            assert raised is not None, f"Expected shape {shape!r} to be refused"
    assert check.call_count == 0, (
        "A refused shape must never reach AWS")


@th.django_unit_test()
def test_search_batch_isolates_per_name_failures(opts):
    """One bad name gets a reason row; its siblings still get real answers."""
    from mojo import errors as me
    from mojo.apps.dnsman.services import registrar

    def flaky_check(name):
        if name == "dnsman-test-bad.com":
            raise me.ValueException(
                f"'{name}' is not a valid domain name for the .com registry")
        if name == "dnsman-test-boom.com":
            raise RuntimeError("socket exploded")
        return _avail(name)

    with mock.patch(f"{R53}.check_availability", side_effect=flaky_check):
        result = registrar.search_batch(domains=[
            "dnsman-test-good.com", "dnsman-test-bad.com", "dnsman-test-boom.com"])

    rows = result.results
    assert len(rows) == 3, f"Expected every name to get a row, got {len(rows)}"
    assert rows[0].available is True and rows[0].reason is None, (
        f"Expected the good name to answer normally, got {rows[0]}")
    assert rows[1].available is None, (
        "A validation failure must stay tri-state None — never False "
        f"(got {rows[1].available!r})")
    assert "not a valid domain name" in rows[1].reason, (
        f"Expected the validation message in the row's reason, got {rows[1].reason!r}")
    assert rows[2].available is None, (
        f"An unexpected error must stay tri-state None, got {rows[2].available!r}")
    assert "again" in rows[2].reason.lower(), (
        f"Expected a retry reason on an unexpected error, got {rows[2].reason!r}")
    assert "socket exploded" not in str(rows[2].reason), (
        "Raw internal error text must not leak into a row reason")


@th.django_unit_test()
def test_search_batch_preserves_request_order_across_the_pool(opts):
    """More names than pool workers: results still come back in request order."""
    from mojo.apps.dnsman.services import registrar

    names = [f"dnsman-test-order-{i}.com" for i in range(7)]
    with mock.patch(f"{R53}.check_availability", side_effect=_check_by_name):
        result = registrar.search_batch(domains=names)

    assert [row.name for row in result.results] == names, (
        f"Expected request order to survive the worker pool, got "
        f"{[row.name for row in result.results]}")


# ---------------------------------------------------------------------------
# suggestions
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_suggest_fills_prices_and_keeps_unsupported_rows(opts):
    from objict import objict as _objict
    from mojo.apps.dnsman.services import registrar

    suggestions = [
        _objict(name="dnsman-test-alpha.com", status="AVAILABLE", available=True),
        _objict(name="dnsman-test-beta.pizza", status="AVAILABLE", available=True),
        _objict(name="dnsman-test-gamma.dev", status="PENDING", available=None),
        _objict(name="dnsman-test-delta.com", status="UNAVAILABLE", available=False),
    ]

    def fake_prices(tld, **kwargs):
        if tld == "pizza":
            return _objict(tld=tld, supported=False, registration_price=None,
                           renewal_price=None, currency=None)
        return _objict(tld=tld, supported=True, registration_price=13.0,
                       renewal_price=13.0, currency="USD")

    with mock.patch(f"{R53}.get_domain_suggestions", return_value=suggestions) as sug, \
         mock.patch(f"{R53}.list_prices", side_effect=fake_prices):
        result = registrar.suggest("  DNSMAN-Test.COM.  ", count=4, only_available=False)

    assert sug.call_args.args[0] == "dnsman-test.com", (
        f"Expected the normalized name to reach the helper, got {sug.call_args.args[0]}")
    assert sug.call_args.kwargs["only_available"] is False, (
        "Expected only_available to pass through")

    rows = result.results
    assert [r.name for r in rows] == [s.name for s in suggestions], (
        f"Expected every suggestion to keep its row in order, got {[r.name for r in rows]}")

    assert rows[0].price == 13.0 and rows[0].currency == "USD", (
        f"Expected the price fill from list_prices, got {rows[0].price} {rows[0].currency}")
    assert rows[0].tld_supported is True and rows[0].reason is None, (
        f"Expected a clean available row, got {rows[0]}")

    assert rows[1].tld_supported is False, (
        "An unsupported TLD must keep its row with tld_supported=False, not be dropped")
    assert rows[1].price is None, (
        f"Expected no price for an unsupported TLD, got {rows[1].price}")
    assert rows[1].reason and "cannot be registered" in rows[1].reason, (
        f"Expected the unsupported-TLD reason, got {rows[1].reason!r}")

    assert rows[2].available is None and "again" in rows[2].reason.lower(), (
        f"Expected the indeterminate row to keep None + retry reason, got {rows[2]}")
    assert rows[3].available is False and "already registered" in rows[3].reason, (
        f"Expected the unavailable row to carry the taken reason, got {rows[3]}")

    with mock.patch(f"{R53}.check_availability",
                    return_value=_avail("dnsman-test.com")):
        single = registrar.search("dnsman-test.com")
    assert set(rows[0].keys()) == set(single.keys()), (
        "Suggest rows must have exactly the single-search row shape, got "
        f"{sorted(rows[0].keys())} vs {sorted(single.keys())}")


@th.django_unit_test()
def test_quote_prices_fresh_never_from_the_cache(opts):
    """The quoted price is capped and written to the ledger — quote must ask
    for a live price (use_cache=False), never a cached one."""
    from mojo.apps.dnsman.services import registrar

    _clean()
    name = f"{NAME_PREFIX}fresh-price.com"

    with _enabled():
        with mock.patch(f"{R53}.check_availability",
                        return_value=_avail(name)) as check:
            registrar.quote(opts.group, opts.user, name)

    assert check.call_args.kwargs.get("use_cache") is False, (
        "quote() must pass use_cache=False so no money decision rides a "
        f"stale cached price, got kwargs {check.call_args.kwargs!r}")


@th.django_unit_test()
def test_suggest_failure_is_clean_and_leaks_no_aws_detail(opts):
    """A registrar failure (missing IAM grant, throttle) must surface as a
    clean retry message — botocore text carries the AWS account id."""
    from mojo import errors as me
    from mojo.apps.dnsman.services import registrar

    boom = Exception(
        "An error occurred (AccessDeniedException): User: "
        "arn:aws:iam::123456789012:user/svc is not authorized to perform: "
        "route53domains:GetDomainSuggestions")

    raised = None
    with mock.patch(f"{R53}.get_domain_suggestions", side_effect=boom):
        try:
            registrar.suggest("dnsman-test.com")
        except me.ValueException as err:
            raised = err

    assert raised is not None, "Expected a helper failure to become a clean ValueException"
    message = str(raised)
    assert "arn:" not in message and "123456789012" not in message, (
        f"AWS account detail leaked into the client-facing error: {message!r}")
    assert "again" in message.lower(), (
        f"Expected a retry-shaped message, got {message!r}")


@th.django_unit_test()
def test_suggest_clamps_count_and_validates_input(opts):
    from mojo import errors as me
    from mojo.apps.dnsman.services import registrar

    with mock.patch(f"{R53}.get_domain_suggestions", return_value=[]) as sug:
        registrar.suggest("dnsman-test.com", count=500)
    assert sug.call_args.kwargs["count"] == registrar.MAX_SUGGESTION_COUNT, (
        f"Expected the count clamped to {registrar.MAX_SUGGESTION_COUNT}, "
        f"got {sug.call_args.kwargs['count']}")

    with mock.patch(f"{R53}.get_domain_suggestions", return_value=[]) as sug:
        registrar.suggest("dnsman-test.com", count=0)
    assert sug.call_args.kwargs["count"] == 1, (
        f"Expected the count clamped up to 1, got {sug.call_args.kwargs['count']}")

    raised = None
    with mock.patch(f"{R53}.get_domain_suggestions", return_value=[]) as sug:
        try:
            registrar.suggest("dnsman-test.com", count="lots")
        except me.ValueException as err:
            raised = err
    assert raised is not None and "whole number" in str(raised), (
        f"Expected a clean validation error for a non-numeric count, got {raised}")
    assert sug.call_count == 0, "A bad count must be refused before any AWS call"

    raised = None
    try:
        registrar.suggest("not_a_domain")
    except me.ValueException as err:
        raised = err
    assert raised is not None, (
        "Expected a TLD-less input to fail normalization before any AWS call")


# ---------------------------------------------------------------------------
# quote
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_quote_happy_path(opts):
    from mojo.apps.dnsman.models import DomainPurchase
    from mojo.apps.dnsman.services import registrar
    from mojo.helpers import dates

    name = f"{NAME_PREFIX}quote.com"
    _clean()

    with _enabled():
        with mock.patch(f"{R53}.check_availability", return_value=_avail(name)):
            result = registrar.quote(opts.group, opts.user, name.upper(), years=2)

    row = DomainPurchase.objects.filter(pk=result.purchase).first()
    assert row is not None, "Expected quote to create a DomainPurchase row"
    assert row.status == "quoted", f"Expected status 'quoted', got {row.status}"
    assert row.domain_name == name, (
        f"Expected the normalized name on the row, got {row.domain_name}")
    assert row.years == 2, f"Expected years to be carried through, got {row.years}"
    assert row.price == Decimal("13.00"), f"Expected price 13.00, got {row.price}"
    assert row.cost == Decimal("13.00"), f"Expected cost to mirror price, got {row.cost}"
    assert row.group_id == opts.group.id, "Expected the quote to be attributed to the group"
    assert row.user_id == opts.user.id, "Expected the quote to be attributed to the user"

    assert len(result.token) == 48, (
        f"Expected a 48 character confirm token, got {len(result.token)}")
    assert row.confirm_token != result.token, (
        "The raw confirm token must never be stored")
    assert row.confirm_token == hashlib.sha256(result.token.encode()).hexdigest(), (
        "Expected the stored token to be the SHA-256 hash of the returned token")
    assert row.quote_expires > dates.utcnow(), (
        f"Expected the quote to expire in the future, got {row.quote_expires}")


@th.django_unit_test()
def test_quote_kill_switch_never_touches_aws(opts):
    """DNSMAN_PURCHASE_ENABLED off must refuse before any registrar call."""
    from mojo.apps.dnsman.models import DomainPurchase
    from mojo.apps.dnsman.services import registrar
    from mojo import errors as me

    name = f"{NAME_PREFIX}killswitch.com"
    _clean()
    raised = None

    with _enabled(DNSMAN_PURCHASE_ENABLED=False):
        with mock.patch(f"{R53}.check_availability") as check:
            try:
                registrar.quote(opts.group, opts.user, name)
            except me.ValueException as err:
                raised = err

    assert raised is not None, "Expected a refusal when purchasing is disabled"
    assert "disabled" in str(raised).lower(), (
        f"Expected the refusal to name the kill switch, got {raised}")
    assert check.call_count == 0, (
        f"Expected zero AWS calls when purchasing is disabled, got {check.call_count}")
    assert DomainPurchase.objects.filter(domain_name=name).count() == 0, (
        "A disabled deployment must not create a purchase row")


@th.django_unit_test()
def test_quote_incomplete_registrant_contact_refused(opts):
    from mojo.apps.dnsman.models import DomainPurchase
    from mojo.apps.dnsman.services import registrar
    from mojo import errors as me

    name = f"{NAME_PREFIX}nocontact.com"
    _clean()
    partial = dict(CONTACT)
    partial.pop("Email")
    raised = None

    with _enabled(DNSMAN_REGISTRANT_CONTACT=partial):
        with mock.patch(f"{R53}.check_availability") as check:
            try:
                registrar.quote(opts.group, opts.user, name)
            except me.ValueException as err:
                raised = err

    assert raised is not None, "Expected a refusal when the registrant contact is incomplete"
    assert "Email" in str(raised), (
        f"Expected the refusal to name the missing field, got {raised}")
    assert check.call_count == 0, (
        "An incomplete registrant contact must be refused before any AWS call")
    assert DomainPurchase.objects.filter(domain_name=name).count() == 0, (
        "An incomplete registrant contact must not create a purchase row")


@th.django_unit_test()
def test_quote_refuses_above_the_price_cap(opts):
    from mojo.apps.dnsman.models import DomainPurchase
    from mojo.apps.dnsman.services import registrar
    from mojo import errors as me

    name = f"{NAME_PREFIX}premium.com"
    _clean()
    raised = None

    with _enabled(DNSMAN_MAX_DOMAIN_PRICE=50.00):
        with mock.patch(f"{R53}.check_availability",
                        return_value=_avail(name, price=2500.0)):
            try:
                registrar.quote(opts.group, opts.user, name)
            except me.ValueException as err:
                raised = err

    assert raised is not None, "Expected a domain above the price cap to be refused"
    assert "50" in str(raised), (
        f"Expected the refusal to state the limit, got {raised}")
    assert DomainPurchase.objects.filter(domain_name=name).count() == 0, (
        "A capped-out quote must not create a purchase row")


@th.django_unit_test()
def test_quote_refuses_a_name_already_managed(opts):
    """Every Domain row is live, so an existing row is a complete answer."""
    from mojo.apps.dnsman.models import Domain, DomainPurchase
    from mojo.apps.dnsman.services import registrar
    from mojo import errors as me

    name = f"{NAME_PREFIX}duplicate.com"
    _clean()
    Domain.objects.create(
        group=opts.group, user=opts.user, name=name,
        provider="route53", status="active", verified=True, metadata={})
    raised = None

    with _enabled():
        with mock.patch(f"{R53}.check_availability") as check:
            try:
                registrar.quote(opts.group, opts.user, name)
            except me.ValueException as err:
                raised = err

    assert raised is not None, "Expected a duplicate name to be refused"
    assert "already managed" in str(raised), (
        f"Expected an actionable duplicate message, got {raised}")
    assert check.call_count == 0, (
        "A name we already hold must be refused before spending an AWS call")
    assert DomainPurchase.objects.filter(domain_name=name).count() == 0, (
        "A duplicate quote must not create a purchase row")


@th.django_unit_test()
def test_quote_indeterminate_availability_creates_no_row(opts):
    """An unanswered registry must never become a purchasable quote."""
    from mojo.apps.dnsman.models import DomainPurchase
    from mojo.apps.dnsman.services import registrar
    from mojo import errors as me

    name = f"{NAME_PREFIX}indeterminate.com"
    _clean()
    raised = None

    with _enabled():
        with mock.patch(f"{R53}.check_availability",
                        return_value=_avail(name, available=None, status="DONT_KNOW")):
            try:
                registrar.quote(opts.group, opts.user, name)
            except me.ValueException as err:
                raised = err

    assert raised is not None, "Expected an indeterminate availability answer to be refused"
    assert DomainPurchase.objects.filter(domain_name=name).count() == 0, (
        "An indeterminate availability answer must create NO purchase row")


@th.django_unit_test()
def test_quote_refuses_an_unsupported_tld(opts):
    from mojo.apps.dnsman.models import DomainPurchase
    from mojo.apps.dnsman.services import registrar
    from mojo import errors as me

    name = f"{NAME_PREFIX}unsupported.pizza"
    _clean()
    raised = None

    with _enabled():
        with mock.patch(f"{R53}.check_availability",
                        return_value=_avail(name, tld="pizza", tld_supported=False,
                                            price=None)):
            try:
                registrar.quote(opts.group, opts.user, name)
            except me.ValueException as err:
                raised = err

    assert raised is not None, "Expected an unsupported TLD to be refused"
    assert "pizza" in str(raised), f"Expected the refusal to name the TLD, got {raised}"
    assert DomainPurchase.objects.filter(domain_name=name).count() == 0, (
        "An unsupported TLD must not create a purchase row")


# ---------------------------------------------------------------------------
# purchase
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_purchase_happy_path_creates_registering_domain(opts):
    from mojo.apps.dnsman.models import Domain, DomainPurchase
    from mojo.apps.dnsman.services import registrar

    name = f"{NAME_PREFIX}buy.com"
    _clean()

    with _enabled():
        with mock.patch(f"{R53}.check_availability", return_value=_avail(name)):
            quoted = registrar.quote(opts.group, opts.user, name)
        with mock.patch(f"{R53}.register",
                        return_value=_register_result(name, "op-buy")) as register:
            result = registrar.purchase(
                opts.group, opts.user, quoted.purchase, quoted.token,
                quoted.name, quoted.price)

    assert register.call_count == 1, (
        f"Expected exactly one registration call, got {register.call_count}")
    sent = register.call_args
    assert sent.args[0] == name, f"Expected the normalized name to be registered, got {sent.args[0]}"
    assert sent.args[1]["Email"] == CONTACT["Email"], (
        "Expected the configured registrant contact to be used")

    row = DomainPurchase.objects.get(pk=quoted.purchase)
    assert row.status == "submitted", (
        f"Expected the purchase to be 'submitted' after the AWS call, got {row.status}")
    assert row.operation_id == "op-buy", (
        f"Expected the operation id to be stored, got {row.operation_id}")
    assert row.confirm_token is None, (
        "Expected the confirm token to be consumed by a successful purchase")

    domain = Domain.objects.get(name=name)
    assert domain.status == "registering", (
        f"Expected the domain to be 'registering' before the poller runs, got {domain.status}")
    assert domain.group_id == opts.group.id, "Expected the domain to belong to the quoting group"
    assert result.domain == domain.id, (
        f"Expected the result to carry the domain id, got {result.domain}")


@th.django_unit_test()
def test_purchase_second_call_on_same_quote_is_uniformly_refused(opts):
    """
    The compare-and-swap: only one confirmation of a quote may win.

    This calls purchase twice SEQUENTIALLY against the same quote — it does not
    simulate real parallelism. What it proves is the state machine the CAS rests
    on: once the row leaves 'quoted', every later confirmation is refused with a
    message that reveals nothing about which check failed.
    """
    from mojo.apps.dnsman.models import Domain, DomainPurchase
    from mojo.apps.dnsman.services import registrar
    from mojo import errors as me

    name = f"{NAME_PREFIX}cas.com"
    _clean()
    raised = None

    with _enabled():
        with mock.patch(f"{R53}.check_availability", return_value=_avail(name)):
            quoted = registrar.quote(opts.group, opts.user, name)
        with mock.patch(f"{R53}.register",
                        return_value=_register_result(name, "op-cas")) as register:
            registrar.purchase(opts.group, opts.user, quoted.purchase, quoted.token,
                               quoted.name, quoted.price)
            try:
                registrar.purchase(opts.group, opts.user, quoted.purchase, quoted.token,
                                   quoted.name, quoted.price)
            except me.ValueException as err:
                raised = err

    assert raised is not None, "Expected the second confirmation of a quote to be refused"
    assert raised.status == 400, f"Expected a 400 refusal, got {raised.status}"
    assert str(raised) == registrar.PURCHASE_REFUSED, (
        f"Expected the uniform refusal message, got {raised}")
    assert register.call_count == 1, (
        f"Expected the domain to be registered exactly once, got {register.call_count}")
    assert Domain.objects.filter(name=name).count() == 1, (
        "Expected exactly one domain row after a duplicate confirmation")
    assert DomainPurchase.objects.get(pk=quoted.purchase).status == "submitted", (
        "Expected the winning purchase to stay 'submitted'")


@th.django_unit_test()
def test_purchase_with_a_wrong_token_is_uniformly_refused(opts):
    from mojo.apps.dnsman.models import Domain, DomainPurchase
    from mojo.apps.dnsman.services import registrar
    from mojo import errors as me

    name = f"{NAME_PREFIX}badtoken.com"
    _clean()
    raised = None

    with _enabled():
        with mock.patch(f"{R53}.check_availability", return_value=_avail(name)):
            quoted = registrar.quote(opts.group, opts.user, name)
        with mock.patch(f"{R53}.register") as register:
            try:
                registrar.purchase(opts.group, opts.user, quoted.purchase, "not-the-token",
                                   quoted.name, quoted.price)
            except me.ValueException as err:
                raised = err

    assert raised is not None, "Expected a bad confirm token to be refused"
    assert str(raised) == registrar.PURCHASE_REFUSED, (
        f"Expected the uniform refusal message, got {raised}")
    assert register.call_count == 0, (
        "A bad confirm token must never reach the registrar")
    assert Domain.objects.filter(name=name).count() == 0, (
        "A refused confirmation must not create a domain row")
    assert DomainPurchase.objects.get(pk=quoted.purchase).status == "quoted", (
        "A bad token must leave the quote usable")


@th.django_unit_test("typed domain and price must exactly match the locked quote")
def test_purchase_requires_typed_domain_and_price(opts):
    from mojo.apps.dnsman.models import Domain, DomainPurchase
    from mojo.apps.dnsman.services import registrar
    from mojo import errors as me

    name = f"{NAME_PREFIX}typed.com"
    _clean()
    with _enabled():
        with mock.patch(f"{R53}.check_availability", return_value=_avail(name)):
            quoted = registrar.quote(opts.group, opts.user, name)
        for typed_domain, typed_price in (("wrong.example", quoted.price),
                                          (quoted.name, "0.01")):
            raised = None
            with mock.patch(f"{R53}.register") as register:
                try:
                    registrar.purchase(
                        opts.group, opts.user, quoted.purchase, quoted.token,
                        typed_domain, typed_price)
                except me.ValueException as error:
                    raised = error
            assert raised is not None, \
                f"mismatched typed confirmation {typed_domain}/{typed_price} was accepted"
            assert not register.called, \
                "typed confirmation mismatch reached the irreversible registrar call"
    assert DomainPurchase.objects.get(pk=quoted.purchase).status == "quoted", \
        "a refused typed confirmation consumed the still-valid quote"
    assert not Domain.objects.filter(name=name).exists(), \
        "a refused typed confirmation created durable registration intent"


@th.django_unit_test()
def test_purchase_refuses_and_expires_a_stale_quote(opts):
    from mojo.apps.dnsman.models import Domain, DomainPurchase
    from mojo.apps.dnsman.services import registrar
    from mojo.helpers import dates
    from mojo import errors as me

    name = f"{NAME_PREFIX}stale.com"
    _clean()
    raised = None

    with _enabled():
        with mock.patch(f"{R53}.check_availability", return_value=_avail(name)):
            quoted = registrar.quote(opts.group, opts.user, name)

        DomainPurchase.objects.filter(pk=quoted.purchase).update(
            quote_expires=dates.subtract(minutes=1))

        with mock.patch(f"{R53}.register") as register:
            try:
                registrar.purchase(opts.group, opts.user, quoted.purchase, quoted.token,
                                   quoted.name, quoted.price)
            except me.ValueException as err:
                raised = err

    assert raised is not None, "Expected an expired quote to be refused"
    assert str(raised) == registrar.PURCHASE_REFUSED, (
        f"Expected the uniform refusal message, got {raised}")
    assert register.call_count == 0, "An expired quote must never reach the registrar"

    row = DomainPurchase.objects.get(pk=quoted.purchase)
    assert row.status == "expired", (
        f"Expected the stale quote to be marked expired, got {row.status}")
    assert row.confirm_token is None, "Expected an expired quote to drop its token"
    assert Domain.objects.filter(name=name).count() == 0, (
        "An expired quote must not create a domain row")


@th.django_unit_test()
def test_purchase_kill_switch_blocks_a_valid_quote(opts):
    """Throwing the switch between quote and confirm must stop the money."""
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import registrar
    from mojo import errors as me

    name = f"{NAME_PREFIX}switched.com"
    _clean()
    raised = None

    with _enabled():
        with mock.patch(f"{R53}.check_availability", return_value=_avail(name)):
            quoted = registrar.quote(opts.group, opts.user, name)

    with _enabled(DNSMAN_PURCHASE_ENABLED=False):
        with mock.patch(f"{R53}.register") as register:
            try:
                registrar.purchase(opts.group, opts.user, quoted.purchase, quoted.token,
                                   quoted.name, quoted.price)
            except me.ValueException as err:
                raised = err

    assert raised is not None, "Expected a disabled deployment to refuse a confirmation"
    assert register.call_count == 0, (
        "A disabled deployment must never reach the registrar")
    assert Domain.objects.filter(name=name).count() == 0, (
        "A refused confirmation must not create a domain row")


@th.django_unit_test()
def test_purchase_register_failure_leaves_no_orphan_domain(opts):
    """No-failed-rows invariant: the ledger keeps the error, the Domain goes."""
    from mojo.apps.dnsman.models import Domain, DomainPurchase
    from mojo.apps.dnsman.services import registrar
    from mojo import errors as me

    name = f"{NAME_PREFIX}boom.com"
    _clean()
    raised = None

    with _enabled():
        with mock.patch(f"{R53}.check_availability", return_value=_avail(name)):
            quoted = registrar.quote(opts.group, opts.user, name)
        with mock.patch(f"{R53}.register",
                        side_effect=Exception("InvalidInput: registrant rejected")):
            try:
                registrar.purchase(opts.group, opts.user, quoted.purchase, quoted.token,
                                   quoted.name, quoted.price)
            except me.ValueException as err:
                raised = err

    assert raised is not None, "Expected a registrar failure to surface to the caller"

    row = DomainPurchase.objects.get(pk=quoted.purchase)
    assert row.status == "failed", (
        f"Expected the purchase to be marked failed, got {row.status}")
    assert "registrant rejected" in (row.error or ""), (
        f"Expected the registrar error to be recorded, got {row.error!r}")
    assert Domain.objects.filter(name=name).count() == 0, (
        "A failed registration must leave no orphan Domain row")


@th.django_unit_test()
def test_purchase_records_a_privacy_downgrade_honestly(opts):
    """The row must never claim privacy the registry refused to give it."""
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import registrar

    name = f"{NAME_PREFIX}downgrade.com"
    _clean()

    with _enabled():
        with mock.patch(f"{R53}.check_availability", return_value=_avail(name)):
            quoted = registrar.quote(opts.group, opts.user, name)
        with mock.patch(f"{R53}.register",
                        return_value=_register_result(name, "op-dg", privacy=False,
                                                      downgraded=True)):
            result = registrar.purchase(
                opts.group, opts.user, quoted.purchase, quoted.token,
                quoted.name, quoted.price)

    domain = Domain.objects.get(name=name)
    assert domain.privacy is False, (
        "Expected a downgraded registration to record privacy=False on the domain")
    assert result.privacy_downgraded is True, (
        "Expected the caller to be told privacy was downgraded")
    assert domain.metadata.get("privacy_downgraded") is True, (
        f"Expected the downgrade to be noted in metadata, got {domain.metadata}")


# ---------------------------------------------------------------------------
# poll_pending
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_poll_pending_completes_a_successful_registration(opts):
    from mojo.apps.dnsman.models import Domain, DomainPurchase
    from mojo.apps.dnsman.services import registrar

    name = f"{NAME_PREFIX}poll-ok.com"
    _clean()
    purchase, domain = _make_submitted(opts, name, operation_id="op-ok")

    with mock.patch(f"{R53}.operation_detail",
                    return_value=_operation("op-ok", "SUCCESSFUL", name)):
        with mock.patch(f"{R53}.find_zone_id", return_value="Z123ABC"):
            with mock.patch(f"{R53}.get_domain_detail",
                            return_value=_domain_detail(name)):
                result = registrar.poll_pending()

    assert result.completed == 1, f"Expected one completed purchase, got {result.completed}"

    row = DomainPurchase.objects.get(pk=purchase.pk)
    assert row.status == "completed", f"Expected status 'completed', got {row.status}"

    domain = Domain.objects.get(pk=domain.pk)
    assert domain.status == "active", f"Expected the domain to go active, got {domain.status}"
    assert domain.hosted_zone_id == "Z123ABC", (
        f"Expected the hosted zone id to be filled in, got {domain.hosted_zone_id}")
    assert domain.expires is not None, "Expected the expiry date to be filled in"
    assert domain.registered_on is not None, "Expected the registration date to be filled in"


@th.django_unit_test()
def test_poll_pending_is_idempotent_under_double_observation(opts):
    """A second observation of the same operation must not flip anything twice."""
    from mojo.apps.dnsman.models import Domain, DomainPurchase
    from mojo.apps.dnsman.services import registrar

    name = f"{NAME_PREFIX}poll-twice.com"
    _clean()
    purchase, domain = _make_submitted(opts, name, operation_id="op-twice")

    with mock.patch(f"{R53}.operation_detail",
                    return_value=_operation("op-twice", "SUCCESSFUL", name)) as detail:
        with mock.patch(f"{R53}.find_zone_id", return_value="Z999") as zone:
            with mock.patch(f"{R53}.get_domain_detail",
                            return_value=_domain_detail(name)):
                first = registrar.poll_pending()
                second = registrar.poll_pending()

                # The second poll no longer selects the row at all. To exercise
                # the guard two concurrent pollers would actually hit, settle the
                # already-completed row directly — sequential, not parallel, but
                # it is the same compare-and-swap.
                row = DomainPurchase.objects.get(pk=purchase.pk)
                third = objict(completed=0)
                registrar._complete(row, third)

    assert first.completed == 1, f"Expected the first poll to complete one row, got {first.completed}"
    assert second.completed == 0, (
        f"Expected the second poll to find nothing to complete, got {second.completed}")
    assert third.completed == 0, (
        "Expected the guarded flip to refuse a second observation of the same row")
    assert detail.call_count == 1, (
        f"Expected AWS to be asked exactly once, got {detail.call_count}")
    assert zone.call_count == 1, (
        f"Expected the hosted zone to be resolved exactly once, got {zone.call_count}")
    assert DomainPurchase.objects.filter(pk=purchase.pk, status="completed").count() == 1, (
        "Expected the purchase to be completed exactly once")
    assert Domain.objects.get(pk=domain.pk).status == "active", (
        "Expected the domain to remain active after a repeat observation")


@th.django_unit_test()
def test_poll_pending_failure_deletes_the_domain_row(opts):
    from mojo.apps.dnsman.models import Domain, DomainPurchase
    from mojo.apps.dnsman.services import registrar

    name = f"{NAME_PREFIX}poll-bad.com"
    _clean()
    purchase, domain = _make_submitted(opts, name, operation_id="op-bad")

    with mock.patch(f"{R53}.operation_detail",
                    return_value=_operation("op-bad", "ERROR", name,
                                            message="registry rejected the request")):
        result = registrar.poll_pending()

    assert result.failed == 1, f"Expected one failed purchase, got {result.failed}"

    row = DomainPurchase.objects.get(pk=purchase.pk)
    assert row.status == "failed", f"Expected status 'failed', got {row.status}"
    assert "registry rejected" in (row.error or ""), (
        f"Expected the AWS message to be recorded, got {row.error!r}")
    assert Domain.objects.filter(pk=domain.pk).count() == 0, (
        "A failed registration must leave no Domain row behind")


@th.django_unit_test()
def test_poll_adopts_the_operation_for_a_crashed_purchase(opts):
    """
    The crash window: committed 'submitted', operation id never stored.

    list_operations is the only way to tell "the call never happened" from
    "the call happened and we died before recording it".
    """
    from mojo.apps.dnsman.models import DomainPurchase
    from mojo.apps.dnsman.services import registrar

    name = f"{NAME_PREFIX}crashwindow.com"
    _clean()
    purchase, domain = _make_submitted(opts, name, operation_id=None)

    operations = [
        _operation("op-other", "SUCCESSFUL", f"{NAME_PREFIX}elsewhere.com"),
        _operation("op-renewal", "SUCCESSFUL", name, op_type="RENEW_DOMAIN"),
        _operation("op-found", "IN_PROGRESS", name),
    ]

    with mock.patch(f"{R53}.list_operations", return_value=operations) as listed:
        with mock.patch(f"{R53}.operation_detail",
                        return_value=_operation("op-found", "IN_PROGRESS", name)) as detail:
            result = registrar.poll_pending()

    row = DomainPurchase.objects.get(pk=purchase.pk)

    assert listed.call_count == 1, (
        f"Expected exactly one reconciliation probe, got {listed.call_count}")
    assert listed.call_args.kwargs.get("submitted_since") == row.created, (
        "Expected the probe to be bounded by the purchase creation time")
    assert result.adopted == 1, f"Expected one adopted operation, got {result.adopted}"
    assert row.operation_id == "op-found", (
        f"Expected the matching REGISTER_DOMAIN operation to be adopted, got {row.operation_id}")
    assert row.status == "submitted", (
        f"Expected an in-progress adoption to stay submitted, got {row.status}")
    assert detail.call_count == 1, (
        "Expected polling to continue normally once the operation was adopted")


@th.django_unit_test()
def test_poll_fails_a_purchase_that_never_reached_aws(opts):
    from mojo.apps.dnsman.models import Domain, DomainPurchase
    from mojo.apps.dnsman.services import registrar
    from mojo.helpers import dates

    name = f"{NAME_PREFIX}neverlanded.com"
    _clean()
    purchase, domain = _make_submitted(opts, name, operation_id=None)
    # Backdate past the reconciliation window — `created` is auto_now_add.
    DomainPurchase.objects.filter(pk=purchase.pk).update(
        created=dates.subtract(minutes=registrar.RECONCILE_TIMEOUT_MINUTES + 15))

    with mock.patch(f"{R53}.list_operations", return_value=[]):
        with mock.patch(f"{R53}.operation_detail") as detail:
            result = registrar.poll_pending()

    assert result.failed == 1, f"Expected the stranded purchase to fail, got {result.failed}"
    assert detail.call_count == 0, (
        "Expected no operation lookup when there is no operation to look up")

    row = DomainPurchase.objects.get(pk=purchase.pk)
    assert row.status == "failed", f"Expected status 'failed', got {row.status}"
    assert "never reached AWS" in (row.error or ""), (
        f"Expected an ops-legible reason, got {row.error!r}")
    assert Domain.objects.filter(pk=domain.pk).count() == 0, (
        "A registration that never reached AWS must leave no Domain row")


@th.django_unit_test()
def test_poll_leaves_a_fresh_unreconciled_purchase_alone(opts):
    """Inside the window, an unmatched purchase waits — it is not failed early."""
    from mojo.apps.dnsman.models import Domain, DomainPurchase
    from mojo.apps.dnsman.services import registrar

    name = f"{NAME_PREFIX}stillwaiting.com"
    _clean()
    purchase, domain = _make_submitted(opts, name, operation_id=None)

    with mock.patch(f"{R53}.list_operations", return_value=[]):
        result = registrar.poll_pending()

    assert result.failed == 0, (
        f"Expected a fresh purchase not to be failed early, got {result.failed}")
    assert result.pending == 1, f"Expected the row to be counted pending, got {result.pending}"
    assert DomainPurchase.objects.get(pk=purchase.pk).status == "submitted", (
        "Expected a fresh unreconciled purchase to stay submitted")
    assert Domain.objects.filter(pk=domain.pk).count() == 1, (
        "Expected the domain row to survive while reconciliation is still in its window")


@th.django_unit_test()
def test_poll_expires_stale_quotes(opts):
    from mojo.apps.dnsman.models import DomainPurchase
    from mojo.apps.dnsman.services import registrar
    from mojo.helpers import dates

    fresh = f"{NAME_PREFIX}fresh-quote.com"
    stale = f"{NAME_PREFIX}stale-quote.com"
    _clean()

    with _enabled():
        with mock.patch(f"{R53}.check_availability", return_value=_avail(fresh)):
            fresh_quote = registrar.quote(opts.group, opts.user, fresh)
        with mock.patch(f"{R53}.check_availability", return_value=_avail(stale)):
            stale_quote = registrar.quote(opts.group, opts.user, stale)

    DomainPurchase.objects.filter(pk=stale_quote.purchase).update(
        quote_expires=dates.subtract(minutes=1))

    result = registrar.poll_pending()

    assert result.expired == 1, f"Expected exactly one expired quote, got {result.expired}"
    assert DomainPurchase.objects.get(pk=stale_quote.purchase).status == "expired", (
        "Expected the stale quote to be expired")
    assert DomainPurchase.objects.get(pk=fresh_quote.purchase).status == "quoted", (
        "Expected the in-window quote to be left alone")


# ---------------------------------------------------------------------------
# WHOIS contacts + privacy
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_get_contacts_returns_the_registrar_view(opts):
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import registrar

    name = f"{NAME_PREFIX}whois.com"
    _clean()
    domain = Domain.objects.create(
        group=opts.group, user=opts.user, name=name,
        provider="route53", status="active", verified=True, metadata={})

    with mock.patch(f"{R53}.get_domain_detail", return_value=_domain_detail(name)):
        result = registrar.get_contacts(domain)

    assert result.registrant.Email == "ops@example.com", (
        f"Expected the registrant contact to be returned, got {result.registrant}")
    assert result.privacy is True, f"Expected privacy to be reported, got {result.privacy}"
    assert result.privacy_supported is True, "Expected .com to support privacy"
    assert result.nameservers == ["ns-1.awsdns.com"], (
        f"Expected the nameservers to be returned, got {result.nameservers}")


@th.django_unit_test()
def test_godaddy_domain_is_management_only(opts):
    """Not a permission failure — an explicit, actionable capability message."""
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import registrar
    from mojo import errors as me

    name = f"{NAME_PREFIX}godaddy.com"
    _clean()
    domain = Domain.objects.create(
        group=opts.group, user=opts.user, name=name,
        provider="godaddy", status="active", verified=True, metadata={})

    contacts_error = None
    privacy_error = None
    update_error = None

    with mock.patch(f"{R53}.get_domain_detail") as detail:
        with mock.patch(f"{R53}.update_privacy") as privacy:
            with mock.patch(f"{R53}.update_contacts") as contacts:
                try:
                    registrar.get_contacts(domain)
                except me.ValueException as err:
                    contacts_error = err
                try:
                    registrar.set_privacy(domain, True)
                except me.ValueException as err:
                    privacy_error = err
                try:
                    registrar.update_contacts(domain, dict(CONTACT))
                except me.ValueException as err:
                    update_error = err

    for label, err in (("get_contacts", contacts_error),
                       ("set_privacy", privacy_error),
                       ("update_contacts", update_error)):
        assert err is not None, f"Expected {label} to refuse a GoDaddy domain"
        assert "management-only" in str(err), (
            f"Expected {label} to give the management-only message, got {err}")
        assert err.status == 400, (
            f"Expected {label} to refuse with 400, not a generic 403, got {err.status}")

    assert detail.call_count == 0, "A GoDaddy domain must never reach Route53"
    assert privacy.call_count == 0, "A GoDaddy domain must never reach Route53"
    assert contacts.call_count == 0, "A GoDaddy domain must never reach Route53"


@th.django_unit_test()
def test_set_privacy_refuses_a_tld_without_privacy(opts):
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import registrar
    from mojo import errors as me

    name = f"{NAME_PREFIX}nopriv.us"
    _clean()
    domain = Domain.objects.create(
        group=opts.group, user=opts.user, name=name,
        provider="route53", status="active", verified=True,
        privacy=False, metadata={})
    raised = None

    with mock.patch(f"{R53}.update_privacy") as update:
        try:
            registrar.set_privacy(domain, True)
        except me.ValueException as err:
            raised = err

    assert raised is not None, "Expected privacy to be refused for a TLD that forbids it"
    assert ".us" in str(raised), (
        f"Expected the refusal to name the TLD so the user can act on it, got {raised}")
    assert update.call_count == 0, (
        "A TLD that cannot have privacy must be refused before the AWS call")


@th.django_unit_test()
def test_set_privacy_updates_the_domain_row(opts):
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import registrar

    name = f"{NAME_PREFIX}privon.com"
    _clean()
    domain = Domain.objects.create(
        group=opts.group, user=opts.user, name=name,
        provider="route53", status="active", verified=True,
        privacy=False, metadata={})

    with mock.patch(f"{R53}.update_privacy", return_value="op-priv") as update:
        result = registrar.set_privacy(domain, True)

    assert update.call_args.args == (name, True), (
        f"Expected privacy to be enabled for the domain, got {update.call_args.args}")
    assert result.operation_id == "op-priv", (
        f"Expected the operation id to be returned, got {result.operation_id}")
    assert Domain.objects.get(pk=domain.pk).privacy is True, (
        "Expected the domain row to record the new privacy state")


@th.django_unit_test()
def test_update_contacts_requires_a_complete_contact(opts):
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import registrar
    from mojo import errors as me

    name = f"{NAME_PREFIX}badcontact.com"
    _clean()
    domain = Domain.objects.create(
        group=opts.group, user=opts.user, name=name,
        provider="route53", status="active", verified=True, metadata={})
    partial = dict(CONTACT)
    partial.pop("PhoneNumber")
    raised = None

    with mock.patch(f"{R53}.update_contacts") as update:
        try:
            registrar.update_contacts(domain, partial)
        except me.ValueException as err:
            raised = err

    assert raised is not None, "Expected an incomplete contact to be refused"
    assert "PhoneNumber" in str(raised), (
        f"Expected the refusal to name the missing field, got {raised}")
    assert update.call_count == 0, (
        "An incomplete contact must be refused before the AWS call")
