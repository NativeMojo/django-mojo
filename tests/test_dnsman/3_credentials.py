"""
BYO credential linking, domain onboarding, and the fail-closed credential gate.

(`mojo/apps/dnsman/services/onboarding.py` + `services/dns.get_adapter`)

Everything runs in-process with `mojo.helpers.dns.godaddy.DNSManager`, that
helper's `requests` module, and the Route53 helper patched — no GoDaddy call and
no AWS call is ever made.

Two properties matter most here and both are security properties:

  1. **Verify, then store.** A credential is only persisted after the provider
     confirms it works, so a failed first link leaves nothing behind and a
     failed rotation leaves the working credential untouched.
  2. **No account-wide surface.** A provider API key is account-wide. Only the
     COUNT of domains is ever stored, and ownership probes are per-name — so a
     caller can confirm a domain they already named and nothing else.
"""

TESTIT_TIER = "extended"

from unittest.mock import patch, MagicMock

from objict import objict
from testit import helpers as th


GD_HELPER = "mojo.helpers.dns.godaddy"
R53 = "mojo.helpers.aws.route53"

CRED_LINK = "dnsman-cred-test link"
CRED_ROTATE = "dnsman-cred-test rotate"
CRED_GATE = "dnsman-cred-test gate"
CRED_INACTIVE = "dnsman-cred-test inactive"
CRED_UNVERIFIED = "dnsman-cred-test unverified"
CRED_NAMES = [CRED_LINK, CRED_ROTATE, CRED_GATE, CRED_INACTIVE, CRED_UNVERIFIED]

BYO_DOMAIN = "byo.dnsmancred.com"
ADOPT_DOMAIN = "adopt.dnsmancred.com"
GATE_DOMAIN = "gate.dnsmancred.com"
TAKEN_DOMAIN = "taken.dnsmancred.com"
DOMAIN_NAMES = [BYO_DOMAIN, ADOPT_DOMAIN, GATE_DOMAIN, TAKEN_DOMAIN]

# Domains the mocked provider account "holds". None of these may ever appear in
# anything the services return or store.
ACCOUNT_DOMAINS = [{"domain": "private-one.example"}, {"domain": "private-two.example"}]


def _response(payload, status_code=200):
    resp = MagicMock()
    resp.content = b"[]"
    resp.status_code = status_code
    resp.json.return_value = payload
    return resp


def _http_error(status_code=401, message="Authentication failed"):
    """A requests-style HTTPError carrying a provider problem body."""
    from requests import HTTPError

    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = {"code": "ACCESS_DENIED", "message": message}
    return HTTPError(message, response=response)


def _usable_credential(name):
    from mojo.apps.dnsman.models import DnsCredential

    credential = DnsCredential(name=name, provider="godaddy",
                               is_active=True, verified=True)
    credential.set_credentials("old-key", "old-secret")
    credential.save()
    return credential


@th.django_unit_setup()
def setup_credentials(opts):
    """Long-lived database: delete this file's rows BEFORE creating them."""
    from mojo.apps.dnsman.models import DnsCredential, Domain

    Domain.objects.filter(name__in=DOMAIN_NAMES).delete()
    DnsCredential.objects.filter(name__in=CRED_NAMES).delete()

    opts.gate_credential = _usable_credential(CRED_GATE)

    inactive = _usable_credential(CRED_INACTIVE)
    inactive.is_active = False
    inactive.save()
    opts.inactive_credential = inactive

    unverified = _usable_credential(CRED_UNVERIFIED)
    unverified.verified = False
    unverified.save()
    opts.unverified_credential = unverified

    opts.gate_domain = Domain.objects.create(
        name=GATE_DOMAIN, provider="godaddy", status="active", verified=True)
    opts.taken_domain = Domain.objects.create(
        name=TAKEN_DOMAIN, provider="godaddy", status="active",
        credential=opts.gate_credential, verified=True)


# ---------------------------------------------------------------------------
# the fail-closed credential gate — before ANY network call
# ---------------------------------------------------------------------------

