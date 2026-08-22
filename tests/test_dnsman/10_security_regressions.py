"""dnsman — regressions for the two exploitable findings from the item-392 security review.

Each test here fails against the pre-fix code. They are kept in their own module
so it is obvious what they defend and why they must not be relaxed.

1. `DomainPurchase` was writable over REST by a read-only `view_dns` holder.
   `CAN_UPDATE` defaults to True, and the save gate reads
   `["SAVE_PERMS", "VIEW_PERMS"]` as a FALLBACK CHAIN — first declared wins —
   so a model with no `SAVE_PERMS` authorizes writes with its `VIEW_PERMS`.
   A consumed quote could be re-armed by resetting `status`/`quote_expires` and
   planting a `confirm_token` hash of the attacker's choosing.

2. Record names were validated by zone SUFFIX only, never by charset. A name
   like `a/../../../../victim.com/records/A/@.example.com` passes an
   `endswith()` check; the HTTP client then normalizes the dot-segments, and a
   provider that interpolates the relative label into a REST path writes to a
   DIFFERENT domain in the same account — turning one managed domain into write
   access to every domain the credential can reach.
"""

from testit import helpers as th

from tests.test_dnsman._helpers import (
    make_user, make_group, make_domain, login,
)


@th.django_unit_setup()
def setup_security_regressions(opts):
    from mojo.apps.dnsman.models import Domain, DomainPurchase

    DomainPurchase.objects.filter(domain_name__startswith="secreg-").delete()
    Domain.objects.filter(name__startswith="secreg-").delete()

    opts.group = make_group("secreg")
    opts.viewer, opts.viewer_email, opts.viewer_pw = make_user(["view_dns"])
    opts.manager, opts.manager_email, opts.manager_pw = make_user(["manage_dns"])

    opts.domain = make_domain(name="secreg-example.com", group=None,
                              provider="godaddy", status="active")

    opts.purchase = DomainPurchase.objects.create(
        group=None, user=None, domain_name="secreg-bought.com",
        status="completed", price="12.00", cost="12.00", currency="USD")


# ---------------------------------------------------------------------------
# 1. The money ledger is genuinely read-only over REST
# ---------------------------------------------------------------------------

@th.django_unit_test("DomainPurchase declares CAN_UPDATE False")
def test_purchase_can_update_false(opts):
    from mojo.apps.dnsman.models import DomainPurchase

    # Asserted at the RestMeta level as well as over HTTP: CAN_UPDATE defaults
    # to True, so its ABSENCE is the bug. A future edit that drops this line
    # must fail here even if the HTTP test below is somehow satisfied.
    assert DomainPurchase.RestMeta.CAN_UPDATE is False, \
        "DomainPurchase.RestMeta must declare CAN_UPDATE = False — it defaults to True"
    assert DomainPurchase.get_rest_meta_prop("SAVE_PERMS", None) is not None, \
        ("DomainPurchase must declare SAVE_PERMS — the save gate reads "
         "[SAVE_PERMS, VIEW_PERMS] as a fallback chain, so omitting it "
         "authorizes writes with the read permission")


@th.django_unit_test("a view_dns holder cannot write the purchase ledger")
def test_purchase_write_refused_for_viewer(opts):
    from mojo.apps.dnsman.models import DomainPurchase

    login(opts, opts.viewer_email, opts.viewer_pw)
    resp = opts.client.post(f"/api/dnsman/purchase/{opts.purchase.pk}",
                            json=dict(price="0.01", status="quoted"))
    assert resp.status_code != 200, \
        f"a read-only view_dns holder wrote the money ledger (status {resp.status_code})"

    opts.purchase.refresh_from_db()
    assert str(opts.purchase.price) == "12.00", \
        f"purchase price was mutated over REST (now {opts.purchase.price})"
    assert opts.purchase.status == "completed", \
        f"purchase status was mutated over REST (now {opts.purchase.status})"


@th.django_unit_test("a consumed quote cannot be re-armed over REST")
def test_purchase_cannot_be_rearmed(opts):
    login(opts, opts.manager_email, opts.manager_pw)
    resp = opts.client.post(f"/api/dnsman/purchase/{opts.purchase.pk}", json=dict(
        status="quoted",
        confirm_token="a" * 64,
        quote_expires="2099-01-01T00:00:00Z"))
    assert resp.status_code != 200, \
        f"a completed purchase was re-armed over REST (status {resp.status_code})"

    opts.purchase.refresh_from_db()
    assert opts.purchase.status == "completed", \
        "status was reset, re-arming the confirm step of the money path"
    assert opts.purchase.confirm_token != "a" * 64, \
        "an attacker-chosen confirm_token hash was planted on the row"


# ---------------------------------------------------------------------------
# 2. Record names cannot escape their provider URL path
# ---------------------------------------------------------------------------

@th.django_unit_test("a traversal record name is rejected at normalization")
def test_record_name_traversal_rejected(opts):
    from mojo.apps.dnsman.services import naming
    from mojo import errors as me

    # This is the exact payload that reached api.godaddy.com/v1/domains/victim.com
    # before the fix: it satisfies endswith(".example.com") but the leading
    # portion is a path escape.
    hostile = "a/../../../../victim.com/records/A/@"
    try:
        naming.normalize_record_name(hostile, "example.com")
        raise AssertionError(
            "a record name containing path separators was accepted — this is a "
            "cross-domain write primitive on providers that build URL paths")
    except me.ValueException:
        pass

    for bad in ["a/b", "..", "a b", "x/../y", "a%2fb"]:
        try:
            naming.normalize_record_name(bad, "example.com")
            raise AssertionError(f"record name {bad!r} should have been rejected")
        except me.ValueException:
            pass


