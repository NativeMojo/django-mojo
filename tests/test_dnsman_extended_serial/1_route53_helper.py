"""Split out of tests/test_dnsman/1_route53_helper.py (maestro #1839).

The TTL-zero cache test patches the shared settings singleton
(mojo.helpers.settings.settings.get) for an in-process call, which is
process-global and unsafe under the parallel default tier.
"""

from unittest.mock import patch, MagicMock

from testit import helpers as th


MODULE = "mojo.helpers.aws.route53"


def _client(**responses):
    """Build a MagicMock boto client with canned return values."""
    client = MagicMock()
    for name, value in responses.items():
        getattr(client, name).return_value = value
    return client


def _prices(tld="com", price=13.0, currency="USD"):
    return {
        "Prices": [{
            "Name": tld,
            "RegistrationPrice": {"Price": price, "Currency": currency},
            "RenewalPrice": {"Price": price, "Currency": currency},
        }]
    }


def _settings(**overrides):
    """Patch settings.get in-process (th.server_settings is for the separate
    server process and does nothing for these direct module calls)."""
    from mojo.helpers.settings import settings as settings_obj

    real_get = settings_obj.get

    def patched_get(name, *args, **kwargs):
        if name in overrides:
            return overrides[name]
        return real_get(name, *args, **kwargs)

    return patch.object(settings_obj, "get", side_effect=patched_get)


@th.django_unit_test()
def test_list_prices_ttl_zero_disables_cache(opts):
    """ROUTE53_PRICE_CACHE_HOURS <= 0 is the escape hatch: no hits, no stores."""
    from mojo.helpers.aws import route53

    route53._price_cache.clear()
    client = _client(list_prices=_prices())

    with _settings(ROUTE53_PRICE_CACHE_HOURS=0):
        with patch(f"{MODULE}._domains_client", return_value=client):
            route53.list_prices("com")
            route53.list_prices("com")

    assert client.list_prices.call_count == 2, (
        f"Expected TTL<=0 to disable caching, got {client.list_prices.call_count} calls")
    assert "com" not in route53._price_cache, (
        "Expected TTL<=0 to store nothing in the cache")

