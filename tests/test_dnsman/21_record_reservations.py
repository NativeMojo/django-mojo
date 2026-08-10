"""Durable ACME ownership of complete DNS record-set writes."""

from concurrent.futures import ThreadPoolExecutor, wait
from importlib import import_module
from threading import Barrier, Event
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


@th.django_unit_test("concurrent no-row reservations have exactly one live owner")
def test_concurrent_no_row_reservation_is_serialized(opts):
    from django.db import close_old_connections
    from mojo import errors as me
    from mojo.apps.dnsman.models import DnsRecordReservation
    from mojo.apps.dnsman.services import record_reservations

    gate = Barrier(2)

    def reserve(certificate_id):
        from mojo.apps.dnsman.models import Certificate
        close_old_connections()
        certificate = Certificate.objects.get(pk=certificate_id)
        gate.wait(timeout=5)
        try:
            row = record_reservations.reserve(
                certificate, opts.name, [f"digest-{certificate_id}"])
            return ("ok", row.certificate_id)
        except Exception as error:
            return (type(error).__name__, None)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            reserve, [opts.cert.pk, opts.other_cert.pk]))
    assert sum(result[0] == "ok" for result in results) == 1, \
        f"the no-row race did not produce one durable owner: {results}"
    assert sum(result[0] == me.ValueException.__name__ for result in results) == 1, \
        f"the losing reservation did not receive the stable ownership conflict: {results}"
    live = DnsRecordReservation.objects.filter(
        domain=opts.domain, record_name=opts.name,
        state__in=("reserved", "cleanup_pending"))
    assert live.count() == 1, f"concurrent reservation created {live.count()} live rows"


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


@th.django_unit_test("interactive no-row mutation holds the stable zone lock through provider I/O")
def test_interactive_no_row_mutation_serializes_reservation(opts):
    from django.db import close_old_connections
    from mojo.apps.dnsman.services import dns, record_reservations

    provider_started = Event()
    release_provider = Event()
    reserve_started = Event()
    adapter = mock.Mock()
    adapter.name = "route53"
    record_name = f"_acme-challenge.no-row.{opts.domain.name}"

    def provider(*args, **kwargs):
        provider_started.set()
        assert release_provider.wait(5), "test did not release the provider call"
        return "change"

    adapter.upsert_record.side_effect = provider

    def interactive():
        from mojo.apps.dnsman.models import Domain
        close_old_connections()
        try:
            domain = Domain.objects.get(pk=opts.domain.pk)
            return dns.upsert_record(domain, "TXT", record_name, ["operator"])
        finally:
            close_old_connections()

    def reserve():
        from mojo.apps.dnsman.models import Certificate
        close_old_connections()
        reserve_started.set()
        try:
            certificate = Certificate.objects.get(pk=opts.cert.pk)
            return record_reservations.reserve(certificate, record_name, ["digest-a"])
        finally:
            close_old_connections()

    with mock.patch.object(dns, "get_adapter", return_value=adapter), \
            ThreadPoolExecutor(max_workers=2) as executor:
        interactive_future = executor.submit(interactive)
        assert provider_started.wait(5), "interactive mutation never reached its provider"
        reserve_future = executor.submit(reserve)
        assert reserve_started.wait(5), "concurrent reservation did not start"
        completed, _ = wait([reserve_future], timeout=0.25)
        assert not completed, \
            "reservation absence check escaped the interactive mutation's zone lock"
        release_provider.set()
        assert interactive_future.result(timeout=5).change_id == "change"
        reservation = reserve_future.result(timeout=5)
    assert reservation.certificate_id == opts.cert.pk, \
        "the serialized reservation did not become the next durable owner"


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
    assert reservation.last_error == "dns.cleanup:RuntimeError", \
        "pending cleanup did not retain bounded failure class/action metadata"


@th.django_unit_test("a failed ambiguous reconcile keeps committed mutation intent")
def test_ambiguous_upsert_missing_inventory_raises(opts):
    from mojo.apps.dnsman.services import dns, record_reservations

    reservation = record_reservations.reserve(opts.cert, opts.name, ["digest-a"])
    adapter = mock.Mock(name="route53")
    adapter.name = "route53"
    adapter.upsert_record.side_effect = TimeoutError("secret request detail")
    adapter.list_records.return_value = [objict(
        type="TXT", name=opts.name, record_values=["different-value"])]
    raised = None
    with mock.patch.object(dns, "get_adapter", return_value=adapter):
        try:
            dns.upsert_record(
                opts.domain, "TXT", opts.name, ["digest-a"],
                reservation=reservation)
        except TimeoutError as error:
            raised = error
    reservation.refresh_from_db()
    assert raised is not None, "a missing authoritative value was treated as success"
    assert reservation.mutation_attempted is True, \
        "provider ambiguity rolled back the durable pre-I/O mutation marker"
    assert reservation.mutation_proven is False, \
        "a mismatched authoritative record was incorrectly proven"


@th.django_unit_test("an absent authoritative record never proves ambiguous ACME mutation")
def test_ambiguous_upsert_absent_inventory_raises(opts):
    from mojo.apps.dnsman.services import dns, record_reservations

    name = f"_acme-challenge.absent.{opts.domain.name}"
    reservation = record_reservations.reserve(opts.cert, name, ["digest-a"])
    adapter = mock.Mock(name="route53")
    adapter.name = "route53"
    adapter.upsert_record.side_effect = TimeoutError("token=provider-secret")
    adapter.list_records.return_value = []
    raised = None
    with mock.patch.object(dns, "get_adapter", return_value=adapter):
        try:
            dns.upsert_record(
                opts.domain, "TXT", name, ["digest-a"], reservation=reservation)
        except TimeoutError as error:
            raised = error
    reservation.refresh_from_db()
    assert raised is not None, "empty authoritative inventory was treated as success"
    assert reservation.mutation_attempted and not reservation.mutation_proven, \
        "empty inventory lost intent or fabricated mutation proof"


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


@th.django_unit_test("migration 0004 follows 0003 and installs conditional live uniqueness")
def test_0004_migration_contract(opts):
    from django.db import connection

    migration = import_module("mojo.apps.dnsman.migrations.0004_dnsrecordreservation")
    assert migration.Migration.dependencies == [
        ("dnsman", "0003_alter_domain_provider_acmedelegation")], \
        f"0004 no longer has the explicit 0003 predecessor: {migration.Migration.dependencies}"
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(
            cursor, "dnsman_record_reservation")
    unique = constraints.get("dnsman_record_reservation_live_uniq")
    assert unique and unique.get("unique"), \
        f"the migrated table lacks conditional live-owner uniqueness: {constraints.keys()}"
