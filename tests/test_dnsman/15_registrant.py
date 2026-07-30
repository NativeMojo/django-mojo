"""
dnsman — the portal-managed registrant contact.

The contact is a per-group `Setting` row with a house (global) fallback, edited
through GET/POST `/api/dnsman/registrant`. Two properties carry the weight and
both are asserted here:

1. **A tenant never learns the house contact.** A group with no contact of its
   own inherits one for purchasing purposes, but its editor shows nulls — not
   the inherited values, and not which of the inherited fields are malformed.
2. **`purchase()` resolves the contact from `row.group`.** The `group` argument
   is optional attribution and is None whenever the caller's group went
   inactive between quote and confirm; resolving from it would file the HOUSE
   contact on a tenant's domain at the one irreversible, real-money step.

Cleanup lives in setup because testit has no teardown decorator, and it goes
through `Setting.remove()` rather than a queryset delete: a queryset `.delete()`
never runs `Model.delete()`, so `remove_from_cache()` would not fire and a stale
Redis entry would keep resolving. A leaked global row here would also shadow
`12_config.py`'s conf-file fixture on the NEXT run.
"""

from unittest import mock

from testit import helpers as th

from tests.test_dnsman._helpers import (
    make_user, make_group, make_group_member, login, assert_no_secrets,
    DNS_PERMS,
)


URL = "/api/dnsman/registrant"

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

# The IDENTIFYING house values — what a leak would actually disclose. The
# non-identifying fields (ContactType, CountryCode, State) are short enum-ish
# tokens that collide with ordinary response text: "US" is a substring of the
# "USD" currency every config response carries, so scanning for them would
# fail on a body that leaked nothing.
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


def _data(resp):
    """A plain-dict return is wrapped as {"status":..., "data": <dict>}."""
    return resp.response["data"]


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
def setup_registrant(opts):
    """Fresh scopes every run. Clean up BEFORE creating — long-lived DB."""
    opts.admin, opts.admin_email, opts.admin_pw = make_user(
        DNS_PERMS, is_superuser=True)
    opts.viewer, opts.viewer_email, opts.viewer_pw = make_user(["view_dns"])
    # Global (User-level) manage_dns, NOT a superuser: reaches any group by id
    # via the framework's global-perm short-circuit, but not the house scope's
    # platform gate.
    opts.global_mgr, opts.global_mgr_email, opts.global_mgr_pw = make_user(DNS_PERMS)

    # Tenant admins: perms on a GroupMember row only.
    opts.user_a, opts.email_a, opts.pw_a, opts.group_a = make_group_member(DNS_PERMS)
    opts.user_b, opts.email_b, opts.pw_b, opts.group_b = make_group_member(DNS_PERMS)
    opts.group_empty = make_group("regempty")

    # A group that exists but is switched off — ?group= on it resolves to None.
    opts.group_dead = make_group("regdead")
    opts.group_dead.is_active = False
    opts.group_dead.save()

    _clear(opts.group_a, opts.group_b, opts.group_empty, opts.group_dead)


# ---------------------------------------------------------------------------
# 1-2. the happy paths, one per scope
# ---------------------------------------------------------------------------

@th.django_unit_test("a platform admin writes and reads back the house contact")
def test_global_write_then_read(opts):
    _clear()
    login(opts, opts.admin_email, opts.admin_pw)

    resp = opts.client.post(URL, dict(contact=dict(HOUSE_CONTACT)))
    assert resp.status_code == 200, \
        f"a platform admin could not save the house contact: {resp.status_code} {resp.response}"
    body = _data(resp)
    assert body["scope"] == "global", f"expected the global scope, got {body['scope']!r}"
    assert body["source"] == "database", \
        f"a saved contact must report source 'database', got {body['source']!r}"
    assert body["problems"] == [], f"a valid contact reported problems: {body['problems']}"
    assert body["effective_configured"] is True, \
        "a complete house contact must make the house scope configured"

    resp = opts.client.get(URL)
    assert resp.status_code == 200, f"reading back the house contact failed: {resp.status_code}"
    body = _data(resp)
    assert body["contact"]["Email"] == HOUSE_CONTACT["Email"], \
        "the house contact did not round-trip through the Setting row"
    assert body["contact"]["PhoneNumber"] == HOUSE_CONTACT["PhoneNumber"], \
        "the house contact did not round-trip through the Setting row"

    _clear()


