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
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
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
def _issuance_env(client=None, dns_stub=None, real_jobs=False):
    """
    Patch the ACME client, services/dns.py and the KMS layer for one test.

    `real_jobs=True` leaves `jobs.publish` ALONE so the genuine publish path
    runs — a Job row is written and queued — and the test drives execution with
    `th.run_jobs()`. Every patch below still applies to the handler, because
    the drain executes it in THIS process; that is the entire reason the drain
    exists rather than a job daemon.
    """
    from unittest import mock

    from mojo.apps import jobs as jobs_module
    from mojo.apps.dnsman.services import certs, dns
    from mojo.models import KSMSecrets

    dns_stub = dns_stub if dns_stub is not None else FakeDns()
    jobs_stub = FakeJobs()
    kms = FakeKMS()

    patches = [
        mock.patch.object(KSMSecrets, "_get_kms", return_value=kms),
        mock.patch.object(certs, "_get_account", return_value=(None, client)),
        mock.patch.object(dns, "upsert_record", dns_stub.upsert_record),
        mock.patch.object(dns, "delete_record", dns_stub.delete_record),
        mock.patch.object(dns, "clear_record", dns_stub.clear_record),
        mock.patch.object(dns, "wait_for_propagation", dns_stub.wait_for_propagation),
    ]
    if not real_jobs:
        patches.append(mock.patch.object(jobs_module, "publish", jobs_stub.publish))

    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        yield objict(dns=dns_stub, jobs=jobs_stub, acme=client, kms=kms)


def _challenge_name(domain):
    return f"_acme-challenge.{domain.name}"


def _delegated_row(domain):
    from mojo.apps.account.models import Group
    from mojo.apps.dnsman.models import AcmeDelegation
    from mojo.helpers import dates

    group = Group.objects.create(
        name=f"delegated_certs_{uuid.uuid4().hex[:8]}", kind="organization")
    domain.group = group
    domain.save(update_fields=["group", "modified"])
    return AcmeDelegation.objects.create(
        domain=domain,
        group=group,
        tenant_uuid=group.get_uuid(),
        tenant_name=group.name,
        domain_name=domain.name,
        source_name=_challenge_name(domain),
        target_name=f"{uuid.uuid4().hex}.hub.example.net",
        state="verified",
        verified_at=dates.utcnow(),
    )


def _hub_result(row, challenge_ref, count=0):
    return objict(
        success=True,
        client_ref=str(row.client_ref),
        domain=row.domain_name,
        source=row.source_name,
        target=row.target_name,
        challenge_ref=challenge_ref,
        active_value_count=count,
    )


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


@th.django_unit_test("dnsman certs: pending delegation is inert and direct Route53 stays unchanged")
def test_pending_delegation_is_inert(opts):
    from unittest import mock

    from mojo.apps.dnsman.models import AcmeDelegation
    from mojo.apps.dnsman.services import acme_hub_client, certs

    domain = _reset_domain("pending-delegation-certs.test")
    row = _delegated_row(domain)
    AcmeDelegation.objects.filter(pk=row.pk).update(
        state="pending", verified_at=None)
    names = [domain.name, f"*.{domain.name}"]
    cert = _pending_certificate(domain, names)
    chain, unused = _make_chain(names)
    client = FakeAcmeClient([domain.name, domain.name], chain=chain)

    with _issuance_env(client) as env, \
            mock.patch.object(acme_hub_client, "publish") as remote_publish:
        result = certs.issue(cert)

    assert result.ok, f"pending delegation must not disrupt direct issuance: {result}"
    assert len(env.dns.upserts) == 1, \
        "pending delegation should leave the direct Route53 challenge writer selected"
    assert not remote_publish.called, \
        "pending delegation must never publish through the federation hub"


