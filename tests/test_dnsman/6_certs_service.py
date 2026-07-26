"""Certificate service tests -- the DNS-01 dance, cleanup, and the sync broadcast.

Everything here runs in-process with three seams stubbed out, so no socket is
ever opened and no KMS key is ever needed:

* the ACME client (``certs._get_account`` hands back a scripted stand-in),
* ``services/dns.py`` (a recorder that captures every record write), and
* ``KSMSecrets._get_kms`` (an in-memory envelope that round-trips).

The KMS stub is not optional: ``Certificate`` and ``AcmeAccount`` extend
``KSMSecrets``, whose ``save_secrets()`` raises ``RuntimeError`` outright when
``KMS_KEY_ID`` is unset -- which it is, in the test environment.
"""
import contextlib
import json
from datetime import datetime, timedelta, timezone

from objict import objict
from testit import helpers as th


ORDER_URL = "https://acme.test/order/1"
FINALIZE_URL = "https://acme.test/order/1/finalize"
CERT_URL = "https://acme.test/cert/1"


# ----------------------------------------------------------------------
# stand-ins
# ----------------------------------------------------------------------

class FakeKMS(object):
    """In-memory stand-in for the KMS envelope helper.

    Encodes the binding context alongside the payload so a decrypt against the
    wrong row fails here exactly as it would against real KMS.
    """

    def encrypt_field(self, context, data):
        return json.dumps({"context": context, "data": dict(data)})

    def decrypt_dict_field(self, context, blob):
        parsed = json.loads(blob)
        if parsed.get("context") != context:
            raise ValueError("KMS context mismatch")
        return parsed["data"]


class FakeDns(object):
    """Recorder standing in for ``mojo.apps.dnsman.services.dns``.

    Mirrors that module's real signatures, including the ``record_values``
    argument name -- never ``values``, which on an objict resolves to the bound
    dict method instead of the data.
    """

    def __init__(self, propagated=True, seen=None, delete_error=None,
                 clear_error=None):
        self.upserts = []
        self.deletes = []
        self.clears = []
        self.waits = []
        self.propagated = propagated
        self.seen = seen
        self.delete_error = delete_error
        self.clear_error = clear_error

    def upsert_record(self, domain, rtype, name, record_values, ttl=300):
        self.upserts.append(objict(
            domain=domain.name, rtype=rtype, name=name,
            record_values=list(record_values), ttl=ttl))
        return objict(change_id="C-upsert", provider=domain.provider)

    def delete_record(self, domain, rtype, name, record_values=None):
        self.deletes.append(objict(
            domain=domain.name, rtype=rtype, name=name,
            record_values=list(record_values) if record_values else None))
        if self.delete_error:
            raise RuntimeError(self.delete_error)
        return objict(change_id="C-delete", provider=domain.provider)

    def clear_record(self, domain, rtype, name, record_values=None):
        self.clears.append(objict(
            domain=domain.name, rtype=rtype, name=name,
            record_values=list(record_values) if record_values else None))
        if self.clear_error:
            raise RuntimeError(self.clear_error)
        return objict(change_id="C-clear", provider=domain.provider)

    def wait_for_propagation(self, domain, rtype, name, record_values, timeout=None):
        self.waits.append(objict(
            domain=domain.name, rtype=rtype, name=name,
            record_values=list(record_values)))
        if self.propagated:
            return True, list(record_values)
        return False, list(self.seen or [])


class FakeJobs(object):
    """Captures every ``jobs.publish`` call the service makes."""

    def __init__(self):
        self.published = []

    def publish(self, func, payload=None, **kwargs):
        job_id = f"job-{len(self.published) + 1}"
        self.published.append(objict(
            func=func,
            payload=dict(payload or {}),
            channel=kwargs.get("channel", "default"),
            broadcast=kwargs.get("broadcast", False),
            job_id=job_id))
        return job_id