@th.django_unit_test("a tenant admin writes and reads back their own group's contact")
def test_group_write_then_read(opts):
    _clear(opts.group_a)
    login(opts, opts.email_a, opts.pw_a)

    resp = opts.client.post(URL, dict(group=opts.group_a.pk, contact=dict(TENANT_CONTACT)))
    assert resp.status_code == 200, \
        f"a group manage_dns holder could not save their contact: {resp.status_code} {resp.response}"
    body = _data(resp)
    assert body["scope"] == "group", f"expected the group scope, got {body['scope']!r}"
    assert body["group"] == opts.group_a.pk, \
        f"the response named the wrong group: {body['group']}"
    assert body["inherited"] is False, \
        "a group with its own contact must not report the contact as inherited"

    resp = opts.client.get(URL, params=dict(group=opts.group_a.pk))
    assert resp.status_code == 200, f"reading back the group contact failed: {resp.status_code}"
    assert _data(resp)["contact"]["Email"] == TENANT_CONTACT["Email"], \
        "the group contact did not round-trip"

    _clear(opts.group_a)


# ---------------------------------------------------------------------------
# 3-4. fallback and override
# ---------------------------------------------------------------------------

@th.django_unit_test("a group with no contact inherits without ever seeing the house one")
def test_group_inherits_house_contact_opaquely(opts):
    _clear(opts.group_a)
    _set(HOUSE_CONTACT)
    login(opts, opts.email_a, opts.pw_a)

    resp = opts.client.get(URL, params=dict(group=opts.group_a.pk))
    assert resp.status_code == 200, f"a tenant admin could not read their scope: {resp.status_code}"
    body = _data(resp)

    assert body["contact"] is None, \
        "a group with no row of its own must report no contact, not the inherited one"
    assert body["source"] == "none", \
        f"a group with no row must report source 'none', got {body['source']!r}"
    assert body["inherited"] is True, \
        "a group inheriting a contact must say so"
    assert body["effective_configured"] is True, \
        "the inherited house contact makes purchasing available to this group"
    assert body["problems"] == [], \
        "problems must describe this scope's own row only, never the inherited contact"

    blob = str(resp.response)
    for value in HOUSE_VALUES:
        assert value not in blob, \
            f"the house contact value {value!r} leaked into a tenant's registrant response"
    assert_no_secrets(resp.response, URL)

    _clear(opts.group_a)


@th.django_unit_test("a group's own contact overrides the house one for purchasing")
def test_group_contact_overrides_house(opts):
    from mojo.apps.dnsman.services import registrar

    _clear(opts.group_a)
    _set(HOUSE_CONTACT)
    _set(TENANT_CONTACT, group=opts.group_a)

    resolved = registrar._registrant_contact(opts.group_a)
    assert resolved["Email"] == TENANT_CONTACT["Email"], \
        f"a group with its own contact must register under it, got {resolved['Email']}"

    house = registrar._registrant_contact(None)
    assert house["Email"] == HOUSE_CONTACT["Email"], \
        "the house contact must be unaffected by a group's override"

    _clear(opts.group_a)


# ---------------------------------------------------------------------------
# 5-8. the permission boundary
# ---------------------------------------------------------------------------

@th.django_unit_test("a tenant admin cannot reach another tenant's contact")
def test_cross_tenant_group_denied(opts):
    _clear(opts.group_a, opts.group_b)
    _set(TENANT_CONTACT, group=opts.group_b)
    login(opts, opts.email_a, opts.pw_a)

    resp = opts.client.get(URL, params=dict(group=opts.group_b.pk))
    assert resp.status_code in (401, 403), \
        f"a GroupMember-only admin of A read group B's contact (status {resp.status_code})"
    blob = str(resp.response)
    assert TENANT_CONTACT["Email"] not in blob, \
        "a denied cross-tenant read still leaked the other group's contact"

    resp = opts.client.post(URL, dict(group=opts.group_b.pk, contact=dict(HOUSE_CONTACT)))
    assert resp.status_code in (401, 403), \
        f"a GroupMember-only admin of A wrote group B's contact (status {resp.status_code})"

    _clear(opts.group_b)