@th.tier("core")
@th.django_unit_test()
def test_missing_credential_fails_closed(opts):
    from mojo.apps.dnsman.services import dns
    from mojo import errors as me

    opts.gate_domain.credential = None

    raised = None
    with patch(f"{GD_HELPER}.DNSManager") as manager_cls, \
            patch(f"{GD_HELPER}.requests") as requests_mock:
        try:
            dns.upsert_record(opts.gate_domain, "TXT", "_acme-challenge", ["digest"])
        except me.ValueException as err:
            raised = err
        provider_calls = (manager_cls.call_count + requests_mock.get.call_count
                          + requests_mock.put.call_count)

    assert raised is not None, (
        "Expected a godaddy Domain with no credential to be refused")
    assert "no linked" in str(raised), (
        f"Expected an actionable 'link a credential' message, got {raised}")
    assert provider_calls == 0, (
        "Expected the credential gate to refuse BEFORE any provider call is made")


@th.tier("core")
@th.django_unit_test()
def test_inactive_credential_fails_closed(opts):
    from mojo.apps.dnsman.services import dns
    from mojo import errors as me

    opts.gate_domain.credential = opts.inactive_credential

    raised = None
    with patch(f"{GD_HELPER}.DNSManager") as manager_cls, \
            patch(f"{GD_HELPER}.requests") as requests_mock:
        try:
            dns.upsert_record(opts.gate_domain, "TXT", "_acme-challenge", ["digest"])
        except me.ValueException as err:
            raised = err
        provider_calls = (manager_cls.call_count + requests_mock.get.call_count
                          + requests_mock.put.call_count)

    assert raised is not None, "Expected an inactive credential to be refused"
    assert "inactive" in str(raised), (
        f"Expected the refusal to name the inactive credential, got {raised}")
    assert provider_calls == 0, (
        "Expected an inactive credential to be refused BEFORE any provider call")


@th.tier("core")
@th.django_unit_test()
def test_unverified_credential_fails_closed(opts):
    from mojo.apps.dnsman.services import dns
    from mojo import errors as me

    opts.gate_domain.credential = opts.unverified_credential

    raised = None
    with patch(f"{GD_HELPER}.DNSManager") as manager_cls, \
            patch(f"{GD_HELPER}.requests") as requests_mock:
        try:
            dns.list_records(opts.gate_domain)
        except me.ValueException as err:
            raised = err
        provider_calls = (manager_cls.call_count + requests_mock.get.call_count
                          + requests_mock.put.call_count)

    assert raised is not None, "Expected an unverified credential to be refused"
    assert "not been verified" in str(raised), (
        f"Expected the refusal to name the verification problem, got {raised}")
    assert provider_calls == 0, (
        "Expected an unverified credential to be refused BEFORE any provider call")


@th.django_unit_test()
def test_route53_needs_no_credential(opts):
    """Route53 rides the house AWS credentials — it must not be gated on one."""
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import dns

    domain = Domain(name="house.dnsmancred.com", provider="route53",
                    status="active", hosted_zone_id="ZHOUSE")
    assert domain.has_usable_credential is True, (
        "Expected a route53 Domain to need no linked credential")
    assert dns.get_adapter(domain).name == "route53", (
        "Expected a route53 Domain to resolve without a credential")


# ---------------------------------------------------------------------------
# linking — verify, THEN store
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_link_credential_verifies_then_stores(opts):
    from mojo.apps.dnsman.models import DnsCredential
    from mojo.apps.dnsman.services import onboarding

    with patch(f"{GD_HELPER}.DNSManager") as manager_cls, \
            patch(f"{GD_HELPER}.requests") as requests_mock:
        manager_cls.return_value.get_domains.return_value = list(ACCOUNT_DOMAINS)
        credential = onboarding.link_credential(
            None, "godaddy", "live-key", "live-secret", name=CRED_LINK)
        strict = manager_cls.call_args.kwargs.get("raise_on_error")

    assert credential.pk is not None, "Expected a verified credential to be persisted"
    assert credential.verified is True, "Expected a verified credential to be marked verified"
    assert credential.verified_at is not None, "Expected verified_at to be stamped"
    assert credential.last_error is None, (
        f"Expected no error on a successful link, got {credential.last_error}")
    assert strict is True, (
        "Expected dnsman to build the DNSManager with raise_on_error=True so a "
        "revoked key surfaces as an error instead of a silently-wrong answer")

    stored = DnsCredential.objects.get(pk=credential.pk)
    assert stored.api_key == "live-key", (
        f"Expected the API key to be stored, got {stored.api_key!r}")
    assert stored.api_secret == "live-secret", "Expected the API secret to be stored"
    assert stored.domain_count == len(ACCOUNT_DOMAINS), (
        f"Expected the domain COUNT to be recorded, got {stored.domain_count}")


