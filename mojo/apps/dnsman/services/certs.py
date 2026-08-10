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

import uuid
from contextlib import contextmanager
from datetime import timedelta, timezone as dt_timezone

from django.db import connection, transaction

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
DEFAULT_RETRY_BASE_SECONDS = 3600
MAX_RETRY_SECONDS = 86400
DEFAULT_ISSUING_STALE_SECONDS = 1800
_ISSUE_LOCK_NAMESPACE = 1421

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


def issuing_stale_seconds():
    value = settings.get_static(
        "DNSMAN_CERT_ISSUING_STALE_SECONDS",
        DEFAULT_ISSUING_STALE_SECONDS,
        kind="int")
    return max(60, min(int(value), 86400))


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
    from mojo.apps.dnsman.models.certificate import (
        STATUS_ACTIVE, STATUS_ISSUING, STATUS_PENDING)

    names = normalize_names(domain, names)
    _require_delegated_profile(domain, names)
    job_func = ISSUE_JOB
    with transaction.atomic():
        # The Domain lock serializes two callers before either can create a
        # duplicate Certificate row for the same requested name set.
        type(domain).objects.select_for_update().get(pk=domain.pk)
        candidates = Certificate.objects.select_for_update().filter(
            domain=domain,
            status__in=(STATUS_PENDING, STATUS_ISSUING, STATUS_ACTIVE))
        cert = None
        for candidate in candidates:
            if sorted(candidate.sans or []) == sorted(names):
                cert = candidate
                break
        if cert is not None and cert.status == STATUS_ACTIVE:
            if not cert.renew_after or cert.renew_after > dates.utcnow():
                raise me.ValueException(
                    f"An active certificate ({cert.common_name}) already covers "
                    f"these names and is not due for renewal until {cert.renew_after}")
            job_func = RENEW_JOB
        elif cert is None:
            cert = Certificate.objects.create(
                domain=domain,
                common_name=names[0],
                sans=list(names),
                status=STATUS_PENDING)

    if cert.status != STATUS_ISSUING:
        publish_job(job_func, cert)
    return cert


def publish_job(func_path, certificate, channel="default", idempotency_marker=None):
    """Queue a certificate job. The payload carries an id and nothing else."""
    from mojo.apps import jobs
    if idempotency_marker is not None:
        idempotency_key = (
            f"dnsman-cert-recover-{certificate.pk}-{idempotency_marker}")
    elif func_path == ISSUE_JOB:
        idempotency_key = f"dnsman-cert-issue-{certificate.pk}"
    else:
        marker = int(certificate.renew_after.timestamp()) if certificate.renew_after else "due"
        idempotency_key = f"dnsman-cert-renew-{certificate.pk}-{marker}"
    return jobs.publish(
        func_path,
        payload={"certificate": certificate.pk},
        channel=channel,
        idempotency_key=idempotency_key)


def _require_delegated_profile(domain, names):
    """Delegated v1 is deliberately only apex plus wildcard."""
    from mojo.apps.dnsman.models.domain import PROVIDER_MOJO
    from mojo.apps.dnsman.services import delegation

    row = delegation.for_domain(domain)
    if row is None and domain.provider != PROVIDER_MOJO:
        return None
    expected = sorted([domain.name, f"*.{domain.name}"])
    if sorted(names) != expected:
        raise me.ValueException(
            "Delegated ACME v1 supports exactly the apex plus wildcard profile")
    if row is None:
        raise me.ValueException("This delegated ACME domain is not verified")
    return row


# ----------------------------------------------------------------------
# issuance
# ----------------------------------------------------------------------
@contextmanager
def _issue_lock(certificate_id):
    """Try to own one certificate across every ACME/network round trip."""
    if connection.vendor != "postgresql":
        yield True
        return
    lock_id = int(certificate_id) % 2147483647
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_try_advisory_lock(%s, %s)",
            [_ISSUE_LOCK_NAMESPACE, lock_id])
        acquired = bool(cursor.fetchone()[0])
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s, %s)",
                    [_ISSUE_LOCK_NAMESPACE, lock_id])