class FakeAcmeClient(object):
    """A scripted ACME client -- the protocol itself is tested in 5_acme_client.py.

    ``identifiers`` is one entry per authorization the CA would create. A
    wildcard order produces TWO authorizations naming the SAME identifier
    (the base domain), which is the case this file exists to pin down.
    """

    def __init__(self, identifiers, chain="", order_responses=None):
        self.identifiers = list(identifiers)
        self.chain = chain
        self.order_responses = list(order_responses or [
            objict(status="ready", url=ORDER_URL, finalize=FINALIZE_URL),
            objict(status="valid", url=ORDER_URL, certificate=CERT_URL),
        ])
        self.orders = []
        self.answered = []
        self.finalized = []
        self.downloads = []
        self.revocations = []
        self.polls = []

    # -- order / authorization ----------------------------------------

    def new_order(self, names):
        self.orders.append(list(names))
        return objict(
            status="pending",
            url=ORDER_URL,
            finalize=FINALIZE_URL,
            authorizations=[f"https://acme.test/authz/{i}"
                            for i in range(len(self.identifiers))])

    def get_authorization(self, url):
        index = int(url.rsplit("/", 1)[1])
        return objict(
            url=url,
            status="pending",
            identifier={"type": "dns", "value": self.identifiers[index]},
            challenges=[
                {"type": "http-01",
                 "url": f"https://acme.test/chall/{index}/http",
                 "token": f"http-tok-{index}"},
                {"type": "dns-01",
                 "url": f"https://acme.test/chall/{index}/dns",
                 "token": f"tok-{index}"},
            ])

    def dns01_challenge(self, authz):
        for challenge in authz.get("challenges") or []:
            if challenge.get("type") == "dns-01":
                return challenge["url"], challenge["token"]
        raise AssertionError("the fake authorization carried no dns-01 challenge")

    def key_authorization_digest(self, token):
        return f"digest-{token}"

    def answer_challenge(self, url):
        self.answered.append(url)
        return objict(status="processing", url=url)

    def poll_order(self, url, until=None, **kwargs):
        self.polls.append(objict(url=url, until=until))
        if len(self.order_responses) > 1:
            return self.order_responses.pop(0)
        return self.order_responses[0]

    # -- issuance ------------------------------------------------------

    def finalize(self, order, csr_der):
        self.finalized.append(objict(order=order.get("url"), csr=csr_der))
        return objict(status="processing", url=order.get("url"))

    def download_certificate(self, url):
        self.downloads.append(url)
        return self.chain

    def revoke_certificate(self, cert_der, reason=0):
        # No ``key`` parameter on purpose: revocation must be signed with the
        # account key (kid). A service that passed key= would TypeError here.
        self.revocations.append(objict(der=cert_der, reason=reason))
        return True


# ----------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------

def _reset_domain(name):
    """Delete anything a previous run left behind, then create the domain.

    The database is long-lived; setup cleans up before it creates.
    """
    from mojo.apps.dnsman.models import Certificate, Domain

    Certificate.objects.filter(domain__name=name).delete()
    Domain.objects.filter(name=name).delete()
    return Domain.objects.create(
        name=name,
        provider="route53",
        status="active",
        hosted_zone_id="Z-TEST",
        verified=True)


def _pending_certificate(domain, names):
    from mojo.apps.dnsman.models import Certificate

    cert = Certificate(
        domain=domain,
        common_name=names[0],
        sans=list(names),
        status="pending")
    cert.save()
    return cert