@th.django_unit_test()
def test_link_credential_counts_a_real_array_response(opts):
    """
    The same link, but through the REAL DNSManager with only the socket mocked.

    GoDaddy's account list is a JSON **array**, and the helper used to wrap every
    body in an `objict` — which raises for a non-empty array. The count could
    therefore never be taken from an account that actually held domains, so this
    scripts a non-empty body deliberately.
    """
    from mojo.apps.dnsman.models import DnsCredential
    from mojo.apps.dnsman.services import onboarding

    with patch(f"{GD_HELPER}.requests") as requests_mock:
        requests_mock.get.return_value = _response(ACCOUNT_DOMAINS)
        credential = onboarding.link_credential(
            None, "godaddy", "live-key", "live-secret", name=CRED_LINK)
        get_calls = requests_mock.get.call_count
        url = requests_mock.get.call_args.args[0]

    stored = DnsCredential.objects.get(pk=credential.pk)
    assert stored.domain_count == len(ACCOUNT_DOMAINS), (
        f"Expected the count to be read off the array body, got {stored.domain_count}")
    assert get_calls == 1, (
        f"Expected exactly one account read for verification, got {get_calls}")
    assert url.endswith("/domains"), (
        f"Expected the verification probe to hit the account domain list, got {url}")


@th.django_unit_test()
def test_link_credential_never_exposes_the_account_domain_list(opts):
    """
    A provider key is account-wide. Only the count is kept — the list itself is
    never returned, stored, or serialized anywhere.
    """
    from mojo.apps.dnsman.models import DnsCredential
    from mojo.apps.dnsman.services import onboarding

    with patch(f"{GD_HELPER}.DNSManager") as manager_cls, \
            patch(f"{GD_HELPER}.requests") as requests_mock:
        manager_cls.return_value.get_domains.return_value = list(ACCOUNT_DOMAINS)
        credential = onboarding.link_credential(
            None, "godaddy", "live-key", "live-secret", name=CRED_LINK)

    stored = DnsCredential.objects.get(pk=credential.pk)
    haystacks = [
        str(credential.to_dict("default")),
        str(credential.to_dict("basic")),
        str(stored.__dict__),
    ]
    for entry in ACCOUNT_DOMAINS:
        for haystack in haystacks:
            assert entry["domain"] not in haystack, (
                f"The account's domain list must never be stored or serialized, "
                f"but '{entry['domain']}' appeared in: {haystack[:400]}")

    graph = credential.to_dict("default")
    assert "api_secret" not in graph, (
        f"Expected no raw api_secret field in the default graph, got {sorted(graph.keys())}")
    assert "api_key" not in graph, (
        f"Expected no raw api_key field in the default graph, got {sorted(graph.keys())}")
    assert "live-key" not in str(graph), (
        f"Expected the raw API key to stay out of every graph, got {graph}")
    assert "live-secret" not in str(graph), (
        f"Expected the raw API secret to stay out of every graph, got {graph}")


@th.django_unit_test()
def test_a_failed_first_link_persists_nothing(opts):
    from mojo.apps.dnsman.models import DnsCredential
    from mojo.apps.dnsman.services import onboarding
    from mojo import errors as me

    before = DnsCredential.objects.filter(name=CRED_ROTATE).count()

    raised = None
    with patch(f"{GD_HELPER}.DNSManager") as manager_cls, \
            patch(f"{GD_HELPER}.requests") as requests_mock:
        manager_cls.return_value.get_domains.side_effect = _http_error(
            401, "Invalid credentials")
        try:
            onboarding.link_credential(
                None, "godaddy", "bad-key", "bad-secret", name=CRED_ROTATE)
        except me.ValueException as err:
            raised = err

    assert raised is not None, "Expected an unverifiable credential to be refused"
    assert "could not be verified" in str(raised), (
        f"Expected a verification failure message, got {raised}")
    assert "Invalid credentials" in str(raised), (
        f"Expected the provider's own message to be surfaced, got {raised}")

    after = DnsCredential.objects.filter(name=CRED_ROTATE).count()
    assert after == before, (
        f"A failed FIRST link must persist nothing, but the row count went "
        f"from {before} to {after}")