def has_still_valid_material(certificate, now=None):
    """Whether stored material remains safe to serve during a renewal."""
    now = now or dates.utcnow()
    return bool(
        certificate.not_after
        and certificate.not_after > now
        and certificate.cert_pem
        and certificate.mojo_secrets)

def issue(certificate):
    """
    Run the full DNS-01 issuance for a Certificate row.

    Never raises for an issuance failure: the outcome is durable state on the
    row (``status``, ``last_error``, ``attempts``) and the return value says
    which way it went. Callers that want a loud failure — the job handler,
    for one — inspect ``result.ok``.
    """
    from mojo.apps.dnsman.models import Certificate
    with _issue_lock(certificate.pk) as acquired:
        if not acquired:
            current = Certificate.objects.get(pk=certificate.pk)
            return objict(ok=True, certificate=current, skipped=True)
        return _issue_locked(certificate)


def _issue_locked(certificate):
    """Issue while the caller holds the certificate advisory lock."""
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.models.certificate import (
        STATUS_ACTIVE, STATUS_ISSUING, STATUS_PENDING)
    from mojo.apps.dnsman.models.acme_delegation import STATE_VERIFIED
    from mojo.apps.dnsman.services import delegation
    from mojo.apps.dnsman.services import dns
    from mojo.apps.dnsman.services import record_reservations
    from mojo.helpers import acme

    with transaction.atomic():
        claimed = Certificate.objects.select_for_update().select_related(
            "domain").get(pk=certificate.pk)
        if claimed.status == STATUS_ISSUING:
            stale_before = dates.utcnow() - timedelta(
                seconds=issuing_stale_seconds())
            if claimed.modified > stale_before:
                return objict(ok=True, certificate=claimed, skipped=True)
            renewing = has_still_valid_material(claimed)
            claimed.last_error = None
            claimed.save(update_fields=["last_error", "modified"])
        elif claimed.status not in (STATUS_PENDING, STATUS_ACTIVE):
            return objict(
                ok=False, certificate=claimed,
                error=f"Certificate is {claimed.status}, not issuable")
        else:
            renewing = claimed.status == STATUS_ACTIVE
            claimed.status = STATUS_ISSUING
            claimed.last_error = None
            claimed.save(update_fields=["status", "last_error", "modified"])

    certificate = claimed
    domain = certificate.domain
    try:
        record_reservations.cleanup_for_certificate(certificate)
    except Exception as err:
        return _record_issue_failure(certificate, err, renewing)
    names = list(certificate.sans or [certificate.common_name])
    delegated = delegation.for_domain(domain)
    if delegated is None and domain.provider == "mojo":
        return _record_issue_failure(
            certificate, me.ValueException("This delegated ACME domain is not verified"),
            renewing)
    if delegated is not None:
        if delegated.state != STATE_VERIFIED:
            return _record_issue_failure(
                certificate,
                me.ValueException("The verified ACME delegation is broken"),
                renewing)
        try:
            delegated = delegation.require_current_owner(delegated)
            _require_delegated_profile(domain, names)
            # Proof and any prior ambiguous cleanup happen before the CA order.
            delegation.prove_alias(delegated)
            _withdraw_pending_delegated_cleanup(delegated)
        except Exception as err:
            return _record_issue_failure(certificate, err, renewing)

    # (record_name, digests) actually written, so cleanup removes exactly what
    # issuance planted and nothing else.
    planted = []
    delegated_challenge_ref = None
    try:
        account, client = _get_account()
        if delegated is not None:
            delegated = delegation.require_current_owner(delegated)
        order = client.new_order(names)
        certificate.acme_order_url = order.get("url")
        certificate.save()

        challenges, answers = _collect_challenges(client, order)

        # One write per record name, carrying every digest that name needs.
        # A wildcard and its base share `_acme-challenge.example.com`; writing
        # them one at a time would have the second write replace the first and
        # exactly one of the two authorizations would fail.
        if delegated is not None:
            delegated_challenge_ref = _publish_delegated_challenges(
                delegated, challenges)
        else:
            for record_name, digests in challenges.items():
                reservation = record_reservations.reserve(
                    certificate, record_name, list(digests))
                # From here on the provider outcome may be ambiguous. Put the
                # durable owner in finally's cleanup list before making the
                # call so a lost response cannot strand an invisible write.
                planted.append((reservation, record_name, list(digests)))
                dns.upsert_record(
                    domain, "TXT", record_name, list(digests), ttl=CHALLENGE_TTL,
                    reservation=reservation)

            for record_name, digests in challenges.items():
                ok, seen = dns.wait_for_propagation(
                    domain, "TXT", record_name, list(digests))
                if not ok:
                    raise me.ValueException(
                        f"Timed out waiting for {record_name} to publish "
                        f"{len(digests)} challenge value(s); "
                        f"saw {list(seen or []) or 'nothing'}")
                reservation = next(
                    item[0] for item in planted if item[1] == record_name)
                record_reservations.mark_proven(reservation)

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
        return _record_issue_failure(certificate, err, renewing)
    finally:
        # Success or failure, the challenge records go away.
        if delegated is not None:
            # `_publish_delegated_challenges` stores the ref on `delegated`
            # before its remote call.  If that call times out, it cannot return
            # the ref, but finally still has the durable/object intent.
            cleanup_ref = (
                delegated_challenge_ref
                or delegated.cleanup_challenge_ref)
            if cleanup_ref:
                _withdraw_delegated_cleanup(delegated, cleanup_ref)
        else:
            cleanup_challenges(domain, planted)

    publish_sync(certificate)
    logit.info(
        f"dnsman: issued {certificate.common_name} "
        f"(expires {certificate.not_after}, renew after {certificate.renew_after})")
    return objict(ok=True, certificate=certificate)


