"""
Tests for IPSet ipdeny source_url auto-derivation.
"""
from testit import helpers as th


@th.django_unit_test()
def test_fetch_ipdeny_derives_source_url(opts):
    """_fetch_ipdeny should auto-construct source_url from country name."""
    from mojo.apps.incident.models.ipset import IPSet

    # Clean up from previous runs
    IPSet.objects.filter(name="country_cn").delete()

    ipset = IPSet.objects.create(
        name="country_cn",
        kind="country",
        source="ipdeny",
    )
    assert not ipset.source_url, f"source_url should be empty initially, got {ipset.source_url}"

    # Call _fetch_ipdeny — it will derive the URL and persist it, then fail on the HTTP call
    # We catch the requests error since we don't have network access, but the URL should be set
    try:
        ipset._fetch_ipdeny()
    except Exception:
        pass

    ipset.refresh_from_db()
    expected = "http://www.ipdeny.com/ipblocks/data/countries/cn.zone"
    assert ipset.source_url == expected, (
        f"source_url should be '{expected}', got '{ipset.source_url}'"
    )

    # Cleanup
    ipset.delete()


@th.django_unit_test()
def test_fetch_ipdeny_raises_on_bad_name(opts):
    """_fetch_ipdeny should raise ValueError when name doesn't start with country_."""
    from mojo.apps.incident.models.ipset import IPSet

    IPSet.objects.filter(name="abuse_ips_test").delete()

    ipset = IPSet.objects.create(
        name="abuse_ips_test",
        kind="abuse",
        source="ipdeny",
    )

    raised = False
    try:
        ipset._fetch_ipdeny()
    except ValueError as e:
        raised = True
        assert "country_" in str(e), f"Error message should mention 'country_', got: {e}"

    assert raised, "_fetch_ipdeny should raise ValueError for non-country name without source_url"

    ipset.delete()


@th.django_unit_test()
def test_fetch_ipdeny_raises_on_empty_code(opts):
    """_fetch_ipdeny should raise ValueError when name is 'country_' with no code."""
    from mojo.apps.incident.models.ipset import IPSet

    IPSet.objects.filter(name="country_").delete()

    ipset = IPSet.objects.create(
        name="country_",
        kind="country",
        source="ipdeny",
    )

    raised = False
    try:
        ipset._fetch_ipdeny()
    except ValueError as e:
        raised = True
        assert "country code" in str(e).lower(), f"Error should mention country code, got: {e}"

    assert raised, "_fetch_ipdeny should raise ValueError for empty country code"

    ipset.delete()


@th.django_unit_test()
def test_refresh_from_source_stores_error_on_bad_name(opts):
    """refresh_from_source should store the ValueError in sync_error."""
    from mojo.apps.incident.models.ipset import IPSet

    IPSet.objects.filter(name="datacenter_test").delete()

    ipset = IPSet.objects.create(
        name="datacenter_test",
        kind="datacenter",
        source="ipdeny",
    )

    result = ipset.refresh_from_source()
    assert result is False, "refresh_from_source should return False on error"

    ipset.refresh_from_db()
    assert ipset.sync_error, "sync_error should be set after ValueError"
    assert "country_" in ipset.sync_error, (
        f"sync_error should mention 'country_', got: {ipset.sync_error}"
    )

    ipset.delete()


@th.django_unit_test()
def test_fetch_ipdeny_skips_derivation_when_url_set(opts):
    """_fetch_ipdeny should use existing source_url and not overwrite it."""
    from mojo.apps.incident.models.ipset import IPSet

    IPSet.objects.filter(name="country_custom").delete()

    custom_url = "http://example.com/custom.zone"
    ipset = IPSet.objects.create(
        name="country_custom",
        kind="country",
        source="ipdeny",
        source_url=custom_url,
    )

    try:
        ipset._fetch_ipdeny()
    except Exception:
        pass

    ipset.refresh_from_db()
    assert ipset.source_url == custom_url, (
        f"source_url should remain '{custom_url}', got '{ipset.source_url}'"
    )

    ipset.delete()