@th.django_unit_test()
def test_rotation_replaces_the_secrets_only_after_verification(opts):
    from mojo.apps.dnsman.models import DnsCredential
    from mojo.apps.dnsman.services import onboarding

    credential = _usable_credential(CRED_ROTATE)

    with patch(f"{GD_HELPER}.DNSManager") as manager_cls, \
            patch(f"{GD_HELPER}.requests") as requests_mock:
        manager_cls.return_value.get_domains.return_value = list(ACCOUNT_DOMAINS)
        rotated = onboarding.link_credential(
            None, "godaddy", "new-key", "new-secret", credential=credential)

    assert rotated.pk == credential.pk, (
        "Expected rotation to update the existing row, not create a second one")
    stored = DnsCredential.objects.get(pk=credential.pk)
    assert stored.api_key == "new-key", (
        f"Expected the rotated key to be stored, got {stored.api_key!r}")
    assert stored.api_secret == "new-secret", "Expected the rotated secret to be stored"
    assert stored.verified is True, "Expected the rotated credential to stay verified"

    assert DnsCredential.objects.filter(name=CRED_ROTATE).count() == 1, (
        "Expected rotation to leave exactly one row")


@th.django_unit_test()
def test_a_failed_rotation_leaves_the_working_secrets_alone(opts):
    from mojo.apps.dnsman.models import DnsCredential
    from mojo.apps.dnsman.services import onboarding
    from mojo import errors as me

    credential = DnsCredential.objects.get(name=CRED_ROTATE)

    raised = None
    with patch(f"{GD_HELPER}.DNSManager") as manager_cls, \
            patch(f"{GD_HELPER}.requests") as requests_mock:
        manager_cls.return_value.get_domains.side_effect = _http_error(
            403, "Key revoked")
        try:
            onboarding.link_credential(
                None, "godaddy", "broken-key", "broken-secret", credential=credential)
        except me.ValueException as err:
            raised = err

    assert raised is not None, "Expected a failed rotation to raise"

    stored = DnsCredential.objects.get(pk=credential.pk)
    assert stored.api_key == "new-key", (
        f"A failed rotation must not overwrite the working key, got {stored.api_key!r}")
    assert stored.api_secret == "new-secret", (
        "A failed rotation must not overwrite the working secret")
    assert stored.verified is False, (
        "Expected a failed rotation to clear the verified flag")
    assert stored.last_error and "Key revoked" in stored.last_error, (
        f"Expected the provider's reason to be recorded, got {stored.last_error!r}")


@th.django_unit_test()
def test_only_byo_providers_can_be_linked(opts):
    from mojo.apps.dnsman.services import onboarding
    from mojo import errors as me

    raised = None
    with patch(f"{GD_HELPER}.DNSManager") as manager_cls:
        try:
            onboarding.link_credential(None, "route53", "key", "secret", name=CRED_LINK)
        except me.ValueException as err:
            raised = err
        calls = manager_cls.call_count

    assert raised is not None, "Expected route53 credentials to be unlinkable"
    assert calls == 0, "Expected an unsupported provider to be refused before any call"


# ---------------------------------------------------------------------------
# register_existing — self-serve BYO onboarding
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_register_existing_requires_an_active_held_domain(opts):
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import onboarding

    with patch(f"{GD_HELPER}.DNSManager") as manager_cls:
        manager = manager_cls.return_value
        manager.get_domain_info.return_value = objict(domain=BYO_DOMAIN, status="ACTIVE")
        domain = onboarding.register_existing(
            None, None, BYO_DOMAIN.upper(), opts.gate_credential)
        probed = manager.get_domain_info.call_args.args[0]
        listed = manager.get_domains.call_count

    assert domain.pk is not None, "Expected a confirmed BYO domain to be created"
    assert domain.name == BYO_DOMAIN, (
        f"Expected the name to be normalized to lowercase, got {domain.name}")
    assert domain.provider == "godaddy", (
        f"Expected the domain to be provider-tagged godaddy, got {domain.provider}")
    assert domain.status == "active", (
        f"Expected a confirmed domain to be active, got {domain.status}")
    assert domain.verified is True, "Expected the probe result to mark the domain verified"
    assert domain.credential_id == opts.gate_credential.pk, (
        "Expected the domain to be bound to the credential that proved control")
    assert probed == BYO_DOMAIN, (
        f"Expected the probe to be per-name, got {probed}")
    assert listed == 0, (
        "Expected NO account-wide listing during onboarding — the probe is per-name so a "
        "caller can only confirm a domain they already named")
    assert Domain.objects.filter(name=BYO_DOMAIN).count() == 1, (
        "Expected exactly one Domain row")