@th.django_unit_test("dnsman certs: verified delegation publishes both digests at the opaque target")
def test_delegated_challenge_writer(opts):
    from unittest import mock

    from mojo.apps.dnsman.services import acme_hub_client, certs, delegation
    from mojo.helpers.dns import probe

    domain = _reset_domain("delegated-writer-certs.test")
    row = _delegated_row(domain)
    names = [domain.name, f"*.{domain.name}"]
    cert = _pending_certificate(domain, names)
    chain, unused = _make_chain(names)
    client = FakeAcmeClient([domain.name, domain.name], chain=chain)
    publishes = []
    withdrawals = []

    def publish(client_ref, challenge_ref, values):
        publishes.append((client_ref, challenge_ref, list(values)))
        return _hub_result(row, challenge_ref, count=len(values))

    def withdraw(client_ref, challenge_ref):
        withdrawals.append((client_ref, challenge_ref))
        return _hub_result(row, challenge_ref)

    proof = objict(ok=True, error=None)
    with _issuance_env(client) as env, \
            mock.patch.object(delegation.probe, "verify_one_hop_cname", return_value=proof), \
            mock.patch.object(acme_hub_client, "publish", side_effect=publish), \
            mock.patch.object(acme_hub_client, "withdraw", side_effect=withdraw), \
            mock.patch.object(probe, "wait_for_txt", return_value=(True, ["seen"])) as wait:
        result = certs.issue(cert)

    assert result.ok, f"delegated issuance should succeed, got {result}"
    assert not env.dns.upserts and not env.dns.clears, \
        "verified delegation must never fall through to direct provider DNS"
    assert len(publishes) == 1 and set(publishes[0][2]) == {
        "digest-tok-0", "digest-tok-1"}, \
        f"both apex/wildcard digests must publish together, got {publishes}"
    assert wait.call_args.args[0] == row.target_name, \
        f"propagation must probe the exact opaque target, got {wait.call_args}"
    assert withdrawals == [(str(row.client_ref), publishes[0][1])], \
        f"the exact durable challenge must be withdrawn in finally, got {withdrawals}"
    row.refresh_from_db()
    assert row.cleanup_challenge_ref is None, \
        "successful idempotent withdrawal should clear durable cleanup intent"


@th.django_unit_test("dnsman certs: ambiguous delegated publish still withdraws in finally")
def test_delegated_ambiguous_publish_withdraws(opts):
    from unittest import mock

    from mojo.apps.dnsman.services import acme_hub_client, certs, delegation

    domain = _reset_domain("delegated-ambiguous-certs.test")
    row = _delegated_row(domain)
    names = [domain.name, f"*.{domain.name}"]
    cert = _pending_certificate(domain, names)
    client = FakeAcmeClient([domain.name, domain.name], chain="")
    withdrawals = []

    def withdraw(client_ref, challenge_ref):
        withdrawals.append((client_ref, challenge_ref))
        return _hub_result(row, challenge_ref)

    proof = objict(ok=True, error=None)
    with _issuance_env(client), \
            mock.patch.object(delegation.probe, "verify_one_hop_cname", return_value=proof), \
            mock.patch.object(
                acme_hub_client, "publish",
                side_effect=acme_hub_client.AcmeHubTransportError("ambiguous")), \
            mock.patch.object(acme_hub_client, "withdraw", side_effect=withdraw):
        result = certs.issue(cert)

    assert not result.ok, "an ambiguous remote publish must fail the issuance"
    assert len(withdrawals) == 1, \
        f"finally must withdraw even when publish returned no response, got {withdrawals}"
    row.refresh_from_db()
    assert row.cleanup_challenge_ref is None, \
        "successful cleanup after ambiguity should retire the durable intent"


@th.django_unit_test("dnsman certs: alias failure consumes no CA order and marks sticky routing broken")
def test_delegated_alias_failure_precedes_order(opts):
    from unittest import mock

    from mojo.apps.dnsman.services import certs, delegation

    domain = _reset_domain("delegated-alias-certs.test")
    row = _delegated_row(domain)
    names = [domain.name, f"*.{domain.name}"]
    cert = _pending_certificate(domain, names)
    client = FakeAcmeClient([domain.name, domain.name], chain="")
    failure = objict(ok=False, error="CNAME delegation does not match the allocation")
    with _issuance_env(client), \
            mock.patch.object(delegation.probe, "verify_one_hop_cname", return_value=failure):
        result = certs.issue(cert)

    assert not result.ok and not client.orders, \
        "a missing/changed alias must fail before creating any CA order"
    row.refresh_from_db()
    assert row.state == "broken", \
        "verified delegation must fail closed as broken with no direct fallback"


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