@th.django_unit_test("legitimate record names still normalize")
def test_legitimate_record_names_accepted(opts):
    from mojo.apps.dnsman.services import naming

    cases = {
        "@": "example.com",
        "www": "www.example.com",
        "_acme-challenge": "_acme-challenge.example.com",
        "_dmarc": "_dmarc.example.com",
        "*": "*.example.com",
        "sel1._domainkey": "sel1._domainkey.example.com",
        "_acme-challenge.example.com": "_acme-challenge.example.com",
    }
    for given, expected in cases.items():
        got = naming.normalize_record_name(given, "example.com")
        assert got == expected, f"normalize_record_name({given!r}) -> {got!r}, expected {expected!r}"


@th.django_unit_test("a wildcard is only legal as the leftmost label")
def test_wildcard_position(opts):
    from mojo.apps.dnsman.services import naming
    from mojo import errors as me

    try:
        naming.normalize_record_name("sub.*", "example.com")
        raise AssertionError("a non-leftmost wildcard should be rejected")
    except me.ValueException:
        pass


@th.django_unit_test("the GoDaddy URL builder percent-encodes every segment")
def test_godaddy_url_encoding(opts):
    # Every GoDaddy URL is built here — the adapter no longer assembles paths of
    # its own, so this is the single layer that makes traversal impossible.
    from mojo.helpers.dns import godaddy

    url = godaddy.build_url("domains", "example.com", "records", "TXT",
                            "a/../../../victim.com/records/A/@")
    assert "victim.com/records" not in url, \
        f"unencoded path separators survived into the provider URL: {url}"
    assert "%2F" in url.upper(), \
        f"path separators were not percent-encoded: {url}"
    assert url.startswith(godaddy.BASE_URL + "/"), \
        f"the built URL escaped the GoDaddy API base: {url}"


@th.django_unit_test("every GoDaddy record call routes through the encoding URL builder")
def test_godaddy_record_calls_use_the_url_builder(opts):
    """
    The adapter used to build its own percent-encoded paths. Now that it does
    not, prove the traversal defence is still on the live write path: a hostile
    relative label must reach the HTTP transport already encoded. The
    transport is injected through DNSManager's ``http=`` seam (item #2558) —
    never a process-global patch of the shared godaddy module.
    """
    from unittest.mock import MagicMock

    from mojo.helpers.dns.godaddy import DNSManager

    hostile = "a/../../../victim.com/records/A/@"
    resp = MagicMock()
    resp.content = b""
    resp.status_code = 200

    recorded = MagicMock()
    recorded.get.return_value = resp
    recorded.put.return_value = resp

    manager = DNSManager("key", "secret", raise_on_error=True, http=recorded)
    manager.get_record("example.com", "TXT", hostile)
    manager.edit_record("example.com", "TXT", hostile, "value", 600)

    for label, call in (("GET", recorded.get.call_args),
                        ("PUT", recorded.put.call_args)):
        url = call.args[0]
        assert "victim.com/records" not in url, \
            f"the {label} reached a different domain in the account: {url}"
        assert url.startswith("https://api.godaddy.com/v1/domains/example.com/records/TXT/"), \
            f"the {label} URL left the requested domain's record path: {url}"


# ---------------------------------------------------------------------------
# 3. Supporting hardening from the same review
# ---------------------------------------------------------------------------

@th.django_unit_test("WHOIS contact PII is not readable with view_dns alone")
def test_whois_requires_manage(opts):
    login(opts, opts.viewer_email, opts.viewer_pw)
    resp = opts.client.get("/api/dnsman/whois", params=dict(domain=opts.domain.pk))
    # The registrar returns the real registrant name, address, phone and email
    # to the account owner regardless of WHOIS privacy — that is PII.
    assert resp.status_code in (401, 403), \
        f"view_dns read registrant contact PII (status {resp.status_code})"


@th.django_unit_test("secrets are not writable through the generic save path")
def test_secrets_not_writable(opts):
    from mojo.apps.dnsman.models import DnsCredential, Certificate

    for model in (DnsCredential, Certificate):
        no_save = set(model.RestMeta.NO_SAVE_FIELDS)
        assert "secrets" in no_save and "mojo_secrets" in no_save, (
            f"{model.__name__}.NO_SAVE_FIELDS must block secrets/mojo_secrets — "
            "on_rest_save_field prefers a set_<key> method and the secrets "
            "mixins expose set_secrets")


@th.django_unit_test("server-owned error fields are not writable")
def test_error_fields_not_writable(opts):
    from mojo.apps.dnsman.models import Domain, Certificate

    assert "last_error" in set(Domain.RestMeta.NO_SAVE_FIELDS), \
        "Domain.last_error is server-owned and must not be writable over REST"
    assert "last_error" in set(Certificate.RestMeta.NO_SAVE_FIELDS), \
        "Certificate.last_error is server-owned and must not be writable over REST"