@th.django_unit_test()
def test_register_existing_refuses_an_already_tracked_name(opts):
    from mojo.apps.dnsman.services import onboarding
    from mojo import errors as me

    raised = None
    with patch(f"{GD_HELPER}.DNSManager") as manager_cls:
        try:
            onboarding.register_existing(None, None, TAKEN_DOMAIN, opts.gate_credential)
        except me.ValueException as err:
            raised = err
        calls = manager_cls.call_count

    assert raised is not None, "Expected an already-tracked name to be refused"
    assert "already tracked" in str(raised), (
        f"Expected the refusal to say the name is already tracked, got {raised}")
    assert calls == 0, "Expected an already-tracked name to be refused before any probe"


@th.django_unit_test()
def test_register_existing_refuses_a_non_active_domain(opts):
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import onboarding
    from mojo import errors as me

    Domain.objects.filter(name=ADOPT_DOMAIN).delete()

    raised = None
    with patch(f"{GD_HELPER}.DNSManager") as manager_cls:
        manager_cls.return_value.get_domain_info.return_value = objict(
            domain=ADOPT_DOMAIN, status="CANCELLED")
        try:
            onboarding.register_existing(None, None, ADOPT_DOMAIN, opts.gate_credential)
        except me.ValueException as err:
            raised = err

    assert raised is not None, "Expected a non-ACTIVE domain to be refused"
    assert "not ACTIVE" in str(raised), (
        f"Expected the refusal to name the provider status, got {raised}")
    assert not Domain.objects.filter(name=ADOPT_DOMAIN).exists(), (
        "A refused registration must create nothing")


@th.django_unit_test()
def test_register_existing_refuses_a_domain_the_account_does_not_hold(opts):
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import onboarding
    from mojo import errors as me

    Domain.objects.filter(name=ADOPT_DOMAIN).delete()

    raised = None
    with patch(f"{GD_HELPER}.DNSManager") as manager_cls:
        manager_cls.return_value.get_domain_info.side_effect = _http_error(
            404, "Domain not found")
        try:
            onboarding.register_existing(None, None, ADOPT_DOMAIN, opts.gate_credential)
        except me.ValueException as err:
            raised = err

    assert raised is not None, (
        "Expected a domain the account does not hold to be refused")
    assert "could not be confirmed" in str(raised), (
        f"Expected an ownership-probe failure message, got {raised}")
    assert not Domain.objects.filter(name=ADOPT_DOMAIN).exists(), (
        "A failed ownership probe must create nothing")


@th.django_unit_test()
def test_register_existing_refuses_an_unusable_credential(opts):
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import onboarding
    from mojo import errors as me

    Domain.objects.filter(name=ADOPT_DOMAIN).delete()

    for credential in (None, opts.inactive_credential, opts.unverified_credential):
        raised = None
        with patch(f"{GD_HELPER}.DNSManager") as manager_cls:
            try:
                onboarding.register_existing(None, None, ADOPT_DOMAIN, credential)
            except me.ValueException as err:
                raised = err
            calls = manager_cls.call_count

        label = credential.name if credential is not None else "no credential"
        assert raised is not None, (
            f"Expected registration with an unusable credential ({label}) to be refused")
        assert calls == 0, (
            f"Expected an unusable credential ({label}) to be refused before any probe")
        assert not Domain.objects.filter(name=ADOPT_DOMAIN).exists(), (
            f"Expected nothing to be created for an unusable credential ({label})")