@th.django_unit_test("dnsman certs: production renewal reuses the row while replacing PEM and serial")
def test_certs_renewal_reuses_row_and_revises_material(opts):
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import certs
    from mojo.helpers import dates

    domain = _reset_domain("direct-renewal-certs.test")
    names = [domain.name]
    old_pem, _old_leaf = _make_chain(names, days=20, serial=0xA11CE)
    new_chain, _new_leaf = _make_chain(names, days=90, serial=0xB22CE)
    now = dates.utcnow()
    certificate = Certificate.objects.create(
        domain=domain, common_name=domain.name, sans=names, status="active",
        cert_pem=old_pem, serial="a11ce", not_after=now + timedelta(days=20),
        renew_after=now - timedelta(days=1))
    original_pk = certificate.pk
    original_pem = certificate.cert_pem
    original_serial = certificate.serial
    client = FakeAcmeClient([domain.name], chain=new_chain)

    with _issuance_env(client):
        certificate.set_private_key_pem("OLD PRIVATE KEY")
        certificate.save()
        result = certs.issue(certificate)

    assert result.ok, f"renewal should have succeeded, error was: {result.get('error')}"
    renewed = Certificate.objects.get(pk=original_pk)
    assert renewed.pk == original_pk, (
        f"renewal replaced the Certificate row instead of reusing pk {original_pk}")
    assert renewed.cert_pem != original_pem, (
        "renewal left the old certificate PEM on the reused row")
    assert renewed.serial != original_serial, (
        f"renewal left the old non-null serial {original_serial!r} on the reused row")
    assert renewed.serial == "b22ce", (
        f"renewal did not persist the new leaf serial, got {renewed.serial!r}")


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


@th.django_unit_test("dnsman certs: failed delegated renewal preserves valid material with bounded retry")
def test_delegated_renewal_failure_preserves_material(opts):
    from unittest import mock

    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import acme_hub_client, certs, delegation
    from mojo.helpers import dates

    domain = _reset_domain("delegated-renewal-certs.test")
    row = _delegated_row(domain)
    names = [domain.name, f"*.{domain.name}"]
    now = dates.utcnow()
    cert = Certificate.objects.create(
        domain=domain, common_name=domain.name, sans=names, status="active",
        cert_pem="OLD CERTIFICATE", chain_pem="OLD CHAIN",
        not_after=now + timedelta(days=20), renew_after=now - timedelta(days=1))
    client = FakeAcmeClient([domain.name, domain.name], chain="")

    def withdraw(client_ref, challenge_ref):
        return _hub_result(row, challenge_ref)

    proof = objict(ok=True, error=None)
    with _issuance_env(client), \
            mock.patch.object(delegation.probe, "verify_one_hop_cname", return_value=proof), \
            mock.patch.object(
                acme_hub_client, "publish",
                side_effect=acme_hub_client.AcmeHubTransportError("ambiguous")), \
            mock.patch.object(acme_hub_client, "withdraw", side_effect=withdraw):
        cert.set_private_key_pem("OLD PRIVATE KEY")
        cert.save()
        result = certs.issue(cert)
        stored = Certificate.objects.get(pk=cert.pk)
        private_key = stored.private_key_pem

    assert not result.ok, "the delegated hub failure must remain visible"
    assert stored.status == "active", \
        f"still-valid renewal material must remain active, got {stored.status}"
    assert (stored.cert_pem, stored.chain_pem, private_key) == (
        "OLD CERTIFICATE", "OLD CHAIN", "OLD PRIVATE KEY"), \
        "a failed renewal must not overwrite certificate custody material"
    assert stored.renew_after > now, \
        f"failure must schedule a later retry, got {stored.renew_after}"
    assert stored.renew_after <= now + timedelta(seconds=certs.MAX_RETRY_SECONDS + 5), \
        f"retry backoff must stay bounded, got {stored.renew_after}"


