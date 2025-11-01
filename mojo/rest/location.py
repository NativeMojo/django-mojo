"""
Location REST API Endpoints

Provides address validation, autocomplete, and geocoding services.
"""

import uuid
from mojo import decorators as md
from mojo.helpers.response import JsonResponse
from mojo.helpers import location


@md.POST('location/address/validate')
@md.public_endpoint()
@md.requires_params("address1", "state")
def rest_validate_address(request):
    """
    Validate and standardize a US address

    POST /api/location/address/validate

    Body:
        address1 (str): Street address (required)
        address2 (str): Apartment, suite, etc. (optional)
        city (str): City name (optional if postal_code provided)
        state (str): Two-letter state code (required)
        postal_code (str): 5-digit ZIP code (optional)
        provider (str): "usps" or "google" (default: "usps")

    Returns:
        {
            "status": true,
            "data": {
                "valid": true,
                "source": "usps_v3",
                "standardized_address": {...},
                "metadata": {...}
            }
        }
    """
    address_data = {
        "address1": request.DATA.get("address1"),
        "address2": request.DATA.get("address2"),
        "city": request.DATA.get("city"),
        "state": request.DATA.get("state"),
        "postal_code": request.DATA.get("postal_code"),
        "provider": request.DATA.get("provider", "usps")
    }

    # Remove None values
    address_data = {k: v for k, v in address_data.items() if v is not None}

    try:
        result = location.validate_address(address_data)
        return JsonResponse(dict(status=True, data=result))
    except Exception as e:
        return JsonResponse(dict(status=False, error=str(e)), status=400)





@md.GET('location/address/suggestions')
@md.public_endpoint()
@md.requires_params("input")
def rest_address_suggestions(request):
    """
    Get address suggestions for autocomplete

    GET /api/location/address/suggestions?input=1600+Amph
    GET /api/location/address/suggestions?input=1600+Amph&session_token=abc123

    Params:
        input (str): Partial address text (required, min 3 characters)
        session_token (str): Optional - if not provided, a new one will be generated and returned
        country (str): ISO country code (default: "US")
        lat (float): Latitude for location bias (optional)
        lng (float): Longitude for location bias (optional)
        radius (int): Radius in meters for location bias (optional)

    Returns:
        {
            "success": true,
            "session_token": "550e8400-e29b-41d4-a716-446655440000",
            "data": [
                {
                    "id": "ChIJ...",
                    "place_id": "ChIJ...",
                    "description": "1600 Amphitheatre Parkway, Mountain View, CA, USA",
                    "main_text": "1600 Amphitheatre Parkway",
                    "secondary_text": "Mountain View, CA, USA"
                }
            ],
            "size": 5,
            "count": 5
        }

    Workflow:
        1. First request (no session_token) -> returns session_token in response
        2. Subsequent requests -> reuse session_token from first response
        3. When user selects -> use same session_token for place-details
        4. New address entry -> omit session_token to get a new one
    """
    input_text = request.DATA.get("input")
    session_token = request.DATA.get("session_token")

    # Generate session token if not provided
    if not session_token:
        session_token = str(uuid.uuid4())

    country = request.DATA.get("country", "US")

    # Optional location bias
    location_bias = None
    lat = request.DATA.get_typed("lat", None, float)
    lng = request.DATA.get_typed("lng", None, float)
    if lat is not None and lng is not None:
        location_bias = {"lat": lat, "lng": lng}

    radius = request.DATA.get_typed("radius", None, int)

    try:
        result = location.get_address_suggestions(
            input_text=input_text,
            session_token=session_token,
            country=country,
            location=location_bias,
            radius=radius
        )
        # Add session_token to response for client to reuse
        result["session_token"] = session_token
        return JsonResponse(result)
    except Exception as e:
        return JsonResponse(dict(success=False, error=str(e), data=[], size=0, count=0), status=400)


