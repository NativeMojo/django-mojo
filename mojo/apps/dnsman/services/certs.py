"""
Certificate issuance, renewal and revocation over ACME DNS-01.

This module is pure mechanism. **It performs no permission checks** — every
entry point is gated by the REST layer before it gets here, exactly like the
rest of the dnsman service surface. Do not add a permission check inside these
functions; do not call them from a request handler that has not already called
``rest_check_permission_or_raise``.

DNS-01 is the only challenge type dnsman uses, because it needs nothing but an
API call: no webserver, no port 80, no presence on the box that will serve the
certificate. That is what lets issuance and renewal run centrally on a worker
while serving hosts stay dumb consumers — they hear "a cert changed" on the
sync channel and pull the material themselves through the gated endpoint.

Two details in here are easy to get wrong and are called out where they live:

* A wildcard and its base domain produce **two separate authorizations that
  share one record name** (``_acme-challenge.example.com``), and both digests
  must be published *simultaneously*. Challenge digests are therefore grouped
  by record name and each name is written exactly once with its full value
  list.
* Challenge records are cleaned up in a ``finally``, on success and on
  failure alike. A left-behind ``_acme-challenge`` TXT is both a leak of what
  we were doing and a source of confusing future validations.

Issuance takes minutes (CA round trips plus a DNS propagation wait), so it is
driven from ``mojo.apps.dnsman.asyncjobs`` and never from a request.
"""

from datetime import timedelta, timezone as dt_timezone

from objict import objict

from mojo import errors as me
from mojo.helpers import dates, logit
from mojo.helpers.settings import settings


# The record label ACME requires for DNS-01, prefixed to each identifier.
CHALLENGE_PREFIX = "_acme-challenge"

# Challenge records live for minutes. A short TTL keeps a stale digest from
# outliving the validation it belongs to.
CHALLENGE_TTL = 60

DEFAULT_RENEW_DAYS = 30
DEFAULT_SYNC_CHANNEL = "certs"

ISSUE_JOB = "mojo.apps.dnsman.asyncjobs.issue_certificate"
RENEW_JOB = "mojo.apps.dnsman.asyncjobs.renew_certificate"
CERT_UPDATED_JOB = "mojo.apps.dnsman.asyncjobs.certificate_updated"


# ----------------------------------------------------------------------
# settings
# ----------------------------------------------------------------------

def directory_url():
    """
    The ACME directory to talk to.

    Defaults to Let's Encrypt **staging** on purpose: an unconfigured deploy
    that starts issuing must not be able to burn production rate limits.
    Going live is a deliberate settings change.
    """
    from mojo.helpers import acme
    return settings.get("DNSMAN_ACME_DIRECTORY_URL", acme.LETSENCRYPT_STAGING)