@th.django_unit_test("a GLOBAL manage_dns holder reaches any group — pre-existing framework shape")
def test_global_perm_holder_reaches_any_group(opts):
    """
    Recorded on purpose, not asserted as isolation.

    `Group.user_has_permission` short-circuits on the user's GLOBAL permission
    dict before it ever consults GroupMember, so a holder of global manage_dns
    reaches every group by naming its id, with no membership. That is framework
    shape shared by every dnsman endpoint; this item does not change it. Pinning
    it here means a future change to that rule shows up as a failing test rather
    than as a silently altered boundary.
    """
    _clear(opts.group_b)
    _set(TENANT_CONTACT, group=opts.group_b)
    login(opts, opts.global_mgr_email, opts.global_mgr_pw)

    resp = opts.client.get(URL, params=dict(group=opts.group_b.pk))
    assert resp.status_code == 200, (
        "a global manage_dns holder is expected to reach any group by id "
        f"(pre-existing behaviour); got {resp.status_code}")
    assert _data(resp)["contact"]["Email"] == TENANT_CONTACT["Email"], \
        "the global-perm read did not return the group's contact"

    _clear(opts.group_b)


@th.django_unit_test("the house contact requires a platform admin, not just manage_dns")
def test_house_scope_requires_platform_admin(opts):
    _clear()
    _set(HOUSE_CONTACT)

    for label, email, password in (
            ("a tenant admin", opts.email_a, opts.pw_a),
            ("a global manage_dns holder", opts.global_mgr_email, opts.global_mgr_pw)):
        login(opts, email, password)
        resp = opts.client.get(URL)
        assert resp.status_code in (401, 403), \
            f"{label} read the house registrant contact (status {resp.status_code})"
        blob = str(resp.response)
        for value in HOUSE_VALUES:
            assert value not in blob, \
                f"a refusal to {label} still leaked the house contact value {value!r}"

        resp = opts.client.post(URL, dict(contact=dict(TENANT_CONTACT)))
        assert resp.status_code in (401, 403), \
            f"{label} wrote the house registrant contact (status {resp.status_code})"

    _clear()


@th.django_unit_test("view_dns alone reads neither scope — the contact is PII")
def test_view_dns_cannot_read_or_write(opts):
    _clear(opts.group_a)
    _set(HOUSE_CONTACT)
    _set(TENANT_CONTACT, group=opts.group_a)
    login(opts, opts.viewer_email, opts.viewer_pw)

    for label, params in (("the house scope", None),
                          ("a group scope", dict(group=opts.group_a.pk))):
        resp = opts.client.get(URL, params=params)
        assert resp.status_code in (401, 403), \
            f"view_dns read {label} of the registrant contact (status {resp.status_code})"

    body = dict(contact=dict(HOUSE_CONTACT))
    resp = opts.client.post(URL, body)
    assert resp.status_code in (401, 403), \
        f"view_dns wrote the house registrant contact (status {resp.status_code})"

    body["group"] = opts.group_a.pk
    resp = opts.client.post(URL, body)
    assert resp.status_code in (401, 403), \
        f"view_dns wrote a group registrant contact (status {resp.status_code})"

    _clear(opts.group_a)


# ---------------------------------------------------------------------------
# 9-11. validation and readable refusals
# ---------------------------------------------------------------------------

@th.django_unit_test("an incomplete contact is a 400 naming the field, and writes nothing")
def test_incomplete_contact_refused(opts):
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.dnsman.services.registrar import REGISTRANT_CONTACT_KEY

    _clear(opts.group_a)
    login(opts, opts.email_a, opts.pw_a)

    partial = dict(TENANT_CONTACT)
    partial.pop("Email")
    resp = opts.client.post(URL, dict(group=opts.group_a.pk, contact=partial))
    assert resp.status_code == 400, \
        f"an incomplete contact should be a readable 400, got {resp.status_code}"
    assert "Email" in str(resp.response), \
        f"the refusal must name the missing field, got {resp.response}"
    assert not Setting.objects.filter(
        key=REGISTRANT_CONTACT_KEY, group=opts.group_a).exists(), \
        "a refused contact must not leave a row behind"


