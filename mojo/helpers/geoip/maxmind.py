"""
MaxMind GeoIP2 provider for GeoIP lookups.
https://www.maxmind.com/en/geoip2-precision-services
"""
from mojo.helpers import logit
from mojo.helpers.location.countries import get_country_name
from .config import get_api_key


# The 'geoip2' package is an optional extra. Python does not cache failed
# imports, so this branch re-executes on every lookup — warn only once per
# process. Gates logging only, never control flow.
_geoip2_warned = False


def fetch(ip_address, api_key=None):
    """
    Fetches geolocation data from MaxMind GeoIP2 web service and normalizes it.

    Note: This requires the 'geoip2' package to be installed:
        pip install geoip2

    Args:
        ip_address: The IP address to look up
        api_key: Not used (MaxMind uses account_id and license_key from config)

    Returns:
        dict: Normalized geolocation data, or None on failure
    """
    global _geoip2_warned
    try:
        import geoip2.webservice
        import geoip2.errors
    except ImportError:
        if not _geoip2_warned:
            _geoip2_warned = True
            logit.warning(
                "[GeoIP] MaxMind provider disabled: optional package 'geoip2' is not "
                "installed (pip install geoip2). Falling back to the next configured "
                "provider. Logged once per process.")
        return None

    account_id = get_api_key('maxmind_account_id')
    license_key = get_api_key('maxmind_license_key')

    if not account_id or not license_key:
        logit.warning(
            "[GeoIP] MaxMind provider requires GEOIP_API_KEY_MAXMIND_ACCOUNT_ID and "
            "GEOIP_API_KEY_MAXMIND_LICENSE_KEY to be set in settings.")
        return None

    try:
        # Initialize the MaxMind client
        with geoip2.webservice.Client(account_id, license_key) as client:
            # Use the Insights endpoint (most comprehensive)
            # You can also use client.city(ip_address) for less detailed data
            response = client.insights(ip_address)

            # Extract data from response
            country_code = response.country.iso_code

            # Build ASN string similar to other providers
            asn = f"AS{response.traits.autonomous_system_number}" if response.traits.autonomous_system_number else None
            asn_org = response.traits.autonomous_system_organization

            # Determine connection type
            connection_type = None
            if response.traits.connection_type:
                connection_type = response.traits.connection_type

            # ISO 3166-2 region code (e.g. "FL" — combined as "US-FL" below)
            sub_iso = response.subdivisions.most_specific.iso_code if response.subdivisions else None
            region_code = f"{country_code}-{sub_iso}" if (country_code and sub_iso) else None

            return {
                'provider': 'maxmind',
                'country_code': country_code,
                'country_name': response.country.name or get_country_name(country_code),
                'region': response.subdivisions.most_specific.name if response.subdivisions else None,
                'region_code': region_code,
                'city': response.city.name,
                'postal_code': response.postal.code,
                'latitude': response.location.latitude,
                'longitude': response.location.longitude,
                'timezone': response.location.time_zone,
                'asn': asn,
                'asn_org': asn_org,
                'isp': response.traits.isp,
                'connection_type': connection_type,
                'data': {
                    'accuracy_radius': response.location.accuracy_radius,
                    'is_anonymous': response.traits.is_anonymous,
                    'is_anonymous_proxy': response.traits.is_anonymous_proxy,
                    'is_anonymous_vpn': response.traits.is_anonymous_vpn,
                    'is_hosting_provider': response.traits.is_hosting_provider,
                    'is_public_proxy': response.traits.is_public_proxy,
                    'is_tor_exit_node': response.traits.is_tor_exit_node,
                    'user_type': response.traits.user_type,
                    'domain': response.traits.domain,
                }
            }

    except geoip2.errors.AddressNotFoundError:
        logit.debug(f"[GeoIP] MaxMind: Address {ip_address} not found in database")
        return None
    except geoip2.errors.AuthenticationError:
        logit.error("[GeoIP] MaxMind: Authentication failed. Check your account ID and license key.")
        return None
    except geoip2.errors.InsufficientFundsError:
        logit.error("[GeoIP] MaxMind: Insufficient funds in account")
        return None
    except geoip2.errors.PermissionRequiredError:
        logit.error("[GeoIP] MaxMind: Permission required for this service")
        return None
    except Exception as e:
        logit.error(f"[GeoIP] Failed to fetch from MaxMind for IP {ip_address}: {e}")
        return None