def _make_chain(names, days=90, serial=0xABC123):
    """Build a real leaf+issuer PEM chain so metadata parsing is exercised."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.now(timezone.utc).replace(microsecond=0)
    issuer_key = ec.generate_private_key(ec.SECP256R1())
    issuer_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "dnsman Test CA R1"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "dnsman tests")])
    issuer_cert = (x509.CertificateBuilder()
                   .subject_name(issuer_name)
                   .issuer_name(issuer_name)
                   .public_key(issuer_key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(days=1))
                   .not_valid_after(now + timedelta(days=3650))
                   .sign(issuer_key, hashes.SHA256()))

    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_cert = (x509.CertificateBuilder()
                 .subject_name(x509.Name([
                     x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
                 .issuer_name(issuer_name)
                 .public_key(leaf_key.public_key())
                 .serial_number(serial)
                 .not_valid_before(now)
                 .not_valid_after(now + timedelta(days=days))
                 .add_extension(
                     x509.SubjectAlternativeName([x509.DNSName(n) for n in names]),
                     critical=False)
                 .sign(issuer_key, hashes.SHA256()))

    leaf_pem = leaf_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    issuer_pem = issuer_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    return leaf_pem + issuer_pem, leaf_cert


@contextlib.contextmanager
def _issuance_env(client=None, dns_stub=None):
    """Patch the ACME client, services/dns.py and the KMS layer for one test."""
    from unittest import mock

    from mojo.apps import jobs as jobs_module
    from mojo.apps.dnsman.services import certs, dns
    from mojo.models import KSMSecrets

    dns_stub = dns_stub if dns_stub is not None else FakeDns()
    jobs_stub = FakeJobs()
    kms = FakeKMS()

    with mock.patch.object(KSMSecrets, "_get_kms", return_value=kms), \
            mock.patch.object(certs, "_get_account", return_value=(None, client)), \
            mock.patch.object(dns, "upsert_record", dns_stub.upsert_record), \
            mock.patch.object(dns, "delete_record", dns_stub.delete_record), \
            mock.patch.object(dns, "clear_record", dns_stub.clear_record), \
            mock.patch.object(dns, "wait_for_propagation", dns_stub.wait_for_propagation), \
            mock.patch.object(jobs_module, "publish", jobs_stub.publish):
        yield objict(dns=dns_stub, jobs=jobs_stub, acme=client, kms=kms)


def _challenge_name(domain):
    return f"_acme-challenge.{domain.name}"


# ----------------------------------------------------------------------
# tests
# ----------------------------------------------------------------------

@th.django_unit_test("dnsman certs: a wildcard and its apex share ONE challenge record carrying BOTH digests")
def test_certs_wildcard_and_apex_share_one_record(opts):
    from mojo.apps.dnsman.services import certs

    domain = _reset_domain("wildcard-certs.test")
    names = [domain.name, f"*.{domain.name}"]
    cert = _pending_certificate(domain, names)
    chain, _leaf = _make_chain(names)

    # The CA creates one authorization per name, and a wildcard's authorization
    # names the BASE domain -- so both land on _acme-challenge.<base>.
    client = FakeAcmeClient([domain.name, domain.name], chain=chain)

    with _issuance_env(client) as env:
        result = certs.issue(cert)

    assert result.ok, f"issuance should have succeeded, error was: {result.get('error')}"
    assert len(env.dns.upserts) == 1, (
        f"the wildcard and the apex share one record name, so exactly one upsert "
        f"is expected; got {len(env.dns.upserts)}: "
        f"{[u.name for u in env.dns.upserts]}")

    upsert = env.dns.upserts[0]
    assert upsert.name == _challenge_name(domain), (
        f"the challenge record should be {_challenge_name(domain)}, got {upsert.name}")
    assert upsert.rtype == "TXT", f"DNS-01 challenges are TXT records, got {upsert.rtype}"
    assert len(upsert.record_values) == 2, (
        f"both authorizations' digests must be published simultaneously; the "
        f"single upsert carried {len(upsert.record_values)}: {upsert.record_values}")
    assert set(upsert.record_values) == {"digest-tok-0", "digest-tok-1"}, (
        f"the record should carry each authorization's digest exactly once, "
        f"got {upsert.record_values}")
    assert len(client.answered) == 2, (
        f"each of the two challenges must be answered, got {len(client.answered)}")


@th.django_unit_test("dnsman certs: a successful issue stores material and cleans up the challenge record")
def test_certs_issue_success_stores_and_cleans_up(opts):
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import certs

    domain = _reset_domain("issue-certs.test")
    names = [domain.name, f"*.{domain.name}"]
    cert = _pending_certificate(domain, names)
    chain, leaf = _make_chain(names, days=90, serial=0xABC123)
    client = FakeAcmeClient([domain.name, domain.name], chain=chain)

    with _issuance_env(client) as env:
        result = certs.issue(cert)

        assert result.ok, f"issuance should have succeeded, error was: {result.get('error')}"

        stored = Certificate.objects.get(pk=cert.pk)
        assert stored.status == "active", (
            f"a completed issuance leaves the certificate active, got {stored.status}")
        assert stored.cert_pem and "BEGIN CERTIFICATE" in stored.cert_pem, (
            "the leaf certificate PEM should be stored on the row")
        assert "BEGIN CERTIFICATE" in (stored.chain_pem or ""), (
            "the issuer chain should be stored separately from the leaf")
        assert stored.cert_pem not in (stored.chain_pem or ""), (
            "the leaf must not be duplicated into the chain field")
        assert "BEGIN PRIVATE KEY" in (stored.private_key_pem or ""), (
            "the private key should round-trip through the KMS-backed secrets")
        assert stored.serial == "abc123", (
            f"the serial should come from the issued certificate, got {stored.serial}")
        assert "dnsman Test CA R1" in (stored.issuer or ""), (
            f"the issuer should come from the issued certificate, got {stored.issuer}")
        assert stored.not_after is not None, "not_after must be read off the certificate"

        expected_renew = stored.not_after - timedelta(days=certs.renew_days())
        assert stored.renew_after == expected_renew, (
            f"renew_after should be not_after minus DNSMAN_CERT_RENEW_DAYS "
            f"({certs.renew_days()}), expected {expected_renew}, got {stored.renew_after}")

    # Cleanup goes through clear_record, not delete_record: "delete this record"
    # is not something GoDaddy can do, and a raise inside the finally would have
    # left the challenge TXT live in the zone.
    assert len(env.dns.clears) == 1, (
        f"the challenge record must be retired on success; got "
        f"{len(env.dns.clears)} clear calls (deletes: {len(env.dns.deletes)})")
    cleared = env.dns.clears[0]
    assert cleared.name == _challenge_name(domain), (
        f"cleanup should retire {_challenge_name(domain)}, got {cleared.name}")
    assert set(cleared.record_values or []) == {"digest-tok-0", "digest-tok-1"}, (
        f"cleanup should name exactly the digests it planted, got {cleared.record_values}")


@th.django_unit_test("dnsman certs: a cleanup failure never fails an issuance that worked")
def test_certs_cleanup_failure_does_not_fail_issuance(opts):
    """Cleanup runs in a ``finally`` and swallows -- the cert is already issued."""
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import certs

    domain = _reset_domain("cleanupfail-certs.test")
    names = [domain.name]
    cert = _pending_certificate(domain, names)
    chain, _leaf = _make_chain(names)
    client = FakeAcmeClient([domain.name], chain=chain)

    dns_stub = FakeDns(clear_error="the provider refused the retirement")

    with _issuance_env(client, dns_stub=dns_stub) as env:
        result = certs.issue(cert)

    assert result.ok, (
        f"a cleanup failure must not fail an issuance that succeeded, error was: "
        f"{result.get('error')}")
    assert len(env.dns.clears) == 1, (
        f"cleanup should still have been attempted, got {len(env.dns.clears)} clear calls")

    stored = Certificate.objects.get(pk=cert.pk)
    assert stored.status == "active", (
        f"the certificate should stay active despite the cleanup failure, got {stored.status}")
    assert stored.last_error is None, (
        f"a cleanup failure must not be recorded as an issuance error, got {stored.last_error!r}")


@th.django_unit_test("dnsman certs: a propagation timeout fails cleanly and still cleans up")
def test_certs_propagation_timeout_fails_cleanly(opts):
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import certs

    domain = _reset_domain("timeout-certs.test")
    names = [domain.name, f"*.{domain.name}"]
    cert = _pending_certificate(domain, names)
    client = FakeAcmeClient([domain.name, domain.name], chain="")

    # Only one of the two digests ever showed up authoritatively.
    dns_stub = FakeDns(propagated=False, seen=["digest-tok-0"])

    with _issuance_env(client, dns_stub=dns_stub) as env:
        result = certs.issue(cert)

    assert not result.ok, "a propagation timeout must not report success"

    stored = Certificate.objects.get(pk=cert.pk)
    assert stored.status == "failed", (
        f"a propagation timeout leaves the certificate failed, got {stored.status}")
    assert stored.attempts == 1, (
        f"a failed attempt should be counted, got attempts={stored.attempts}")
    assert _challenge_name(domain) in (stored.last_error or ""), (
        f"last_error should name the record that never propagated, got: {stored.last_error}")
    assert "digest-tok-0" in (stored.last_error or ""), (
        f"last_error should report what was actually seen, got: {stored.last_error}")

    assert len(env.dns.clears) == 1, (
        f"challenge records must be cleaned up on failure too; got "
        f"{len(env.dns.clears)} clear calls")
    assert env.dns.clears[0].name == _challenge_name(domain), (
        f"the planted record should be the one retired, got {env.dns.clears[0].name}")
    assert not client.finalized, (
        "the CSR must never be submitted when the challenge never propagated")


@th.django_unit_test("dnsman certs: an invalid order records the CA's error instead of raising")
def test_certs_invalid_order_records_ca_error(opts):
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import certs

    domain = _reset_domain("invalid-certs.test")
    names = [domain.name]
    cert = _pending_certificate(domain, names)

    ca_detail = "No TXT record found at _acme-challenge.invalid-certs.test"
    client = FakeAcmeClient(
        [domain.name],
        order_responses=[objict(
            status="invalid",
            url=ORDER_URL,
            error={"type": "urn:ietf:params:acme:error:dns", "detail": ca_detail})])

    with _issuance_env(client) as env:
        result = certs.issue(cert)

    assert not result.ok, "an invalid order must not report success"

    stored = Certificate.objects.get(pk=cert.pk)
    assert stored.status == "failed", (
        f"an invalid order leaves the certificate failed, got {stored.status}")
    assert ca_detail in (stored.last_error or ""), (
        f"the CA's own error detail must be preserved verbatim, got: {stored.last_error}")
    assert "urn:ietf:params:acme:error:dns" in (stored.last_error or ""), (
        f"the CA's problem type should be preserved too, got: {stored.last_error}")
    assert not client.finalized, (
        "an invalid order must not be finalized")
    # Cleanup routes through clear_record (see cleanup_challenges) — a plain
    # delete is not something every provider can do. The point of the assertion
    # is unchanged: a FAILED order must still retire what it planted, or the
    # zone keeps a live challenge digest.
    assert len(env.dns.clears) == 1, (
        f"challenge records must be cleaned up after an invalid order; got "
        f"{len(env.dns.clears)} clear calls (deletes: {len(env.dns.deletes)})")


@th.django_unit_test("dnsman certs: the sync broadcast carries identifiers and no key material")
def test_certs_sync_broadcast_carries_no_material(opts):
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import certs

    domain = _reset_domain("broadcast-certs.test")
    names = [domain.name, f"*.{domain.name}"]
    cert = _pending_certificate(domain, names)
    chain, _leaf = _make_chain(names)
    client = FakeAcmeClient([domain.name, domain.name], chain=chain)

    with _issuance_env(client) as env:
        result = certs.issue(cert)
        assert result.ok, f"issuance should have succeeded, error was: {result.get('error')}"
        stored = Certificate.objects.get(pk=cert.pk)
        # Proves the omission below is deliberate: the key IS held, it simply
        # never travels on the channel.
        assert stored.private_key_pem, (
            "the private key should be stored, so an empty broadcast payload is "
            "a deliberate omission rather than an absent key")

    broadcasts = [p for p in env.jobs.published if p.broadcast]
    assert len(broadcasts) == 1, (
        f"exactly one cert-updated broadcast is expected, got {len(broadcasts)}")

    published = broadcasts[0]
    assert published.channel == certs.sync_channel(), (
        f"the broadcast should go to the configured sync channel "
        f"({certs.sync_channel()}), got {published.channel}")
    assert published.payload.get("certificate") == cert.pk, (
        f"the payload should identify the certificate, got {published.payload}")
    assert published.payload.get("domain") == domain.name, (
        f"the payload should name the domain, got {published.payload}")

    blob = json.dumps(published.payload)
    for forbidden in ("BEGIN", "PRIVATE KEY", "CERTIFICATE", "mojo_secrets"):
        assert forbidden not in blob, (
            f"the sync broadcast must carry no key or certificate material; "
            f"found '{forbidden}' in payload {published.payload}")
    for field in ("cert_pem", "chain_pem", "private_key_pem"):
        assert field not in published.payload, (
            f"'{field}' must never appear in the broadcast payload, got "
            f"{list(published.payload.keys())}")


@th.django_unit_test("dnsman certs: renew_due picks up only active certs past renew_after")
def test_certs_renew_due_selects_only_due(opts):
    from unittest import mock

    from mojo.apps import jobs as jobs_module
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import certs
    from mojo.helpers import dates

    domain = _reset_domain("renewal-certs.test")
    now = dates.utcnow()

    due = Certificate.objects.create(
        domain=domain, common_name="due.renewal-certs.test",
        sans=["due.renewal-certs.test"], status="active",
        not_after=now + timedelta(days=20), renew_after=now - timedelta(days=1))
    not_due = Certificate.objects.create(
        domain=domain, common_name="notdue.renewal-certs.test",
        sans=["notdue.renewal-certs.test"], status="active",
        not_after=now + timedelta(days=80), renew_after=now + timedelta(days=50))
    failed = Certificate.objects.create(
        domain=domain, common_name="failed.renewal-certs.test",
        sans=["failed.renewal-certs.test"], status="failed",
        not_after=now + timedelta(days=20), renew_after=now - timedelta(days=1))
    never = Certificate.objects.create(
        domain=domain, common_name="never.renewal-certs.test",
        sans=["never.renewal-certs.test"], status="active",
        not_after=None, renew_after=None)

    jobs_stub = FakeJobs()
    with mock.patch.object(jobs_module, "publish", jobs_stub.publish):
        result = certs.renew_due()

    assert result.certificates == [due.pk], (
        f"only the active certificate past renew_after should be queued; "
        f"expected [{due.pk}], got {result.certificates} "
        f"(not_due={not_due.pk}, failed={failed.pk}, never={never.pk})")
    assert result.count == 1, f"one certificate was due, got count={result.count}"
    assert len(jobs_stub.published) == 1, (
        f"one renewal job per due certificate; got {len(jobs_stub.published)}")
    assert jobs_stub.published[0].func == certs.RENEW_JOB, (
        f"the queued job should be the renewal handler, got {jobs_stub.published[0].func}")
    assert jobs_stub.published[0].payload == {"certificate": due.pk}, (
        f"the job payload should carry only the certificate id, got "
        f"{jobs_stub.published[0].payload}")


@th.django_unit_test("dnsman certs: request_certificate refuses a duplicate active cert outside its renewal window")
def test_certs_request_refuses_duplicate_active(opts):
    from unittest import mock

    from mojo import errors as me
    from mojo.apps import jobs as jobs_module
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import certs
    from mojo.helpers import dates

    domain = _reset_domain("duplicate-certs.test")
    now = dates.utcnow()
    names = [domain.name, f"*.{domain.name}"]

    active = Certificate.objects.create(
        domain=domain, common_name=domain.name, sans=list(names),
        status="active",
        not_after=now + timedelta(days=80),
        renew_after=now + timedelta(days=50))

    jobs_stub = FakeJobs()
    with mock.patch.object(jobs_module, "publish", jobs_stub.publish):
        refused = None
        try:
            certs.request_certificate(domain)
        except me.ValueException as err:
            refused = str(err)

        assert refused is not None, (
            "requesting a certificate already covered by an active, not-yet-due "
            "cert should be refused")
        assert not jobs_stub.published, (
            f"a refused request must not queue any job, got {jobs_stub.published}")
        assert Certificate.objects.filter(domain=domain).count() == 1, (
            "a refused request must not create a Certificate row")

        # Inside its renewal window the same request is legitimate again.
        active.renew_after = now - timedelta(days=1)
        active.save()
        fresh = certs.request_certificate(domain)

    assert fresh.status == "pending", (
        f"a fresh request starts pending, got {fresh.status}")
    assert fresh.sans == names, (
        f"names should default to the apex plus its wildcard, got {fresh.sans}")
    assert len(jobs_stub.published) == 1, (
        f"an accepted request queues exactly one issuance job, got "
        f"{len(jobs_stub.published)}")
    assert jobs_stub.published[0].func == certs.ISSUE_JOB, (
        f"the queued job should be the issuance handler, got "
        f"{jobs_stub.published[0].func}")


@th.django_unit_test("dnsman certs: revoke signs with the account key and marks the row revoked")
def test_certs_revoke(opts):
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import certs

    domain = _reset_domain("revoke-certs.test")
    names = [domain.name]
    chain, _leaf = _make_chain(names)
    leaf_pem, chain_pem = chain.split("-----END CERTIFICATE-----\n", 1)
    leaf_pem = leaf_pem + "-----END CERTIFICATE-----\n"

    cert = Certificate.objects.create(
        domain=domain, common_name=domain.name, sans=list(names),
        status="active", cert_pem=leaf_pem, chain_pem=chain_pem)

    client = FakeAcmeClient([domain.name], chain=chain)
    with _issuance_env(client):
        result = certs.revoke(cert)

    assert result.ok, "revocation should report success"
    assert len(client.revocations) == 1, (
        f"exactly one revocation should be sent, got {len(client.revocations)}")
    assert client.revocations[0].reason == 0, (
        f"the default revocation reason is 0 (unspecified), got "
        f"{client.revocations[0].reason}")
    assert Certificate.objects.get(pk=cert.pk).status == "revoked", (
        "a revoked certificate's row should be marked revoked, not deleted")