@th.django_unit_test("dnsman certs: material remains available during renewal but not initial issuance")
def test_material_available_while_renewing(opts):
    from unittest import mock

    from mojo.apps.account.models import Group
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.rest import certificate as certificate_rest
    from mojo.apps.dnsman.services import certs
    from mojo.helpers import dates

    domain = _reset_domain("material-renewing-certs.test")
    group = Group.objects.create(
        name=f"material_renewing_{uuid.uuid4().hex[:8]}", kind="organization")
    domain.group = group
    domain.save(update_fields=["group", "modified"])
    now = dates.utcnow()
    renewing = Certificate.objects.create(
        domain=domain, common_name=domain.name, sans=[domain.name],
        status="active", cert_pem="OLD CERT", chain_pem="OLD CHAIN",
        not_after=now + timedelta(days=10), renew_after=now - timedelta(days=1))
    initial = Certificate.objects.create(
        domain=domain, common_name=f"initial.{domain.name}",
        sans=[f"initial.{domain.name}"], status="issuing")
    request = objict(user=objict(pk=123), ip="127.0.0.1")

    with _issuance_env(FakeAcmeClient([])), \
            mock.patch.object(Certificate, "rest_check_permission_or_raise"):
        renewing.set_private_key_pem("OLD PRIVATE KEY")
        renewing.save()
        Certificate.objects.filter(pk=renewing.pk).update(status="issuing")
        payload = certificate_rest.on_certificate_material(request, pk=renewing.pk)
        assert payload["private_key_pem"] == "OLD PRIVATE KEY", \
            "still-valid stored material must remain consumable during renewal"
        try:
            certificate_rest.on_certificate_material(request, pk=initial.pk)
        except Exception as error:
            assert "not active" in str(error), \
                f"initial issuance should expose no material, got {error}"
        else:
            assert False, "initial issuance must never expose material"


@th.django_unit_test("dnsman certs: stale issuing workers are requeued and safely reclaimed")
def test_stale_issuing_recovery(opts):
    from unittest import mock

    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import certs
    from mojo.helpers import dates

    domain = _reset_domain("stale-issuing-certs.test")
    now = dates.utcnow()
    Certificate.objects.filter(
        status="active", renew_after__lte=now).update(
            renew_after=now + timedelta(days=1))
    stale_renewal = Certificate.objects.create(
        domain=domain, common_name=domain.name, sans=[domain.name],
        status="active", cert_pem="OLD CERT", chain_pem="OLD CHAIN",
        not_after=now + timedelta(days=10), renew_after=now - timedelta(days=1))
    stale_initial = Certificate.objects.create(
        domain=domain, common_name=f"initial.{domain.name}",
        sans=[f"initial.{domain.name}"], status="issuing")
    fresh = Certificate.objects.create(
        domain=domain, common_name=f"fresh.{domain.name}",
        sans=[f"fresh.{domain.name}"], status="issuing")
    old_modified = now - timedelta(seconds=certs.issuing_stale_seconds() + 5)
    chain, unused = _make_chain([domain.name])
    client = FakeAcmeClient([domain.name], chain=chain)

    with _issuance_env(client) as env:
        stale_renewal.set_private_key_pem("OLD PRIVATE KEY")
        stale_renewal.save()
        Certificate.objects.filter(pk=stale_renewal.pk).update(
            status="issuing", modified=old_modified)
        Certificate.objects.filter(pk=stale_initial.pk).update(modified=old_modified)

        queued = certs.renew_due(now=now)
        duplicate = certs.issue(Certificate.objects.get(pk=fresh.pk))
        reclaimed = certs.issue(Certificate.objects.get(pk=stale_renewal.pk))

    assert set(queued.certificates) == {stale_renewal.pk, stale_initial.pk}, \
        f"only stale issuing rows should be requeued, got {queued.certificates}"
    jobs_by_pk = {
        published.payload["certificate"]: published.func
        for published in env.jobs.published if not published.broadcast}
    assert jobs_by_pk[stale_renewal.pk] == certs.RENEW_JOB, \
        "stale valid material should requeue the renewal handler"
    assert jobs_by_pk[stale_initial.pk] == certs.ISSUE_JOB, \
        "stale initial issuance should requeue the issuance handler"
    assert duplicate.ok and duplicate.skipped and not duplicate.get("error"), \
        f"a fresh issuing duplicate should stay inert, got {duplicate}"
    assert reclaimed.ok and len(client.orders) == 1, \
        f"a stale dead worker should be reclaimed exactly once, got {reclaimed}"
    Certificate.objects.filter(pk__in=[stale_initial.pk, fresh.pk]).delete()


