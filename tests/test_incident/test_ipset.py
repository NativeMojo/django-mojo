"""Central IPSet validation and durable lifecycle regressions."""

import datetime
from unittest import mock

from testit import helpers as th


def _remove(name):
    from mojo.apps.incident.models import IPSet
    IPSet.objects.filter(name=name).delete()


@th.django_unit_test("new sets are disabled and canonicalized atomically")
def test_create_disabled_and_canonical(opts):
    from mojo.apps.incident.models import IPSet

    _remove("country_cn")
    row = IPSet.objects.create(
        name="country_cn", kind="country", source="manual",
        data="10.1.2.3/8\n192.0.2.8\n10.0.0.0/8")
    assert row.is_enabled is False, "creation bypassed the disabled lifecycle"
    assert row.cidrs == ["10.0.0.0/8", "192.0.2.8/32"], \
        f"CIDRs were not canonicalized/deduplicated/sorted: {row.cidrs!r}"


@th.django_unit_test("invalid or IPv6 data is all-or-nothing")
def test_ipv6_data_refused_without_partial_save(opts):
    from mojo.apps.incident.models import IPSet
    from mojo import errors as merrors

    _remove("ipv4_only")
    row = IPSet.objects.create(
        name="ipv4_only", kind="custom", source="manual",
        data="192.0.2.0/24")
    row.data = "198.51.100.0/24\n2001:db8::/32"
    with th.assert_raises(merrors.ValueException):
        row.save(update_fields=["data"])
    row.refresh_from_db()
    assert row.data == "192.0.2.0/24", \
        "failed validation partially replaced the durable CIDRs"


@th.django_unit_test("set names are immutable and reserved namespaces are closed")
def test_name_lifecycle(opts):
    from mojo.apps.incident.models import IPSet
    from mojo import errors as merrors

    _remove("immutable_set")
    row = IPSet.objects.create(
        name="immutable_set", kind="custom", source="manual")
    row.name = "renamed_set"
    with th.assert_raises(merrors.ValueException):
        row.save(update_fields=["name"])
    with th.assert_raises(merrors.ValueException):
        IPSet.objects.create(name="mojo_blocked", kind="custom")
    with th.assert_raises(merrors.ValueException):
        IPSet.objects.create(name="mojo_operator", kind="custom")
    with th.assert_raises(merrors.ValueException):
        IPSet.objects.create(name="operator_tmp", kind="custom")
    with th.assert_raises(merrors.ValueException):
        IPSet.objects.create(name="x" * 28, kind="custom")


@th.django_unit_test("configured permanent set name is dynamically reserved")
def test_configured_permanent_name_collision(opts):
    from mojo.apps.incident.models import IPSet
    from mojo import errors as merrors

    _remove("configured_reserved")
    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.permanent_set_name",
            return_value="configured_reserved"):
        with th.assert_raises(merrors.ValueException):
            IPSet.objects.create(name="configured_reserved", kind="custom")


@th.django_unit_test("generic state mutation and deletion are retired")
def test_state_and_delete_require_lifecycle(opts):
    from mojo.apps.incident.models import IPSet
    from mojo import errors as merrors

    _remove("lifecycle_set")
    row = IPSet.objects.create(
        name="lifecycle_set", kind="custom", source="manual")
    row.is_enabled = True
    with th.assert_raises(merrors.ValueException):
        row.save(update_fields=["is_enabled"])
    with th.assert_raises(merrors.ValueException):
        row.delete()
    assert IPSet.RestMeta.CAN_DELETE is False, \
        "generic REST delete still advertises support"
    assert "is_enabled" in IPSet.RestMeta.NO_SAVE_FIELDS, \
        "generic REST writes can still mutate lifecycle state"


@th.django_unit_test("checked enable persists dispatch and partial result")
def test_enable_partial_is_not_success(opts):
    from mojo.apps.incident.models import IPSet

    _remove("checked_set")
    row = IPSet.objects.create(
        name="checked_set", kind="custom", source="manual",
        data="192.0.2.0/24")
    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.reconcile_set",
            return_value={"status": "partial", "ok": False,
                          "error": {"code": "missing_host",
                                    "message": "host receipt missing"}}):
        result = row.enable()
    row.refresh_from_db()
    assert result["status"] == "partial" and row.is_enabled is True, \
        "desired enable state was not retained for eventual repair"
    assert row.last_synced is not None and "missing_host" in row.sync_error, \
        "dispatch time or bounded checked failure was not persisted"


