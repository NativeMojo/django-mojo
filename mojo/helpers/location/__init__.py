

def validate_address(address_data):
    """
    Validate an address using USPS API.

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
    if address_data.get("provider") == "google":
        return google.validate_address(address_data)
    return usps.validate_address(address_data)


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
