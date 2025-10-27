"""
MaxMind GeoIP2 provider for GeoIP lookups.
https://www.maxmind.com/en/geoip2-precision-services
"""
from mojo.helpers.location.countries import get_country_name
from .config import MAXMIND_ACCOUNT_ID, MAXMIND_LICENSE_KEY


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
    try:
        import geoip2.webservice
    except ImportError:
        print("[GeoIP Error] MaxMind provider requires the 'geoip2' package. Install with: pip install geoip2")
        return None

    account_id = MAXMIND_ACCOUNT_ID
    license_key = MAXMIND_LICENSE_KEY

    if not account_id or not license_key:
        print("[GeoIP Error] MaxMind provider requires GEOIP_API_KEY_MAXMIND_ACCOUNT_ID and "
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

            return {
                'provider': 'maxmind',
                'country_code': country_code,
                'country_name': response.country.name or get_country_name(country_code),
                'region': response.subdivisions.most_specific.name if response.subdivisions else None,
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
        print(f"[GeoIP Error] MaxMind: Address {ip_address} not found in database")
        return None
    except geoip2.errors.AuthenticationError:
        print("[GeoIP Error] MaxMind: Authentication failed. Check your account ID and license key.")
        return None
    except geoip2.errors.InsufficientFundsError:
        print("[GeoIP Error] MaxMind: Insufficient funds in account")
        return None
    except geoip2.errors.PermissionRequiredError:
        print("[GeoIP Error] MaxMind: Permission required for this service")
        return None
    except Exception as e:
        print(f"[GeoIP Error] Failed to fetch from MaxMind for IP {ip_address}: {e}")
        return None
