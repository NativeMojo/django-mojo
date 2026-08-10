"""Exclusive, durable ownership for complete DNS record-set mutations."""

from contextlib import contextmanager

from django.db import IntegrityError, transaction

from mojo import errors as me
from mojo.helpers import dates

from mojo.apps.dnsman.models.dns_record_reservation import (
    LIVE_STATES, STATE_CLEANUP_PENDING, STATE_RELEASED, STATE_RESERVED)


def _values(values):
    return sorted({str(value) for value in (values or [])})


def reserve(certificate, record_name, record_values):
    """Reserve a TXT set before ACME makes a provider call.

    A retry by the same certificate adopts its own durable intent. Any other
    owner receives a stable conflict and consumes no provider mutation.
    """
    from mojo.apps.dnsman.models import DnsRecordReservation

    desired = _values(record_values)
    try:
        with transaction.atomic():
            current = DnsRecordReservation.objects.select_for_update().filter(
                domain=certificate.domain, record_type="TXT",
                record_name=record_name, state__in=LIVE_STATES).first()
            if current is not None:
                if current.certificate_id != certificate.pk:
                    raise me.ValueException(
                        f"{record_name} TXT is reserved by another certificate operation")
                current.record_values = desired
                current.state = STATE_RESERVED
                current.mutation_attempted = False
                current.mutation_proven = False
                current.last_error = None
                current.save(update_fields=[
                    "record_values", "state", "mutation_attempted",
                    "mutation_proven", "last_error", "modified"])
                return current
            return DnsRecordReservation.objects.create(
                domain=certificate.domain, certificate=certificate,
                record_type="TXT", record_name=record_name,
                record_values=desired)
    except IntegrityError:
        raise me.ValueException(
            f"{record_name} TXT is reserved by another certificate operation")


def _same_owner(row, reservation):
    return bool(
        row is not None and reservation is not None
        and row.pk == getattr(reservation, "pk", None)
        and row.owner_ref == getattr(reservation, "owner_ref", None))


@contextmanager
def mutation_guard(domain, record_type, record_name, reservation=None):
    """Serialize a complete-set provider mutation with ACME ownership."""
    from mojo.apps.dnsman.models import DnsRecordReservation

    with transaction.atomic():
        row = DnsRecordReservation.objects.select_for_update().filter(
            domain=domain, record_type=record_type, record_name=record_name,
            state__in=LIVE_STATES).first()
        if row is not None and not _same_owner(row, reservation):
            raise me.ValueException(
                f"{record_name} {record_type} is temporarily reserved for certificate validation")
        if reservation is not None and not _same_owner(row, reservation):
            raise me.ValueException("The DNS record reservation is no longer current")
        yield row


def mark_attempted(reservation):
    reservation.mutation_attempted = True
    reservation.last_error = None
    reservation.save(update_fields=["mutation_attempted", "last_error", "modified"])


def mark_proven(reservation):
    reservation.mutation_proven = True
    reservation.last_error = None
    reservation.save(update_fields=["mutation_proven", "last_error", "modified"])


def mark_cleanup_pending(reservation, error):
    reservation.state = STATE_CLEANUP_PENDING
    reservation.last_error = str(error)[:1000]
    reservation.save(update_fields=["state", "last_error", "modified"])


def release(reservation):
    reservation.state = STATE_RELEASED
    reservation.last_error = None
    reservation.save(update_fields=["state", "last_error", "modified"])


def reconcile(reservation, records):
    """Prove an ambiguous provider response from authoritative inventory."""
    expected = _values(reservation.record_values)
    for record in records or []:
        rtype = str(record.get("type") or "").upper()
        name = str(record.get("name") or "").lower().rstrip(".")
        actual = record.get("record_values")
        if actual is None:
            actual = record.get("values")
        if rtype == reservation.record_type and name == reservation.record_name:
            if _values(actual) == expected:
                mark_proven(reservation)
                return True
            return False
    return False


def cleanup_pending():
    from mojo.apps.dnsman.models import DnsRecordReservation
    return DnsRecordReservation.objects.filter(
        state=STATE_CLEANUP_PENDING).order_by("created")


def cleanup_for_certificate(certificate):
    """Retry durable cleanup before this certificate starts another order."""
    from mojo.apps.dnsman.services import dns

    for reservation in cleanup_pending().filter(certificate=certificate):
        try:
            dns.clear_record(
                reservation.domain, reservation.record_type,
                reservation.record_name, reservation.record_values,
                reservation=reservation)
            release(reservation)
        except Exception as error:
            mark_cleanup_pending(reservation, error)
            raise me.ValueException(
                "A prior certificate DNS challenge is still pending cleanup")
    return True
