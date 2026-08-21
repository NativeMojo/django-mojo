"""Split out of tests/test_dnsman/6_certs_service.py (maestro #2558).

Two kinds of coverage that cannot run under the parallel default tier:

* the real publish -> drain job-engine flow, whose drained handler calls
  ``certs.issue`` with no seams — it only sees the stubs via process-global
  patches of certs module attributes;
* tests that patch shared model/settings surfaces outright
  (``Certificate.rest_check_permission_or_raise``, the settings singleton as
  read through ``mojo.apps.edge.services.render``).

The scaffolding below mirrors the source module (which now injects its stubs
through the certs keyword seams); assertions are verbatim.
"""
import contextlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from objict import objict
from testit import helpers as th


ORDER_URL = "https://acme.test/order/1"
FINALIZE_URL = "https://acme.test/order/1/finalize"
CERT_URL = "https://acme.test/cert/1"
# Distinct from the default-tier module's channel: under --all both modules
# may run in one invocation, and a shared channel would cross-drain jobs.
CERT_JOB_CHANNEL = "testit_dnsman_cert_jobs_ext"


# ----------------------------------------------------------------------
# stand-ins (copied from the source module)
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

    def upsert_record(self, domain, rtype, name, record_values, ttl=300,
                      reservation=None):
        self.upserts.append(objict(
            domain=domain.name, rtype=rtype, name=name,
            record_values=list(record_values), ttl=ttl))
        return objict(change_id="C-upsert", provider=domain.provider)

    def delete_record(self, domain, rtype, name, record_values=None,
                      reservation=None):
        self.deletes.append(objict(
            domain=domain.name, rtype=rtype, name=name,
            record_values=list(record_values) if record_values else None))
        if self.delete_error:
            raise RuntimeError(self.delete_error)
        return objict(change_id="C-delete", provider=domain.provider)

    def clear_record(self, domain, rtype, name, record_values=None,
                     reservation=None):
        self.clears.append(objict(
            domain=domain.name, rtype=rtype, name=name,
            record_values=list(record_values) if record_values else None))
        if self.clear_error:
            raise RuntimeError(self.clear_error)
        return objict(change_id="C-clear", provider=domain.provider)

    def wait_for_propagation(self, domain, rtype, name, record_values, timeout=None,
                             change_id=None):
        self.waits.append(objict(
            domain=domain.name, rtype=rtype, name=name,
            record_values=list(record_values), change_id=change_id))
        if self.propagated:
            return True, list(record_values)
        return False, list(self.seen or [])


