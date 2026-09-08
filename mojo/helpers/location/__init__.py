from collections.abc import Mapping

from mojo.helpers import logit
from mojo.helpers.settings import settings


def _failure(provider, result=None, error=None):
    if error is not None:
        message = str(error) or f"{provider.title()} address validation failed"
        return {}, message, error.__class__.__name__, False

    if not isinstance(result, Mapping):
        message = f"{provider.title()} address validator returned an invalid response"
        return {}, message, "InvalidResponse", False

    payload = dict(result)
    message = payload.get("error") or f"{provider.title()} address validation failed"
    status = payload.get("status", False)
    if not isinstance(status, (bool, int)) and status is not None:
        status = "unknown"
    return payload, str(message), "ValidationFailure", status


def _attempt(provider, address_data, validator):
    try:
        result = validator(address_data)
    except Exception as err:
        payload, message, failure_class, status = _failure(provider, error=err)
        return None, payload, message, failure_class, status

    if isinstance(result, Mapping) and result.get("valid") is True:
        payload = dict(result)
        payload["provider"] = provider
        return payload, None, None, None, None

    payload, message, failure_class, status = _failure(provider, result=result)
    return None, payload, message, failure_class, status


def validate_address(address_data):
    """
    Validate an address using USPS with one-way fallback to Google.

    Args:
        address_data (dict): Address data to validate.
        {
            "address1": "123 Main St",
            "address2": "Apt 4B",
            "city": "Anytown",
            "state": "CA",
            "postal_code": "12345",
            "country": "US",
            "provider": "usps"
        }

    Returns:
        dict: Validated address data.
    """
    from . import google
    from . import usps

    requested_provider = address_data.get("provider", "usps")
    usps_ready = bool(settings.USPS_CLIENT_ID and settings.USPS_CLIENT_SECRET)
    if requested_provider == "google" or not usps_ready:
        providers = (("google", google.validate_address),)
    else:
        providers = (
            ("usps", usps.validate_address),
            ("google", google.validate_address),
        )

    errors = {}
    final_provider = None
    final_payload = {}
    final_message = None
    for provider, validator in providers:
        success, payload, message, failure_class, status = _attempt(
            provider, address_data, validator)
        if success is not None:
            return success

        errors[provider] = message
        final_provider = provider
        final_payload = payload
        final_message = message
        if provider == "usps":
            logit.warning(
                "location address failover provider=usps "
                f"class={failure_class} status={status}")

    final_payload["status"] = False
    final_payload["provider"] = final_provider
    final_payload["error"] = final_message
    final_payload["errors"] = errors
    return final_payload

def get_address_suggestions(input_text, session_token=None, country="US", location=None, radius=None):
    """
    Get address suggestions as user types (autocomplete)

    Uses Google Places Autocomplete API

    Args:
        input_text (str): Partial address text (e.g., "1600 Amph")
        session_token (str, optional): Session token for per-session billing
        country (str): ISO country code to restrict results (default: "US")
        location (dict, optional): Dict with 'lat' and 'lng' to bias results
        radius (int, optional): Radius in meters to bias results around location

    Returns:
        dict: {
            "success": bool,
            "data": [
                {
                    "id": "ChIJ...",  # Same as place_id, for UI frameworks
                    "place_id": "ChIJ...",
                    "description": "1600 Amphitheatre Parkway, Mountain View, CA, USA",
                    "main_text": "1600 Amphitheatre Parkway",
                    "secondary_text": "Mountain View, CA, USA",
                    "types": ["street_address"]
                },
                ...
            ],
            "size": int,
            "count": int
        }

    Example:
        >>> suggestions = get_address_suggestions("1600 Amph")
        >>> for s in suggestions["data"]:
        ...     print(s["description"])
    """
    from . import google
    service = google.get_google_api()
    return service.get_address_suggestions(
        input_text=input_text,
        session_token=session_token,
        country=country,
        location=location,
        radius=radius
    )


def get_place_details(place_id, session_token=None):
    """
    Get full address details for a selected place from autocomplete

    Use this after user selects a suggestion from get_address_suggestions()

    Args:
        place_id (str): Place ID from autocomplete suggestion
        session_token (str, optional): Same session token used in autocomplete

    Returns:
        dict: {
            "success": bool,
            "address": {
                "address1": "1600 Amphitheatre Parkway",
                "city": "Mountain View",
                "state": "California",
                "state_code": "CA",
                "postal_code": "94043",
                "country": "United States",
                "country_code": "US",
                "formatted_address": "...",
                "latitude": 37.4224764,
                "longitude": -122.0842499
            }
        }

    Example:
        >>> # User types "1600 Amph"
        >>> suggestions = get_address_suggestions("1600 Amph", session_token="abc123")
        >>>
        >>> # User selects first suggestion
        >>> details = get_place_details(suggestions["data"][0]["place_id"], session_token="abc123")
        >>> print(details["address"]["address1"])
        "1600 Amphitheatre Parkway"
    """
    from . import google
    service = google.get_google_api()
    return service.get_place_details(place_id=place_id, session_token=session_token)
