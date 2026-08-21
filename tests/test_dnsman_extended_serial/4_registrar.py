"""Split out of tests/test_dnsman/4_registrar.py (maestro #1839).

Every test here configures the purchasing deployment through `_enabled()`,
which patches the shared settings singleton (mojo.helpers.settings.settings.get)
for in-process registrar calls — process-global, so unsafe under the parallel
default tier. The route53 helper functions stay patched exactly as in the
source module; no AWS call is ever made.
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