@th.django_unit_test("dnsman certs: concurrent issue jobs atomically admit only one worker")
def test_concurrent_issue_claim(opts):
    from unittest import mock

    from django.db import close_old_connections
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import certs

    domain = _reset_domain("concurrent-issue-certs.test")
    names = [domain.name]
    cert = _pending_certificate(domain, names)
    chain, unused = _make_chain(names)
    client = FakeAcmeClient([domain.name], chain=chain)
    started = threading.Event()
    release = threading.Event()

    def slow_account():
        started.set()
        assert release.wait(10), "test worker timed out waiting for duplicate claim"
        return None, client

    def first_issue():
        close_old_connections()
        try:
            return certs.issue(Certificate.objects.get(pk=cert.pk))
        finally:
            close_old_connections()

    with _issuance_env(client), \
            mock.patch.object(certs, "_get_account", side_effect=slow_account):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(first_issue)
            assert started.wait(10), "first worker never reached the ACME boundary"
            duplicate = certs.issue(Certificate.objects.get(pk=cert.pk))
            release.set()
            admitted = future.result(timeout=15)

    assert duplicate.ok and duplicate.skipped is True, \
        f"duplicate job should be an inert success, got {duplicate}"
    assert admitted.ok and len(client.orders) == 1, \
        f"exactly one worker may consume a CA order, got {len(client.orders)}"


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

        # Inside its renewal window the same request is a deduplicated renewal
        # of the existing material, not a second Certificate row.
        active.renew_after = now - timedelta(days=1)
        active.save()
        fresh = certs.request_certificate(domain)

    assert fresh.pk == active.pk and fresh.status == "active", (
        f"a due request should reuse the active certificate, got {fresh.pk}/{fresh.status}")
    assert fresh.sans == names, (
        f"names should default to the apex plus its wildcard, got {fresh.sans}")
    assert len(jobs_stub.published) == 1, (
        f"an accepted request queues exactly one issuance job, got "
        f"{len(jobs_stub.published)}")
    assert jobs_stub.published[0].func == certs.RENEW_JOB, (
        f"the queued job should be the renewal handler, got "
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


# ----------------------------------------------------------------------
# The real publish -> execute path
#
# The tests above call certs.issue() directly, which is right for exercising
# issuance internals. These exercise the contract BETWEEN the service and the
# job engine — that a published job actually resolves to its handler, carries
# a payload the handler can use, and drives the Certificate row to its end
# state. Nothing verified that before: the job layer was always stubbed out.
# ----------------------------------------------------------------------

@th.django_unit_test("dnsman certs: request_certificate -> job -> issued, through the real queue")
def test_certs_request_runs_through_the_job_engine(opts):
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import certs

    th.clear_jobs()
    domain = _reset_domain("jobflow-certs.test")
    names = [domain.name, f"*.{domain.name}"]
    chain, _leaf = _make_chain(names, days=90, serial=0xB0B1)
    # Two authorizations naming the SAME identifier — the wildcard case.
    client = FakeAcmeClient([domain.name, domain.name], chain=chain)

    with _issuance_env(client=client, real_jobs=True) as env:
        cert = certs.request_certificate(domain)

        # Nothing has been issued yet — request_certificate only queues.
        assert cert.status == "pending", \
            f"a freshly requested certificate should be pending, got {cert.status}"
        assert th.pending_job_count() >= 1, \
            "request_certificate should have left a real job on the queue"

        drained = th.run_jobs()

    assert drained.count >= 1, f"the issuance job should have run, drained {drained.count}"

    cert.refresh_from_db()
    assert cert.status == "active", (
        f"the job handler should have carried the certificate to active, got "
        f"{cert.status} (last_error={cert.last_error})")
    assert cert.cert_pem, "an issued certificate should have stored its PEM"
    assert cert.not_after is not None, "issuance should record the validity window"

    # The handler ran in-process, so the ACME and DNS stubs above applied to it.
    assert client.finalized, "the patched ACME client should have been driven by the handler"
    assert len(env.dns.clears) == 1, \
        f"the handler should have retired its challenge record, got {len(env.dns.clears)}"


@th.django_unit_test("dnsman certs: the job payload names the certificate the handler resolves")
def test_certs_job_payload_resolves(opts):
    from mojo.apps.jobs.models import Job
    from mojo.apps.dnsman.services import certs

    th.clear_jobs()
    domain = _reset_domain("payload-certs.test")
    names = [domain.name, f"*.{domain.name}"]
    chain, _leaf = _make_chain(names, days=90, serial=0xB0B2)
    client = FakeAcmeClient([domain.name, domain.name], chain=chain)

    with _issuance_env(client=client, real_jobs=True):
        cert = certs.request_certificate(domain)

        job = Job.objects.filter(func=certs.ISSUE_JOB).order_by("-created").first()
        assert job is not None, "request_certificate should have written a real Job row"
        # The payload survives a JSON round-trip through the queue; a stubbed
        # publish would have handed the handler the original Python object and
        # hidden any serialization problem.
        assert job.payload == {"certificate": cert.pk}, \
            f"the job payload should carry the certificate id, got {job.payload}"

        th.run_jobs()

    cert.refresh_from_db()
    assert cert.status == "active", f"the resolved handler should have issued, got {cert.status}"


@th.django_unit_test("dnsman certs: a failed issuance marks the certificate AND fails the job")
def test_certs_job_failure_surfaces_both_places(opts):
    from mojo.apps.jobs.models import Job
    from mojo.apps.dnsman.services import certs

    th.clear_jobs()
    domain = _reset_domain("jobfail-certs.test")
    # A propagation timeout is the realistic failure: the record never went live.
    dns_stub = FakeDns(propagated=False, seen=[])
    client = FakeAcmeClient([domain.name, domain.name], chain="")

    with _issuance_env(client=client, dns_stub=dns_stub, real_jobs=True):
        cert = certs.request_certificate(domain)
        th.run_jobs()

    cert.refresh_from_db()
    assert cert.status == "failed", \
        f"the certificate row is the durable record of failure, got {cert.status}"
    assert cert.last_error, "a failed issuance should record why"

    job = Job.objects.filter(func=certs.ISSUE_JOB).order_by("-created").first()
    assert job.status in ("failed", "retrying"), (
        "the handler re-raises after recording the row, so the failure is visible "
        f"in the jobs surface too; got {job.status}")


# extended: drives the real job engine end to end for a dnsman feature path.
# Valuable, but ~15s and not a framework contract -- the queue-side behaviour it
# depends on is covered by test_jobs, and dnsman is an optional app.
@th.requires_extra("extended")
@th.django_unit_test("dnsman certs: renew_due queues real jobs that reissue when drained")
def test_certs_renew_due_runs_through_the_job_engine(opts):
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import certs
    from mojo.helpers import dates

    th.clear_jobs()
    domain = _reset_domain("renewflow-certs.test")
    now = dates.utcnow()
    chain, _leaf = _make_chain([domain.name], days=90, serial=0xB0B3)
    client = FakeAcmeClient([domain.name], chain=chain)

    # renew_due() scans every certificate on the platform, and earlier tests in
    # this module leave due rows behind on the long-lived database. Clear them
    # so the drain handles exactly the one this test is about.
    Certificate.objects.filter(renew_after__lte=now).delete()

    due = Certificate.objects.create(
        domain=domain, common_name=domain.name, sans=[domain.name],
        status="active", cert_pem="stale",
        not_after=now + timedelta(days=20), renew_after=now - timedelta(days=1))

    with _issuance_env(client=client, real_jobs=True):
        result = certs.renew_due()
        assert result.certificates == [due.pk], (
            f"only the certificate past renew_after should be queued, got "
            f"{result.certificates}")
        assert th.pending_job_count() >= 1, "renew_due should have queued a real job"

        drained = th.run_jobs()

    assert drained.count >= 1, f"the renewal job should have run, drained {drained.count}"

    due.refresh_from_db()
    assert due.status == "active", f"a renewed certificate stays active, got {due.status}"
    assert due.cert_pem != "stale", \
        "renewal should have replaced the stored certificate material"
    assert due.renew_after > now, \
        f"renewal should push renew_after forward, got {due.renew_after}"