def _retry_seconds(attempts):
    try:
        base = int(settings.get(
            "DNSMAN_CERT_RETRY_BASE_SECONDS", DEFAULT_RETRY_BASE_SECONDS))
    except (TypeError, ValueError):
        base = DEFAULT_RETRY_BASE_SECONDS
    base = min(max(base, 60), MAX_RETRY_SECONDS)
    return min(base * (2 ** max((attempts or 1) - 1, 0)), MAX_RETRY_SECONDS)


def _record_issue_failure(certificate, error, renewing):
    """Persist a failure without destroying a still-valid active certificate."""
    from mojo.apps.dnsman.models.certificate import STATUS_ACTIVE, STATUS_FAILED

    certificate.refresh_from_db()
    attempts = (certificate.attempts or 0) + 1
    still_valid = bool(renewing and has_still_valid_material(certificate))
    certificate.status = STATUS_ACTIVE if still_valid else STATUS_FAILED
    certificate.last_error = str(error)
    certificate.attempts = attempts
    if still_valid:
        certificate.renew_after = dates.utcnow() + timedelta(
            seconds=_retry_seconds(attempts))
    certificate.save(update_fields=[
        "status", "last_error", "attempts", "renew_after", "modified"])
    logit.error(
        f"dnsman: certificate {certificate.pk} ({certificate.common_name}) "
        f"issuance failed: {error}")
    return objict(ok=False, certificate=certificate, error=str(error))


def _publish_delegated_challenges(row, challenges):
    from mojo.apps.dnsman.models import AcmeDelegation
    from mojo.apps.dnsman.services import acme_hub_client, delegation
    from mojo.helpers.dns import probe

    if set(challenges) != {row.source_name}:
        raise me.ValueException(
            "Delegated ACME authorizations did not match the immutable source")
    digests = list(challenges[row.source_name])
    challenge_ref = uuid.uuid4().hex
    delegation.require_current_owner(row)

    # Commit cleanup intent before the ambiguous remote publish call.
    AcmeDelegation.objects.filter(pk=row.pk).update(
        cleanup_challenge_ref=challenge_ref,
        cleanup_requested_at=dates.utcnow(),
        modified=dates.utcnow())
    row.cleanup_challenge_ref = challenge_ref
    row.cleanup_requested_at = dates.utcnow()

    result = acme_hub_client.publish(
        str(row.client_ref), challenge_ref, digests)
    delegation.compare_challenge_result(row, result, challenge_ref)
    ok, seen = probe.wait_for_txt(row.target_name, digests)
    if not ok:
        raise me.ValueException(
            f"Timed out waiting for the delegated ACME target to publish "
            f"{len(digests)} challenge value(s); saw "
            f"{len(list(seen or []))} value(s)")
    return challenge_ref


