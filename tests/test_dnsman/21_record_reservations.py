"""Durable ACME ownership of complete DNS record-set writes."""

from unittest import mock

from objict import objict
from testit import helpers as th

from tests.test_dnsman._helpers import make_certificate, make_domain


@th.django_unit_setup()
def setup_record_reservations(opts):
    from mojo.apps.dnsman.models import Domain, DnsRecordReservation

    DnsRecordReservation.objects.all().delete()
    Domain.objects.filter(name__startswith="reservation-").delete()
    opts.domain = make_domain(
        "reservation-example.com", provider="route53", status="active")
    opts.cert = make_certificate(opts.domain, status="pending")
    opts.other_cert = make_certificate(opts.domain, status="pending")
    opts.name = "_acme-challenge.reservation-example.com"


@th.django_unit_test("one certificate exclusively owns a challenge record set")
def test_conflicting_certificate_is_refused(opts):
    from mojo.apps.dnsman.services import record_reservations
    from mojo import errors as me

    first = record_reservations.reserve(opts.cert, opts.name, ["digest-a"])
    raised = None
    try:
        record_reservations.reserve(opts.other_cert, opts.name, ["digest-b"])
    except me.ValueException as error:
        raised = error
    assert raised is not None, "a second certificate stole a live DNS reservation"
    first.refresh_from_db()
    assert first.record_values == ["digest-a"], \
        "the refused owner replaced the first certificate's complete TXT set"


@th.django_unit_test("interactive DNS writes cannot replace an ACME reservation")
def test_interactive_write_is_locked_out(opts):
    from mojo.apps.dnsman.services import dns, record_reservations
    from mojo import errors as me

    record_reservations.reserve(opts.cert, opts.name, ["digest-a"])
    adapter = mock.Mock()
    raised = None
    with mock.patch.object(dns, "get_adapter", return_value=adapter):
        try:
            dns.upsert_record(opts.domain, "TXT", opts.name, ["operator-value"])
        except me.ValueException as error:
            raised = error
    assert raised is not None, "an interactive write replaced an active ACME challenge"
    assert not adapter.upsert_record.called, \
        "the provider was called before durable reservation ownership was checked"


@th.django_unit_test("an ambiguous provider timeout reconciles from exact inventory")
def test_ambiguous_upsert_reconciles(opts):
    from mojo.apps.dnsman.services import dns, record_reservations

    reservation = record_reservations.reserve(opts.cert, opts.name, ["digest-a"])
    adapter = mock.Mock(name="route53")
    adapter.name = "route53"
    adapter.upsert_record.side_effect = TimeoutError("response lost")
    adapter.list_records.return_value = [objict(
        type="TXT", name=opts.name, record_values=["digest-a"])]
    with mock.patch.object(dns, "get_adapter", return_value=adapter):
        result = dns.upsert_record(
            opts.domain, "TXT", opts.name, ["digest-a"],
            reservation=reservation)
    reservation.refresh_from_db()
    assert result.change_id == "reconciled", \
        f"authoritative exact state did not reconcile the timeout: {result}"
    assert reservation.mutation_attempted and reservation.mutation_proven, \
        "ambiguous mutation intent/proof was not retained durably"


@th.django_unit_test("failed challenge cleanup remains durable and blocks reuse")
def test_cleanup_failure_stays_pending(opts):
    from mojo.apps.dnsman.models.dns_record_reservation import STATE_CLEANUP_PENDING
    from mojo.apps.dnsman.services import certs, record_reservations

    reservation = record_reservations.reserve(opts.cert, opts.name, ["digest-a"])
    with mock.patch(
            "mojo.apps.dnsman.services.dns.clear_record",
            side_effect=RuntimeError("provider unavailable")):
        certs.cleanup_challenges(
            opts.domain, [(reservation, opts.name, ["digest-a"])])
    reservation.refresh_from_db()
    assert reservation.state == STATE_CLEANUP_PENDING, \
        "failed cleanup released the record and lost retry ownership"
    assert "provider unavailable" in reservation.last_error, \
        "pending cleanup retained no actionable provider failure"


@th.django_unit_test("successful cleanup releases the exact reservation")
def test_cleanup_releases(opts):
    from mojo.apps.dnsman.models.dns_record_reservation import STATE_RELEASED
    from mojo.apps.dnsman.services import certs, record_reservations

    reservation = record_reservations.reserve(opts.cert, opts.name, ["digest-a"])
    with mock.patch("mojo.apps.dnsman.services.dns.clear_record") as clear:
        certs.cleanup_challenges(
            opts.domain, [(reservation, opts.name, ["digest-a"])])
    reservation.refresh_from_db()
    assert reservation.state == STATE_RELEASED, \
        "successful cleanup left a live reservation blocking operators"
    clear.assert_called_once_with(
        opts.domain, "TXT", opts.name, ["digest-a"], reservation=reservation)