class FakeJobs(object):
    """Captures the dnsman publishes the certs service makes.

    Tests patch the cert service's local ``publish_job`` seam instead of the
    process-global ``jobs.publish`` function, so parallel modules retain their
    own queue behavior. Kept as a stub class because the tests read
    ``.published`` objicts.
    """

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

    def publish_certificate(self, func, certificate, channel="default",
                            idempotency_marker=None):
        return self.publish(
            func, payload={"certificate": certificate.pk}, channel=channel,
            idempotency_marker=idempotency_marker)

    def publish_sync(self, certificate):
        return self.publish(
            "mojo.apps.dnsman.asyncjobs.certificate_updated",
            payload={
                "certificate": certificate.pk,
                "domain": certificate.domain.name,
                "common_name": certificate.common_name,
                "not_after": (
                    certificate.not_after.isoformat()
                    if certificate.not_after else None),
            },
            channel="certs", broadcast=True)


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

    `real_jobs=True` forwards certificate work through the genuine publish path
    on a private test channel, so a Job row is written and queued without a
    parallel module clearing or draining it. Every patch below still applies to
    the handler, because the drain executes it in THIS process; that is the
    entire reason the drain exists rather than a job daemon.
    """
    from unittest import mock

    from mojo.apps.dnsman.services import certs, dns
    from mojo.models import KSMSecrets

    dns_stub = dns_stub if dns_stub is not None else FakeDns()
    jobs_stub = FakeJobs()
    kms = FakeKMS()

    real_publish_job = certs.publish_job

    def publish_certificate_job(func, certificate, channel="default",
                                idempotency_marker=None):
        return real_publish_job(
            func, certificate, channel=CERT_JOB_CHANNEL,
            idempotency_marker=idempotency_marker)

    patches = [
        mock.patch.object(KSMSecrets, "_get_kms", return_value=kms),
        mock.patch.object(certs, "_get_account", return_value=(None, client)),
        mock.patch.object(dns, "upsert_record", dns_stub.upsert_record),
        mock.patch.object(dns, "delete_record", dns_stub.delete_record),
        mock.patch.object(dns, "clear_record", dns_stub.clear_record),
        mock.patch.object(dns, "wait_for_propagation", dns_stub.wait_for_propagation),
        mock.patch.object(certs, "publish_sync", side_effect=jobs_stub.publish_sync),
    ]
    if real_jobs:
        patches.append(mock.patch.object(
            certs, "publish_job", side_effect=publish_certificate_job))
    else:
        patches.append(mock.patch.object(
            certs, "publish_job", side_effect=jobs_stub.publish_certificate))

    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        yield objict(dns=dns_stub, jobs=jobs_stub, acme=client, kms=kms)


def _challenge_name(domain):
    return f"_acme-challenge.{domain.name}"



# ----------------------------------------------------------------------
# tests
# ----------------------------------------------------------------------

@th.django_unit_test("dnsman certs: material remains available during renewal but not initial issuance")
def test_material_available_while_renewing(opts):
    """Moved from the default-tier module (item #2558): patching
    Certificate.rest_check_permission_or_raise is a process-global class-attr
    patch, unsafe under the parallel default tier."""
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



# ----------------------------------------------------------------------
# The real publish -> execute path
#
# The default-tier module calls certs.issue() directly, which is right for
# exercising issuance internals. These exercise the contract BETWEEN the
# service and the job engine — that a published job actually resolves to its
# handler, carries a payload the handler can use, and drives the Certificate
# row to its end state. The drained handler calls certs.issue with no seams,
# which is why this coverage needs the process-global patches above.
# ----------------------------------------------------------------------

@th.django_unit_test("dnsman certs: request_certificate -> job -> issued, through the real queue")
def test_certs_request_runs_through_the_job_engine(opts):
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import certs

    th.clear_jobs(channel=CERT_JOB_CHANNEL)
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
        assert th.pending_job_count(channel=CERT_JOB_CHANNEL) >= 1, \
            "request_certificate should have left a real job on the queue"

        drained = th.run_jobs(channel=CERT_JOB_CHANNEL)

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

    th.clear_jobs(channel=CERT_JOB_CHANNEL)
    domain = _reset_domain("payload-certs.test")
    names = [domain.name, f"*.{domain.name}"]
    chain, _leaf = _make_chain(names, days=90, serial=0xB0B2)
    client = FakeAcmeClient([domain.name, domain.name], chain=chain)

    with _issuance_env(client=client, real_jobs=True):
        cert = certs.request_certificate(domain)

        job = Job.objects.filter(
            func=certs.ISSUE_JOB, channel=CERT_JOB_CHANNEL).order_by("-created").first()
        assert job is not None, "request_certificate should have written a real Job row"
        # The payload survives a JSON round-trip through the queue; a stubbed
        # publish would have handed the handler the original Python object and
        # hidden any serialization problem.
        assert job.payload == {"certificate": cert.pk}, \
            f"the job payload should carry the certificate id, got {job.payload}"

        th.run_jobs(channel=CERT_JOB_CHANNEL)

    cert.refresh_from_db()
    assert cert.status == "active", f"the resolved handler should have issued, got {cert.status}"


@th.django_unit_test("dnsman certs: a failed issuance marks the certificate AND fails the job")
def test_certs_job_failure_surfaces_both_places(opts):
    from mojo.apps.jobs.models import Job
    from mojo.apps.dnsman.services import certs

    th.clear_jobs(channel=CERT_JOB_CHANNEL)
    domain = _reset_domain("jobfail-certs.test")
    # A propagation timeout is the realistic failure: the record never went live.
    dns_stub = FakeDns(propagated=False, seen=[])
    client = FakeAcmeClient([domain.name, domain.name], chain="")

    with _issuance_env(client=client, dns_stub=dns_stub, real_jobs=True):
        cert = certs.request_certificate(domain)
        th.run_jobs(channel=CERT_JOB_CHANNEL)

    cert.refresh_from_db()
    assert cert.status == "failed", \
        f"the certificate row is the durable record of failure, got {cert.status}"
    assert cert.last_error, "a failed issuance should record why"

    job = Job.objects.filter(
        func=certs.ISSUE_JOB, channel=CERT_JOB_CHANNEL).order_by("-created").first()
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

    th.clear_jobs(channel=CERT_JOB_CHANNEL)
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
        assert th.pending_job_count(channel=CERT_JOB_CHANNEL) >= 1, \
            "renew_due should have queued a real job"

        drained = th.run_jobs(channel=CERT_JOB_CHANNEL)

    assert drained.count >= 1, f"the renewal job should have run, drained {drained.count}"

    due.refresh_from_db()
    assert due.status == "active", f"a renewed certificate stays active, got {due.status}"
    assert due.cert_pem != "stale", \
        "renewal should have replaced the stored certificate material"
    assert due.renew_after > now, \
        f"renewal should push renew_after forward, got {due.renew_after}"


@th.django_unit_test("dnsman certs: issuance succeeds under the DNS-01-only edge posture")
def test_certs_issue_success_dns01_only_posture(opts):
    """The posture half of the source module's issue-success test (item
    #2558): with the edge HTTP listener disabled, a DNS-01 issuance still
    completes and never reads the edge ACME webroot."""
    from unittest import mock

    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.edge.services import render as edge_render

    domain = _reset_domain("issue-posture-certs.test")
    names = [domain.name, f"*.{domain.name}"]
    cert = _pending_certificate(domain, names)
    chain, leaf = _make_chain(names, days=90, serial=0xABC124)
    client = FakeAcmeClient([domain.name, domain.name], chain=chain)

    def static(name, default=None, kind=None):
        if name == "EDGE_HTTP_ENABLED":
            return False
        if name == "EDGE_ACME_WEBROOT":
            raise AssertionError("DNS-01 issuance read the edge ACME webroot")
        return default

    from mojo.apps.dnsman.services import certs

    with mock.patch.object(
            edge_render.settings, "get_static", side_effect=static), \
            _issuance_env(client) as env:
        assert edge_render.http_enabled() is False, \
            "the regression did not exercise the DNS-01-only edge posture"
        result = certs.issue(cert)

    assert result.ok, (
        f"issuance should have succeeded, error was: {result.get('error')}")
    stored = Certificate.objects.get(pk=cert.pk)
    assert stored.status == "active", (
        f"a completed issuance leaves the certificate active, got {stored.status}")
    assert len(env.dns.clears) == 1, (
        f"the challenge record must be retired on success; got "
        f"{len(env.dns.clears)} clear calls")