def _withdraw_pending_delegated_cleanup(row):
    if row.cleanup_challenge_ref:
        _withdraw_delegated_cleanup(row, row.cleanup_challenge_ref, raise_errors=True)


def _withdraw_delegated_cleanup(row, challenge_ref, raise_errors=False):
    from mojo.apps.dnsman.models import AcmeDelegation
    from mojo.apps.dnsman.services import acme_hub_client, delegation

    try:
        row = delegation.require_current_owner(row)
        result = acme_hub_client.withdraw(str(row.client_ref), challenge_ref)
        delegation.compare_challenge_result(row, result, challenge_ref)
        AcmeDelegation.objects.filter(
            pk=row.pk, cleanup_challenge_ref=challenge_ref).update(
                cleanup_challenge_ref=None,
                cleanup_requested_at=None,
                modified=dates.utcnow())
        row.cleanup_challenge_ref = None
        row.cleanup_requested_at = None
    except Exception as err:
        logit.error(
            f"dnsman: could not withdraw delegated ACME challenge for "
            f"delegation {row.pk}: {err}")
        if raise_errors:
            raise


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
    Retire the challenge records issuance planted.

    Goes through ``dns.clear_record``, not ``dns.delete_record``, because "delete
    this record" is not something every provider can do. Route53 removes the
    values and that is the end of it. GoDaddy has no delete at all — its only
    write is a PUT that replaces a whole record set, and it rejects an empty
    replacement, so removing the LAST value of a set is impossible and
    ``delete_record`` says so by raising. Cleanup cannot act on that: it runs in
    a ``finally`` and swallows, so the raise just left the `_acme-challenge` TXT
    live in the customer's zone, collecting another stale digest on every
    renewal. ``clear_record`` lets the adapter pick the strongest retirement it
    has — a real delete, or an overwrite with a single inert placeholder.

    Still deliberately swallows its own errors: a cleanup problem must never
    replace the real reason issuance failed, nor fail an issuance that succeeded.
    """
    from mojo.apps.dnsman.services import dns, record_reservations

    for planted_record in planted:
        # The two-tuple form is the public helper's pre-reservation contract
        # and remains useful to provider-adapter callers/tests. Issuance itself
        # always supplies the durable three-tuple.
        if len(planted_record) == 3:
            reservation, record_name, digests = planted_record
        else:
            reservation = None
            record_name, digests = planted_record
        try:
            kwargs = {"reservation": reservation} if reservation is not None else {}
            dns.clear_record(domain, "TXT", record_name, list(digests), **kwargs)
            if reservation is not None:
                record_reservations.release(reservation)
        except Exception as err:
            if reservation is not None:
                record_reservations.mark_cleanup_pending(reservation, err)
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

    Returns ``objict(count, certificates, jobs)``. Jobs carry an idempotency
    key derived from the certificate and current renewal deadline, so
    overlapping scans converge on one queued job.
    """
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.models.certificate import STATUS_ACTIVE, STATUS_ISSUING

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

    # Worker death after the status claim leaves an issuing row behind.  The
    # grace period bounds recovery, and the try-lock refuses recovery while a
    # live worker still owns the certificate even if its ACME round trip runs
    # longer than that grace period.
    stale_before = now - timedelta(seconds=issuing_stale_seconds())
    stale = Certificate.objects.filter(
        status=STATUS_ISSUING, modified__lte=stale_before).select_related("domain")
    for cert in stale:
        with _issue_lock(cert.pk) as acquired:
            if not acquired:
                continue
            current = Certificate.objects.get(pk=cert.pk)
            if current.status != STATUS_ISSUING or current.modified > stale_before:
                continue
            marker = int(current.modified.timestamp())
            job_func = RENEW_JOB if has_still_valid_material(current, now) else ISSUE_JOB
        try:
            job_ids.append(publish_job(
                job_func, current, idempotency_marker=marker))
            certificates.append(current.pk)
        except Exception as err:
            logit.error(
                f"dnsman: could not requeue stale issuance for certificate "
                f"{current.pk}: {err}")
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