@th.django_unit_test("stale IPSet instances are refused before dispatch")
def test_stale_sync_is_fenced_before_wait(opts):
    from mojo.apps.incident.models import IPSet

    _remove("stale_set")
    stale = IPSet.objects.create(
        name="stale_set", kind="custom", source="manual",
        data="192.0.2.0/24")
    IPSet.objects.filter(pk=stale.pk).update(description="newer")
    # A queryset update does not advance auto_now, so explicitly move the
    # durable revision to model a concurrent governed write.
    IPSet.objects.filter(pk=stale.pk).update(
        modified=stale.modified + datetime.timedelta(seconds=1))
    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.reconcile_set") as reconcile:
        result = stale.sync()
    assert result["status"] == "partial", result
    assert result["error"]["code"] == "generation_superseded", result
    reconcile.assert_not_called()


@th.django_unit_test("source refresh validation preserves the previous snapshot")
def test_source_refresh_is_atomic(opts):
    from mojo.apps.incident.models import IPSet

    _remove("source_set")
    row = IPSet.objects.create(
        name="source_set", kind="custom", source="tor",
        data="192.0.2.0/24")
    with mock.patch.object(
            row, "_fetch_tor",
            return_value=["198.51.100.0/24", "2001:db8::/32"]):
        result = row.refresh_from_source()
    row.refresh_from_db()
    assert result is False and row.data == "192.0.2.0/24", \
        "invalid source refresh partially replaced the prior set"
    assert row.sync_error == "source_refresh_failed", \
        "source failure was not bounded to a safe code"


@th.django_unit_test("ipdeny country sources retain safe URL derivation")
def test_ipdeny_source_url_derivation(opts):
    from mojo.apps.incident.models import IPSet

    _remove("country_gb")
    row = IPSet.objects.create(
        name="country_gb", kind="country", source="ipdeny")
    response = mock.Mock(text="192.0.2.0/24\n")
    with mock.patch("requests.get", return_value=response) as request:
        assert row._fetch_ipdeny() == ["192.0.2.0/24"]
    response.raise_for_status.assert_called_once_with()
    request.assert_called_once_with(
        "https://www.ipdeny.com/ipblocks/data/countries/gb.zone", timeout=30)
    row.refresh_from_db()
    assert row.source_url.endswith("/gb.zone")


@th.django_unit_test("ipdeny derivation rejects invalid country set names")
def test_ipdeny_invalid_name_is_closed(opts):
    from mojo.apps.incident.models import IPSet

    _remove("datacenter_edge")
    row = IPSet.objects.create(
        name="datacenter_edge", kind="datacenter", source="ipdeny")
    with th.assert_raises(ValueError):
        row._fetch_ipdeny()


@th.django_unit_test("explicit ipdeny URLs are preserved")
def test_ipdeny_explicit_source_url(opts):
    from mojo.apps.incident.models import IPSet

    _remove("country_custom")
    source_url = "https://feeds.example.invalid/custom.zone"
    row = IPSet.objects.create(
        name="country_custom", kind="country", source="ipdeny",
        source_url=source_url)
    response = mock.Mock(text="")
    with mock.patch("requests.get", return_value=response) as request:
        row._fetch_ipdeny()
    request.assert_called_once_with(source_url, timeout=30)
    row.refresh_from_db()
    assert row.source_url == source_url


@th.django_unit_test("cache sets use the sole reserved-namespace factory")
def test_cache_factory_is_disabled(opts):
    from mojo.apps.incident.models import IPSet

    IPSet.objects.filter(name__in=IPSet.THREAT_CACHE_SETS).delete()
    rows = IPSet.ensure_threat_caches()
    assert {row.name for row in rows} == set(IPSet.THREAT_CACHE_SETS), rows
    assert all(not row.is_enabled for row in rows), \
        "cache-only sets entered the enforcement lifecycle"


@th.django_unit_test("source credentials are excluded from generic output")
def test_source_key_is_sensitive(opts):
    from mojo.apps.incident.models import IPSet

    assert "source_key" in IPSet.RestMeta.SENSITIVE_FIELDS
    assert "source_key" not in IPSet.RestMeta.SEARCH_FIELDS