@md.GET('location/address/place-details')
@md.public_endpoint()
@md.requires_params("place_id")
def rest_place_details(request):
    """
    Get full address details for a selected place

    GET /api/location/address/place-details?place_id=ChIJ...&session_token=abc123

    Params:
        place_id (str): Place ID from autocomplete suggestion (required)
        session_token (str): Same session token used in autocomplete (optional but recommended)

    Returns:
        {
            "success": true,
            "address": {
                "address1": "1600 Amphitheatre Parkway",
                "city": "Mountain View",
                "state": "California",
                "state_code": "CA",
                "postal_code": "94043",
                "latitude": 37.4224764,
                "longitude": -122.0842499,
                "formatted_address": "..."
            }
        }
    """
    place_id = request.DATA.get("place_id")
    session_token = request.DATA.get("session_token")

    try:
        result = location.get_place_details(
            place_id=place_id,
            session_token=session_token
        )
        return JsonResponse(result)
    except Exception as e:
        return JsonResponse(dict(success=False, error=str(e)), status=400)


@md.POST('location/address/geocode')
@md.public_endpoint()
@md.requires_params("address")
def rest_geocode_address(request):
    """
    Convert address to coordinates (geocoding)

    POST /api/location/address/geocode

    Body:
        address (str or dict): Full address string or address components

    Returns:
        {
            "success": true,
            "latitude": 37.4224764,
            "longitude": -122.0842499,
            "formatted_address": "...",
            "place_id": "...",
            "address_components": {...}
        }
    """
    from mojo.helpers.location import google

    address = request.DATA.get("address")

    try:
        service = google.get_google_api()
        result = service.geocode_address(address)
        return JsonResponse(result)
    except Exception as e:
        return JsonResponse(dict(success=False, error=str(e)), status=400)


@md.GET('location/address/reverse-geocode')
@md.public_endpoint()
@md.requires_params("lat", "lng")
def rest_reverse_geocode(request):
    """
    Convert coordinates to address (reverse geocoding)

    GET /api/location/address/reverse-geocode?lat=37.4224764&lng=-122.0842499

    Params:
        lat (float): Latitude (required)
        lng (float): Longitude (required)

    Returns:
        {
            "success": true,
            "formatted_address": "...",
            "place_id": "...",
            "address_components": {...}
        }
    """
    from mojo.helpers.location import google

    lat = request.DATA.get_typed("lat", None, float)
    lng = request.DATA.get_typed("lng", None, float)

    if lat is None or lng is None:
        return JsonResponse(dict(success=False, error="Invalid lat/lng coordinates"), status=400)

    try:
        service = google.get_google_api()
        result = service.reverse_geocode(lat, lng)
        return JsonResponse(result)
    except Exception as e:
        return JsonResponse(dict(success=False, error=str(e)), status=400)


@md.GET('location/timezone')
@md.public_endpoint()
@md.requires_params("lat", "lng")
def rest_get_timezone(request):
    """
    Get timezone information for coordinates

    GET /api/location/timezone?lat=37.4224764&lng=-122.0842499

    Params:
        lat (float): Latitude (required)
        lng (float): Longitude (required)
        timestamp (int): Unix timestamp (optional, default: current time)

    Returns:
        {
            "success": true,
            "timezone_id": "America/Los_Angeles",
            "timezone_name": "Pacific Daylight Time",
            "raw_offset": -28800,
            "dst_offset": 3600,
            "total_offset": -25200
        }
    """
    from mojo.helpers.location import google

    lat = request.DATA.get_typed("lat", None, float)
    lng = request.DATA.get_typed("lng", None, float)
    timestamp = request.DATA.get_typed("timestamp", None, int)

    if lat is None or lng is None:
        return JsonResponse(dict(success=False, error="Invalid lat/lng coordinates"), status=400)

    try:
        service = google.get_google_api()
        result = service.get_timezone(lat, lng, timestamp)
        return JsonResponse(result)
    except Exception as e:
        return JsonResponse(dict(success=False, error=str(e)), status=400)


# Public endpoint for testing (no auth required)
@md.GET('location/address/validate-sample')
@md.public_endpoint()
def rest_validate_address_sample(request):
    """
    Sample address validation endpoint (public, for testing)

    GET /api/location/address/validate-sample

    Returns a validated sample address for testing purposes.
    """
    sample_address = {
        "address1": "1600 Amphitheatre Parkway",
        "city": "Mountain View",
        "state": "CA",
        "postal_code": "94043",
        "provider": "google"
    }

    try:
        result = location.validate_address(sample_address)
        return JsonResponse(dict(status=True, data=result, sample=True))
    except Exception as e:
        return JsonResponse(dict(status=False, error=str(e)), status=400)