@th.django_unit_test("malformed shape is refused before AWS ever sees it")
def test_malformed_contact_refused(opts):
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.dnsman.services.registrar import REGISTRANT_CONTACT_KEY

    _clear(opts.group_a)
    login(opts, opts.email_a, opts.pw_a)

    cases = [
        ("ContactType", dict(TENANT_CONTACT, ContactType="INDIVIDUAL")),
        ("PhoneNumber", dict(TENANT_CONTACT, PhoneNumber="512-555-0199")),
        ("Zipcode", dict(TENANT_CONTACT, Zipcode="73301")),
    ]
    for field, contact in cases:
        resp = opts.client.post(URL, dict(group=opts.group_a.pk, contact=contact))
        assert resp.status_code == 400, \
            f"a bad {field} should be a readable 400, got {resp.status_code}"
        assert field in str(resp.response), \
            f"the refusal must name {field}, got {resp.response}"

    assert not Setting.objects.filter(
        key=REGISTRANT_CONTACT_KEY, group=opts.group_a).exists(), \
        "a refused contact must not leave a row behind"


@th.django_unit_test("a deactivated ?group= is a readable 400, not a platform-admin refusal")
def test_inactive_group_is_a_value_error(opts):
    login(opts, opts.email_a, opts.pw_a)

    resp = opts.client.get(URL, params=dict(group=opts.group_dead.pk))
    assert resp.status_code == 400, (
        "a deactivated group must be explained, not silently promoted to the "
        f"house scope and refused by the platform gate; got {resp.status_code}")
    assert "not active" in str(resp.response), \
        f"the refusal must say the group is not active, got {resp.response}"


# ---------------------------------------------------------------------------
# 12. clearing
# ---------------------------------------------------------------------------

@th.django_unit_test("clear drops the row AND its Redis entry")
def test_clear_removes_row_and_cache(opts):
    from mojo.helpers.settings import settings
    from mojo.apps.dnsman.services.registrar import REGISTRANT_CONTACT_KEY

    _clear(opts.group_a)
    _set(TENANT_CONTACT, group=opts.group_a)
    login(opts, opts.email_a, opts.pw_a)

    resp = opts.client.post(URL, dict(group=opts.group_a.pk, clear=True))
    assert resp.status_code == 200, f"clear failed: {resp.status_code} {resp.response}"
    body = _data(resp)
    assert body["contact"] is None, "a cleared scope must report no contact"
    assert body["source"] == "none", \
        f"a cleared scope must report source 'none', got {body['source']!r}"

    # Read through the settings chain, not the DB: a queryset delete would drop
    # the row while leaving Redis resolving the old value.
    resolved = settings.get(REGISTRANT_CONTACT_KEY, {}, group=opts.group_a, kind="dict")
    assert not resolved, \
        f"a cleared contact still resolves through the settings cache: {sorted(resolved.keys())}"


# ---------------------------------------------------------------------------
# 13-14. the money path
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
            registrar.purchase(None, opts.user_a, quoted.purchase, quoted.token)

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


# ---------------------------------------------------------------------------
# 15. the config endpoint stays a boolean
# ---------------------------------------------------------------------------

@th.django_unit_test("config reports the per-group boolean and still echoes no contact")
def test_config_is_group_aware_and_opaque(opts):
    _clear(opts.group_a, opts.group_empty)
    _set(HOUSE_CONTACT)
    _set(dict(TENANT_CONTACT, PhoneNumber=""), group=opts.group_a)
    login(opts, opts.global_mgr_email, opts.global_mgr_pw)

    # Group A's own contact is broken, so purchasing is unavailable TO IT even
    # though the house contact is fine — the boolean has to be per-group.
    resp = opts.client.get("/api/dnsman/config", params=dict(group=opts.group_a.pk))
    assert resp.status_code == 200, f"config returned {resp.status_code}"
    assert _data(resp)["registrant_contact_configured"] is False, \
        "a group whose own contact is incomplete must report purchasing unavailable"

    # A group with nothing of its own inherits the house contact.
    resp = opts.client.get("/api/dnsman/config", params=dict(group=opts.group_empty.pk))
    assert resp.status_code == 200, f"config returned {resp.status_code}"
    assert _data(resp)["registrant_contact_configured"] is True, \
        "a group with no contact of its own must inherit the house one"

    assert_no_secrets(resp.response, "/api/dnsman/config")
    blob = str(resp.response)
    for value in HOUSE_VALUES:
        assert value not in blob, \
            f"config leaked the registrant contact value {value!r}"

    _clear(opts.group_a)
