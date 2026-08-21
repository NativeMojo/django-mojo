"""Split out of tests/test_dnsman/15_registrant.py (maestro #1839).

The two money-path tests hold a passthrough patch on the shared settings
singleton (mojo.helpers.settings.settings.get) to flip the purchasing kill
switch for IN-PROCESS registrar calls — process-global, so unsafe under the
parallel default tier. The registrant-contact semantics they assert are
unchanged; see the source module's docstring.
"""

from unittest import mock

from testit import helpers as th

from tests.test_dnsman._helpers import make_group_member, DNS_PERMS


R53 = "mojo.helpers.aws.route53"


HOUSE_CONTACT = {
    "FirstName": "House",
    "LastName": "Operator",
    "ContactType": "COMPANY",
    "AddressLine1": "500 House Way",
    "City": "Portland",
    "State": "OR",
    "CountryCode": "US",
    "ZipCode": "97201",
    "PhoneNumber": "+1.5035550100",
    "Email": "house-ops@example.com",
}


TENANT_CONTACT = {
    "FirstName": "Tenant",
    "LastName": "Admin",
    "ContactType": "PERSON",
    "AddressLine1": "9 Tenant Street",
    "City": "Austin",
    "State": "TX",
    "CountryCode": "US",
    "ZipCode": "73301",
    "PhoneNumber": "+1.5125550199",
    "Email": "tenant-admin@example.com",
}


HOUSE_VALUES = [
    HOUSE_CONTACT[field] for field in (
        "FirstName", "LastName", "AddressLine1", "City",
        "ZipCode", "PhoneNumber", "Email")
]


def _clear(*groups):
    """Drop the contact row for the global scope and each named group."""
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.dnsman.services.registrar import REGISTRANT_CONTACT_KEY

    Setting.remove(REGISTRANT_CONTACT_KEY, group=None)
    for group in groups:
        if group is not None:
            Setting.remove(REGISTRANT_CONTACT_KEY, group=group)


def _set(contact, group=None):
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.dnsman.services.registrar import REGISTRANT_CONTACT_KEY

    return Setting.set(REGISTRANT_CONTACT_KEY, dict(contact),
                       is_secret=True, group=group)


def _purchasing_on():
    """
    Flip the kill switch for an IN-PROCESS service call, passing everything
    else through to the real resolver.

    Deliberately not a blanket patch: the registrant contact must still resolve
    through the real settings chain, because the per-group Setting row IS what
    these tests are about. `th.server_settings` is for the separate server
    process and does nothing here.
    """
    from mojo.helpers.settings import settings as settings_obj

    real_get = settings_obj.get

    def patched(key, *args, **kwargs):
        if key == "DNSMAN_PURCHASE_ENABLED":
            return True
        return real_get(key, *args, **kwargs)

    return patched


@th.django_unit_setup()
def setup_registrant_extended(opts):
    """One fresh tenant scope. Clean up BEFORE creating — long-lived DB."""
    opts.user_a, opts.email_a, opts.pw_a, opts.group_a = make_group_member(DNS_PERMS)
    _clear(opts.group_a)


# ---------------------------------------------------------------------------
# the money path
# ---------------------------------------------------------------------------

@th.django_unit_test("a group's incomplete contact refuses the quote before any AWS call")
def test_quote_refuses_on_group_contact(opts):
    from mojo.apps.dnsman.models import DomainPurchase
    from mojo.apps.dnsman.services import registrar
    from mojo.helpers.settings import settings as settings_obj
    from mojo import errors as me

    name = "reg-quote-scope.com"
    DomainPurchase.objects.filter(domain_name=name).delete()
    _clear(opts.group_a)
    # The HOUSE contact is complete; the group's own is not. The group's must win.
    _set(HOUSE_CONTACT)
    _set(dict(TENANT_CONTACT, Email=""), group=opts.group_a)

    raised = None
    with mock.patch.object(settings_obj, "get", side_effect=_purchasing_on()):
        with mock.patch(f"{R53}.check_availability") as check:
            try:
                registrar.quote(opts.group_a, opts.user_a, name)
            except me.ValueException as err:
                raised = err

    assert raised is not None, \
        "a group whose own contact is incomplete must be refused, house contact or not"
    assert "Email" in str(raised), \
        f"the refusal must name the missing field, got {raised}"
    assert check.call_count == 0, \
        "an incomplete group contact must be refused before any AWS call"
    assert DomainPurchase.objects.filter(domain_name=name).count() == 0, \
        "a refused quote must not create a purchase row"

    _clear(opts.group_a)


@th.django_unit_test("purchase files the QUOTE's group contact, never the house one")
def test_purchase_uses_row_group_not_argument(opts):
    """
    The D0 regression. Fails against code that resolved the contact from
    `purchase()`'s `group` argument.

    That argument is optional attribution — the scoping check a few lines above
    accepts None outright — and `request.group` is None whenever the caller's
    group went inactive between the quote and this confirmation. `row.group` is
    the authority: it is what the Domain row is created with. Getting this
    backwards files the OPERATOR's name, address, phone and email as the
    registrant of a tenant's domain, at the one step that cannot be undone.
    """
    from mojo.apps.dnsman.models import Domain, DomainPurchase
    from mojo.apps.dnsman.services import registrar
    from mojo.helpers.settings import settings as settings_obj

    name = "reg-d0-regression.com"
    Domain.objects.filter(name=name).delete()
    DomainPurchase.objects.filter(domain_name=name).delete()
    _clear(opts.group_a)
    _set(HOUSE_CONTACT)
    _set(TENANT_CONTACT, group=opts.group_a)

    from objict import objict

    avail = objict(name=name, status="AVAILABLE", available=True, price=11.0,
                   currency="USD", tld="com", tld_supported=True,
                   privacy_supported=True)
    registered = objict(name=name, operation_id="op-d0", privacy=True,
                        privacy_downgraded=False)

    with mock.patch.object(settings_obj, "get", side_effect=_purchasing_on()):
        with mock.patch(f"{R53}.check_availability", return_value=avail):
            quoted = registrar.quote(opts.group_a, opts.user_a, name)
        with mock.patch(f"{R53}.register", return_value=registered) as register:
            # group=None on purpose: exactly what the REST layer passes when the
            # buyer's group has gone inactive since the quote.
            registrar.purchase(
                None, opts.user_a, quoted.purchase, quoted.token,
                quoted.name, quoted.price)

    sent = register.call_args.args[1]
    assert sent["Email"] == TENANT_CONTACT["Email"], (
        "purchase filed the wrong registrant: expected the QUOTE's group contact "
        f"({TENANT_CONTACT['Email']}), got {sent['Email']}")
    assert sent["Email"] != HOUSE_CONTACT["Email"], \
        "purchase filed the HOUSE contact on a tenant's domain"

    row = DomainPurchase.objects.get(pk=quoted.purchase)
    assert row.metadata.get("registrant_scope") == opts.group_a.pk, (
        "the ledger must record which scope's contact was filed, got "
        f"{row.metadata.get('registrant_scope')!r}")
    assert row.metadata.get("registrant_fingerprint"), \
        "the ledger must record a fingerprint of the contact that was filed"
    for value in HOUSE_VALUES:
        assert value not in str(row.metadata), \
            "the ledger metadata must not store contact values"

    Domain.objects.filter(name=name).delete()
    DomainPurchase.objects.filter(domain_name=name).delete()
    _clear(opts.group_a)