# ---------------------------------------------------------------------------
# adopt_route53 — no purchase, no money
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_adopt_route53_uses_the_existing_zone(opts):
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import onboarding

    Domain.objects.filter(name=ADOPT_DOMAIN).delete()

    with patch(f"{R53}.find_zone_id", return_value="ZADOPTED") as find_zone, \
            patch(f"{R53}.get_zone", return_value=objict(private=False)), \
            patch(f"{R53}.create_hosted_zone") as create_zone, \
            patch(f"{R53}.register") as register:
        domain = onboarding.adopt_route53(None, None, ADOPT_DOMAIN)

    assert domain.pk is not None, "Expected the adopted domain to be created"
    assert domain.provider == "route53", (
        f"Expected the adopted domain to be route53, got {domain.provider}")
    assert domain.hosted_zone_id == "ZADOPTED", (
        f"Expected the existing zone id to be recorded, got {domain.hosted_zone_id}")
    assert domain.status == "active", (
        f"Expected an adopted domain to be active, got {domain.status}")
    assert domain.verified is True, "Expected an adopted zone to count as verified"
    assert find_zone.call_count == 1, "Expected the zone to be looked up exactly once"
    assert create_zone.call_count == 0, (
        "Expected no zone to be created when one already exists")
    assert register.call_count == 0, (
        "Adoption must never touch the registrar — no purchase, no money")


@th.django_unit_test()
def test_adopt_route53_refuses_a_private_only_zone(opts):
    """
    find_zone_id falls back to a PRIVATE zone when no public zone carries the
    name. A private zone is VPC-internal: it resolves nowhere on the public
    internet, so a certificate for it could never validate and its records would
    be invisible. Adopting one would look successful and be useless.
    """
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import onboarding
    from mojo import errors as me

    Domain.objects.filter(name=ADOPT_DOMAIN).delete()

    raised = None
    with patch(f"{R53}.find_zone_id", return_value="ZPRIVATE"), \
            patch(f"{R53}.get_zone", return_value=objict(private=True)), \
            patch(f"{R53}.create_hosted_zone") as create_zone:
        try:
            onboarding.adopt_route53(None, None, ADOPT_DOMAIN)
        except me.ValueException as err:
            raised = err
        created = create_zone.call_count

    assert raised is not None, "Expected a private-only zone to be refused"
    assert "private" in str(raised).lower(), (
        f"Expected the refusal to name the private zone as the reason, got {raised}")
    assert created == 0, "A refused adoption must not create a replacement zone"
    assert not Domain.objects.filter(name=ADOPT_DOMAIN).exists(), (
        "A refused adoption must create nothing")


@th.django_unit_test()
def test_adopt_route53_refuses_an_already_tracked_name(opts):
    from mojo.apps.dnsman.services import onboarding
    from mojo import errors as me

    raised = None
    with patch(f"{R53}.find_zone_id", return_value="ZADOPTED") as find_zone:
        try:
            onboarding.adopt_route53(None, None, TAKEN_DOMAIN)
        except me.ValueException as err:
            raised = err
        calls = find_zone.call_count

    assert raised is not None, "Expected an already-tracked name to be refused"
    assert "already tracked" in str(raised), (
        f"Expected the refusal to say the name is already tracked, got {raised}")
    assert calls == 0, "Expected an already-tracked name to be refused before any AWS call"


@th.django_unit_test()
def test_adopt_route53_without_a_zone_requires_create_zone(opts):
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import onboarding
    from mojo import errors as me

    Domain.objects.filter(name=ADOPT_DOMAIN).delete()

    raised = None
    with patch(f"{R53}.find_zone_id", return_value=None), \
            patch(f"{R53}.create_hosted_zone") as create_zone:
        try:
            onboarding.adopt_route53(None, None, ADOPT_DOMAIN)
        except me.ValueException as err:
            raised = err
        calls = create_zone.call_count

    assert raised is not None, "Expected adoption to refuse when no hosted zone exists"
    assert calls == 0, (
        "Expected no zone to be created unless create_zone was explicitly requested")
    assert not Domain.objects.filter(name=ADOPT_DOMAIN).exists(), (
        "A refused adoption must create nothing")

    with patch(f"{R53}.find_zone_id", return_value=None), \
            patch(f"{R53}.create_hosted_zone",
                  return_value=objict(zone_id="ZFRESH", name=ADOPT_DOMAIN)) as create_zone:
        domain = onboarding.adopt_route53(None, None, ADOPT_DOMAIN, create_zone=True)

    assert create_zone.call_count == 1, (
        "Expected create_zone=True to create the missing hosted zone")
    assert domain.hosted_zone_id == "ZFRESH", (
        f"Expected the new zone id to be recorded, got {domain.hosted_zone_id}")