def renew_days():
    try:
        return int(settings.get("DNSMAN_CERT_RENEW_DAYS", DEFAULT_RENEW_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_RENEW_DAYS


def sync_channel():
    return settings.get("DNSMAN_CERT_SYNC_CHANNEL", DEFAULT_SYNC_CHANNEL)


# ----------------------------------------------------------------------
# ACME account
# ----------------------------------------------------------------------

def _get_account(url=None):
    """
    Load (or lazily create and register) the ACME account for a directory URL.

    Returns ``(account, client)``. One account row per directory URL, so
    staging and production never share a key. The account key is a P-256 key
    held in KMS-backed secrets and has no REST surface anywhere.
    """
    from mojo.apps.dnsman.models import AcmeAccount
    from mojo.helpers import acme

    url = url or directory_url()
    contact = settings.get("DNSMAN_ACME_CONTACT_EMAIL", None)

    account = AcmeAccount.objects.filter(directory_url=url).last()
    if account is None:
        account = AcmeAccount(
            directory_url=url, contact_email=contact, status="pending")
        account.save()

    key_pem = account.private_key_pem
    if key_pem:
        key = acme.load_key(key_pem)
    else:
        key = acme.generate_key()
        account.set_private_key_pem(acme.dump_key(key))
        account.save()

    client = acme.AcmeClient(
        url, key,
        contact_email=account.contact_email or contact,
        kid=account.account_url)

    if not account.account_url:
        try:
            account.account_url = client.new_account()
        except Exception as err:
            account.status = "failed"
            account.last_error = str(err)
            account.save()
            raise
        account.status = "active"
        account.last_error = None
        account.save()
    return account, client


# ----------------------------------------------------------------------
# requesting
# ----------------------------------------------------------------------

def normalize_names(domain, names=None):
    """
    Normalize and validate the names a certificate will cover.

    Defaults to the apex plus its wildcard, which is what almost every caller
    wants and is also the case that exercises the shared-record-name path.
    Every name must sit inside the domain's own zone — a certificate for a
    name we do not control is a request the CA will reject anyway, and
    refusing here makes the reason legible.
    """
    from mojo.apps.dnsman.services import naming

    base = naming.normalize_domain(domain.name)
    if not names:
        names = [base, f"*.{base}"]
    elif isinstance(names, str):
        names = [names]

    ordered = []
    for entry in names:
        value = (entry or "").strip().lower().rstrip(".")
        if not value:
            continue
        wildcard = value.startswith("*.")
        bare = value[2:] if wildcard else value
        if not bare or not naming.is_in_zone(bare, base):
            raise me.ValueException(
                f"'{entry}' is not inside {base} — a certificate may only cover "
                f"names in the domain's own zone")
        value = f"*.{bare}" if wildcard else bare
        if value not in ordered:
            ordered.append(value)

    if not ordered:
        raise me.ValueException("A certificate needs at least one name")
    return ordered


def find_covering_certificate(domain, names, now=None):
    """
    The active certificate covering exactly these names and not yet due for
    renewal, or None.

    "Exactly" is deliberate: a cert for the apex does not satisfy a request
    that also wants the wildcard, and issuing a narrower cert over a wider one
    would silently downgrade what the caller asked for.
    """
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.models.certificate import STATUS_ACTIVE

    now = now or dates.utcnow()
    wanted = sorted(names)
    for cert in Certificate.objects.filter(domain=domain, status=STATUS_ACTIVE):
        if sorted(cert.sans or []) != wanted:
            continue
        if cert.renew_after and cert.renew_after <= now:
            # Inside its renewal window: asking for a fresh one is legitimate.
            continue
        return cert
    return None


def request_certificate(domain, names=None):
    """
    Create a pending Certificate for a domain and queue its issuance.

    Returns the Certificate row. Issuance itself happens on the job runner —
    see ``asyncjobs.issue_certificate``.
    """
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.models.certificate import STATUS_PENDING

    names = normalize_names(domain, names)
    existing = find_covering_certificate(domain, names)
    if existing is not None:
        raise me.ValueException(
            f"An active certificate ({existing.common_name}) already covers "
            f"these names and is not due for renewal until {existing.renew_after}")

    cert = Certificate(
        domain=domain,
        common_name=names[0],
        sans=list(names),
        status=STATUS_PENDING)
    cert.save()
    publish_job(ISSUE_JOB, cert)
    return cert


def publish_job(func_path, certificate, channel="default"):
    """Queue a certificate job. The payload carries an id and nothing else."""
    from mojo.apps import jobs
    return jobs.publish(
        func_path,
        payload={"certificate": certificate.pk},
        channel=channel)


# ----------------------------------------------------------------------
# issuance
# ----------------------------------------------------------------------

def issue(certificate):
    """
    Run the full DNS-01 issuance for a Certificate row.

    Never raises for an issuance failure: the outcome is durable state on the
    row (``status``, ``last_error``, ``attempts``) and the return value says
    which way it went. Callers that want a loud failure — the job handler,
    for one — inspect ``result.ok``.
    """
    from mojo.apps.dnsman.models.certificate import (
        STATUS_ACTIVE, STATUS_FAILED, STATUS_ISSUING)
    from mojo.apps.dnsman.services import dns
    from mojo.helpers import acme

    domain = certificate.domain
    names = list(certificate.sans or [certificate.common_name])

    certificate.status = STATUS_ISSUING
    certificate.last_error = None
    certificate.save()

    # (record_name, digests) actually written, so cleanup removes exactly what
    # issuance planted and nothing else.
    planted = []
    try:
        account, client = _get_account()
        order = client.new_order(names)
        certificate.acme_order_url = order.get("url")
        certificate.save()

        challenges, answers = _collect_challenges(client, order)

        # One write per record name, carrying every digest that name needs.
        # A wildcard and its base share `_acme-challenge.example.com`; writing
        # them one at a time would have the second write replace the first and
        # exactly one of the two authorizations would fail.
        for record_name, digests in challenges.items():
            dns.upsert_record(
                domain, "TXT", record_name, list(digests), ttl=CHALLENGE_TTL)
            planted.append((record_name, list(digests)))

        for record_name, digests in challenges.items():
            ok, seen = dns.wait_for_propagation(
                domain, "TXT", record_name, list(digests))
            if not ok:
                raise me.ValueException(
                    f"Timed out waiting for {record_name} to publish "
                    f"{len(digests)} challenge value(s); "
                    f"saw {list(seen or []) or 'nothing'}")

        for challenge_url in answers:
            client.answer_challenge(challenge_url)

        # finalize needs `ready`, not `valid` — polling to `valid` here would
        # wait forever for a certificate no one has asked for yet.
        order = client.poll_order(order.get("url"), until=acme.ORDER_READY)
        if order.get("status") == "invalid":
            raise me.ValueException(
                f"The CA could not validate the order: {_order_error(order)}")

        key = acme.generate_key()
        client.finalize(order, acme.make_csr(key, names))

        order = client.poll_order(order.get("url"))
        if order.get("status") != "valid":
            raise me.ValueException(
                f"The CA did not issue the certificate: {_order_error(order)}")

        cert_url = order.get("certificate")
        if not cert_url:
            raise me.ValueException(
                "The CA reported a valid order with no certificate URL")

        cert_pem, chain_pem = _split_chain(client.download_certificate(cert_url))
        details = read_certificate(cert_pem)

        certificate.cert_pem = cert_pem
        certificate.chain_pem = chain_pem
        certificate.set_private_key_pem(acme.dump_key(key))
        certificate.issuer = (details.issuer or "")[:255] or None
        certificate.serial = details.serial
        certificate.not_before = details.not_before
        certificate.not_after = details.not_after
        certificate.renew_after = details.not_after - timedelta(days=renew_days())
        certificate.status = STATUS_ACTIVE
        certificate.last_error = None
        certificate.save()
    except Exception as err:
        certificate.status = STATUS_FAILED
        certificate.last_error = str(err)
        certificate.attempts = (certificate.attempts or 0) + 1
        certificate.save()
        logit.error(
            f"dnsman: certificate {certificate.pk} ({certificate.common_name}) "
            f"issuance failed: {err}")
        return objict(ok=False, certificate=certificate, error=str(err))
    finally:
        # Success or failure, the challenge records go away.
        cleanup_challenges(domain, planted)

    publish_sync(certificate)
    logit.info(
        f"dnsman: issued {certificate.common_name} "
        f"(expires {certificate.not_after}, renew after {certificate.renew_after})")
    return objict(ok=True, certificate=certificate)


def _collect_challenges(client, order):
    """
    Return ``(challenges, answers)`` for an order.

    ``challenges`` maps a challenge record name to the list of digests that
    must be published there **at the same time** — the grouping is the whole
    point, because a wildcard authorization and its base-domain authorization
    both name ``_acme-challenge.<base>``.
    """
    challenges = {}
    answers = []
    for authz_url in order.get("authorizations") or []:
        authz = client.get_authorization(authz_url)
        if authz.get("status") == "valid":
            # Already validated on a previous order; nothing to publish.
            continue
        identifier = (authz.get("identifier") or {}).get("value")
        if not identifier:
            raise me.ValueException(
                f"The ACME authorization at {authz_url} named no identifier")
        challenge_url, token = client.dns01_challenge(authz)
        digest = client.key_authorization_digest(token)
        record_name = f"{CHALLENGE_PREFIX}.{identifier}"
        digests = challenges.setdefault(record_name, [])
        if digest not in digests:
            digests.append(digest)
        answers.append(challenge_url)
    return challenges, answers


def cleanup_challenges(domain, planted):
    """
    Remove the challenge records issuance planted.

    Deliberately swallows its own errors: this runs in a ``finally``, and a
    cleanup problem must never replace the real reason issuance failed.
    """
    from mojo.apps.dnsman.services import dns

    for record_name, digests in planted:
        try:
            dns.delete_record(domain, "TXT", record_name, list(digests))
        except Exception as err:
            logit.error(
                f"dnsman: could not remove challenge record {record_name} "
                f"on {domain.name}: {err}")


def _order_error(order):
    """The CA's own words for why an order went bad, preserved verbatim."""
    error = order.get("error") or {}
    parts = []
    detail = error.get("detail")
    if detail:
        parts.append(f"{error.get('type') or 'error'}: {detail}")
    for sub in error.get("subproblems") or []:
        identifier = (sub.get("identifier") or {}).get("value") or ""
        parts.append(f"[{identifier}] {sub.get('type')}: {sub.get('detail')}")
    if not parts:
        return "the CA supplied no error detail"
    return " | ".join(parts)


def _split_chain(pem_text):
    """
    Split a PEM chain into ``(leaf, rest)``.

    The CA returns the leaf first followed by its issuers; we store them apart
    because a serving host needs them in different places.
    """
    marker = "-----END CERTIFICATE-----"
    blocks = []
    for part in (pem_text or "").split(marker):
        part = part.strip()
        if not part:
            continue
        blocks.append(f"{part}\n{marker}\n")
    if not blocks:
        raise me.ValueException("The CA returned no certificate material")
    return blocks[0], "".join(blocks[1:])


def read_certificate(cert_pem):
    """Read the metadata dnsman tracks out of a leaf certificate PEM."""
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(cert_pem.encode("utf-8"))
    if hasattr(cert, "not_valid_before_utc"):
        not_before = cert.not_valid_before_utc
        not_after = cert.not_valid_after_utc
    else:
        not_before = cert.not_valid_before.replace(tzinfo=dt_timezone.utc)
        not_after = cert.not_valid_after.replace(tzinfo=dt_timezone.utc)
    return objict(
        issuer=cert.issuer.rfc4514_string(),
        serial=format(cert.serial_number, "x"),
        not_before=not_before,
        not_after=not_after)


# ----------------------------------------------------------------------
# renewal
# ----------------------------------------------------------------------

def renew_due(now=None):
    """
    Queue a renewal job for every active certificate past its renew_after.

    Returns ``objict(count, certificates, jobs)``. Idempotent enough to run on
    a schedule: a certificate already being reissued moves off ``active`` and
    stops matching.
    """
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.models.certificate import STATUS_ACTIVE

    now = now or dates.utcnow()
    certificates = []
    job_ids = []
    qset = Certificate.objects.filter(
        status=STATUS_ACTIVE, renew_after__lte=now).select_related("domain")
    for cert in qset:
        try:
            job_ids.append(publish_job(RENEW_JOB, cert))
            certificates.append(cert.pk)
        except Exception as err:
            logit.error(
                f"dnsman: could not queue renewal for certificate {cert.pk}: {err}")
    return objict(count=len(certificates), certificates=certificates, jobs=job_ids)


# ----------------------------------------------------------------------
# revocation
# ----------------------------------------------------------------------

def revoke(certificate, reason=0):
    """
    Ask the CA to revoke a certificate and mark the row revoked.

    Signed with the account key (kid) — never with the certificate's own key,
    which would need the JWS to carry a jwk instead.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from mojo.apps.dnsman.models.certificate import STATUS_REVOKED

    if not certificate.cert_pem:
        raise me.ValueException("This certificate has no material to revoke")

    account, client = _get_account()
    leaf = x509.load_pem_x509_certificate(certificate.cert_pem.encode("utf-8"))
    client.revoke_certificate(
        leaf.public_bytes(serialization.Encoding.DER), reason=reason)

    certificate.status = STATUS_REVOKED
    certificate.save()
    logit.info(f"dnsman: revoked {certificate.common_name} (reason {reason})")
    return objict(ok=True, certificate=certificate)


# ----------------------------------------------------------------------
# consumer sync
# ----------------------------------------------------------------------

def publish_sync(certificate):
    """
    Tell every runner on the sync channel that a certificate changed.

    The payload carries **no key material** — not the private key, not even
    the public PEMs. A consumer hears "this cert changed" and pulls the
    material itself through the gated, access-logged material endpoint, so a
    broadcast can never spray key bytes into a queue or a log.

    Broadcast failures are logged, not raised: the certificate is already
    issued and stored, and losing the notification must not undo that.
    """
    from mojo.apps import jobs

    payload = {
        "certificate": certificate.pk,
        "domain": certificate.domain.name,
        "common_name": certificate.common_name,
        "not_after": certificate.not_after.isoformat() if certificate.not_after else None,
    }
    try:
        return jobs.publish(
            CERT_UPDATED_JOB,
            payload=payload,
            channel=sync_channel(),
            broadcast=True)
    except Exception as err:
        logit.error(
            f"dnsman: certificate {certificate.pk} issued but the sync "
            f"broadcast failed: {err}")
        return None
