"""
Google Address & Location Service

Provides:
- Address Validation (verify complete address)
- Address Autocomplete (suggestions as user types)
- Geocoding (address → coordinates)
- Reverse Geocoding (coordinates → address)
- Time Zone Lookup (coordinates → timezone)

Authentication:
- API Key (recommended, simpler)
- Service Account (enterprise, OAuth tokens)
"""

import requests
from datetime import datetime, timedelta
import threading
import uuid
from typing import List, Dict, Optional
from google.oauth2 import service_account
from google.auth.transport.requests import Request

from mojo.helpers import logit
from mojo.helpers.settings import settings


class GoogleAddressService:
    """
    Unified Google Address & Location Service

    All Google location-related APIs in one service with unified authentication.
    """

    def __init__(self, use_service_account=False):
        """
        Initialize Google Address Service

        Args:
            use_service_account: If True, use service account auth.
                               If False, use API key (simpler, recommended)
        """
        self.use_service_account = use_service_account

        # API endpoints
        self.validation_url = "https://addressvalidation.googleapis.com/v1"
        self.autocomplete_url = "https://maps.googleapis.com/maps/api/place/autocomplete/json"
        self.place_details_url = "https://maps.googleapis.com/maps/api/place/details/json"
        self.geocoding_url = "https://maps.googleapis.com/maps/api/geocode/json"
        self.timezone_url = "https://maps.googleapis.com/maps/api/timezone/json"

        if self.use_service_account:
            # Service Account authentication (enterprise)
            self.service_account_file = settings.GOOGLE_SERVICE_ACCOUNT_FILE
            self.scopes = ['https://www.googleapis.com/auth/cloud-platform']

            # Token storage
            self._access_token = None
            self._access_token_expires_at = None
            self._credentials = None
            self._token_lock = threading.Lock()

            # Load credentials
            self._load_credentials()
        else:
            # API Key authentication (simple)
            self.api_key = settings.GOOGLE_MAPS_API_KEY

    # ==========================================
    # Authentication (Service Account)
    # ==========================================

    def _load_credentials(self):
        """Load service account credentials from JSON file"""
        try:
            self._credentials = service_account.Credentials.from_service_account_file(
                self.service_account_file,
                scopes=self.scopes
            )
        except Exception as e:
            logit.exception(f"Failed to load Google service account credentials: {e}")
            raise GoogleAuthenticationError(f"Failed to load credentials: {e}")

    def get_access_token(self):
        """Get valid access token (service account only)"""
        if not self.use_service_account:
            raise GoogleAuthenticationError("get_access_token called but not using service account auth")

        with self._token_lock:
            # Check if we have a valid access token
            if self._access_token and self._access_token_expires_at:
                if datetime.now() < (self._access_token_expires_at - timedelta(minutes=5)):
                    return self._access_token

            # Get new token
            return self._refresh_access_token()

    def _refresh_access_token(self):
        """Get new access token using service account credentials"""
        try:
            self._credentials.refresh(Request())
            self._access_token = self._credentials.token
            self._access_token_expires_at = self._credentials.expiry
            return self._access_token

        except Exception as e:
            logit.exception(f"Failed to refresh Google access token: {e}")
            raise GoogleAuthenticationError(f"Failed to get access token: {e}")

    def _get_auth_headers_and_params(self):
        """Get authentication headers and params based on auth method"""
        headers = {"Content-Type": "application/json"}
        params = {}

        if self.use_service_account:
            access_token = self.get_access_token()
            headers["Authorization"] = f"Bearer {access_token}"
        else:
            params["key"] = self.api_key

        return headers, params

    # ==========================================
    # Address Validation
    # ==========================================

    def validate_address(self, address_data):
        """
        Validate a complete address

        Args:
            address_data: Dict with address1, city, state, zip, etc.

        Returns:
            Dict with validation result
        """
        url = f"{self.validation_url}:validateAddress"

        # Prepare payload
        payload = {
            "address": {
                "regionCode": "US",
                "addressLines": [address_data["address1"]],
                "locality": address_data["city"],
                "administrativeArea": address_data["state"]
            }
        }

        if address_data.get("address2"):
            payload["address"]["addressLines"].append(address_data["address2"])
        if address_data.get("postal_code"):
            payload["address"]["postalCode"] = address_data["postal_code"]

        # Make request with automatic token refresh
        max_retries = 2
        for attempt in range(max_retries):
            try:
                headers, params = self._get_auth_headers_and_params()

                response = requests.post(
                    url,
                    json=payload,
                    headers=headers,
                    params=params,
                    timeout=10
                )

                # Handle 401 Unauthorized (token issue)
                if response.status_code == 401:
                    if self.use_service_account and attempt < max_retries - 1:
                        # Clear token and retry
                        self._access_token = None
                        self._access_token_expires_at = None
                        continue
                    else:
                        raise GoogleAPIError("Authentication failed")

                # Handle quota/rate limit errors
                if response.status_code == 429:
                    raise GoogleAPIError("Rate limit exceeded")

                # Handle other errors
                if response.status_code != 200:
                    error_detail = response.text
                    logit.error(f"Google Address Validation API error {response.status_code}: {error_detail}")
                    raise GoogleAPIError(f"API returned {response.status_code}")

                # Success - parse and return
                return self._parse_validation_response(response.json(), address_data)

            except requests.exceptions.Timeout:
                raise GoogleAPIError("Request timed out")
            except requests.exceptions.RequestException as e:
                logit.exception(f"Google Address Validation request failed: {e}")
                raise GoogleAPIError(f"Request failed: {e}")

        raise GoogleAPIError("Failed to validate address after retries")

    def _parse_validation_response(self, data, original_address):
        """Parse validation response into standardized format"""
        result = data.get("result", {})
        verdict = result.get("verdict", {})
        address = result.get("address", {})
        metadata = address.get("metadata", {})
        geocode = result.get("geocode", {})

        # Check if address is complete
        if not verdict.get("addressComplete", False):
            return {
                "valid": False,
                "error": "Incomplete or invalid address",
                "original_address": original_address
            }

        # Check validation granularity
        validation_granularity = verdict.get("validationGranularity", "")
        if validation_granularity in ["GRANULARITY_UNKNOWN", "OTHER"]:
            return {
                "valid": False,
                "error": "Address could not be validated to sufficient detail",
                "original_address": original_address
            }

        # Check for PO Box
        if metadata.get("poBox", False):
            return {
                "valid": False,
                "error": "PO Box addresses not accepted",
                "original_address": original_address
            }

        # Extract address components
        postal_address = address.get("postalAddress", {})
        address_lines = postal_address.get("addressLines", [])

        # Get coordinates
        location = geocode.get("location", {})

        # Determine if residential or business
        is_residential = metadata.get("residential", False)
        is_business = metadata.get("business", False)

        return {
            "valid": True,
            "source": "google",
            "standardized_address": {
                "line1": address_lines[0] if len(address_lines) > 0 else "",
                "line2": address_lines[1] if len(address_lines) > 1 else None,
                "city": postal_address.get("locality", ""),
                "state": postal_address.get("administrativeArea", ""),
                "postal_code": postal_address.get("postalCode", ""),
                "zip4": None,
                "full_zip": postal_address.get("postalCode", "")
            },
            "metadata": {
                "residential": is_residential,
                "business": is_business,
                "deliverable": True,
                "vacant": False,
                "latitude": location.get("latitude"),
                "longitude": location.get("longitude"),
                "place_id": geocode.get("placeId"),
                "plus_code": geocode.get("plusCode", {}).get("globalCode"),
            },
            "corrections": {
                "address_corrected": verdict.get("hasReplacedComponents", False),
                "has_unconfirmed_components": verdict.get("hasUnconfirmedComponents", False),
                "validation_granularity": validation_granularity
            },
            "original_address": original_address
        }

    # ==========================================
    # Address Autocomplete
    # ==========================================

    def get_address_suggestions(
        self,
        input_text: str,
        session_token: Optional[str] = None,
        country: str = "US",
        location: Optional[Dict[str, float]] = None,
        radius: Optional[int] = None
    ) -> Dict:
        """
        Get address suggestions based on partial input

        Args:
            input_text: Partial address text (e.g., "1600 Amph")
            session_token: Session token for per-session billing (recommended!)
            country: ISO country code to restrict results (default: US)
            location: Dict with 'lat' and 'lng' to bias results
            radius: Radius in meters to bias results around location

        Returns:
            Dict with suggestions and metadata
        """
        if not input_text or len(input_text) < 3:
            return {
                "success": False,
                "error": "Input too short (minimum 3 characters)",
                "data": [],
                "size": 0,
                "count": 0
            }

        params = {
            "input": input_text,
            "types": "address",
            "components": f"country:{country.lower()}"
        }

        # Add session token for better pricing
        if session_token:
            params["sessiontoken"] = session_token

        # Add location bias
        if location:
            params["location"] = f"{location['lat']},{location['lng']}"
            if radius:
                params["radius"] = radius

        # Add API key
        if not self.use_service_account:
            params["key"] = self.api_key

        try:
            response = requests.get(
                self.autocomplete_url,
                params=params,
                timeout=5
            )

            if response.status_code != 200:
                logit.error(f"Google Autocomplete API error: {response.status_code}")
                return {
                    "success": False,
                    "error": f"API returned status {response.status_code}",
                    "data": [],
                    "size": 0,
                    "count": 0
                }

            data = response.json()
            status = data.get("status")

            if status == "ZERO_RESULTS":
                return {
                    "success": True,
                    "data": [],
                    "size": 0,
                    "count": 0,
                    "message": "No addresses found"
                }

            if status != "OK":
                error_message = data.get("error_message", status)
                logit.error(f"Google Autocomplete error: {error_message}")
                return {
                    "success": False,
                    "error": error_message,
                    "data": [],
                    "size": 0,
                    "count": 0
                }

            # Parse predictions
            predictions = data.get("predictions", [])
            suggestions = []

            for prediction in predictions:
                place_id = prediction.get("place_id")
                suggestions.append({
                    "id": place_id,  # Required by UI framework
                    "place_id": place_id,
                    "description": prediction.get("description"),
                    "main_text": prediction.get("structured_formatting", {}).get("main_text"),
                    "secondary_text": prediction.get("structured_formatting", {}).get("secondary_text"),
                    "types": prediction.get("types", [])
                })

            return {
                "success": True,
                "data": suggestions,
                "size": len(suggestions),
                "count": len(suggestions)
            }

        except requests.exceptions.Timeout:
            raise GoogleAPIError("Autocomplete request timed out")
        except requests.exceptions.RequestException as e:
            logit.exception(f"Google Autocomplete request failed: {e}")
            raise GoogleAPIError(f"Request failed: {e}")

    def get_place_details(self, place_id: str, session_token: Optional[str] = None) -> Dict:
        """
        Get full address details for a selected place

        Args:
            place_id: Place ID from autocomplete suggestion
            session_token: Same session token used in autocomplete

        Returns:
            Parsed address components
        """
        params = {
            "place_id": place_id,
            "fields": "address_components,formatted_address,geometry"
        }

        if session_token:
            params["sessiontoken"] = session_token

        # Add API key
        if not self.use_service_account:
            params["key"] = self.api_key

        try:
            response = requests.get(
                self.place_details_url,
                params=params,
                timeout=5
            )

            if response.status_code != 200:
                logit.error(f"Google Place Details API error: {response.status_code}")
                return {
                    "success": False,
                    "error": f"API returned status {response.status_code}"
                }

            data = response.json()
            status = data.get("status")

            if status != "OK":
                error_message = data.get("error_message", status)
                logit.error(f"Google Place Details error: {error_message}")
                return {
                    "success": False,
                    "error": error_message
                }

            result = data.get("result", {})
            address_components = result.get("address_components", [])

            # Parse address components
            parsed = self._parse_address_components(address_components)
            parsed["formatted_address"] = result.get("formatted_address")

            # Add coordinates
            geometry = result.get("geometry", {})
            location = geometry.get("location", {})
            parsed["latitude"] = location.get("lat")
            parsed["longitude"] = location.get("lng")

            return {
                "success": True,
                "address": parsed
            }

        except requests.exceptions.RequestException as e:
            logit.exception(f"Google Place Details request failed: {e}")
            raise GoogleAPIError(f"Request failed: {e}")

    def _parse_address_components(self, components: List[Dict]) -> Dict:
        """Parse Google address components into standard format"""
        address = {
            "street_number": None,
            "street_name": None,
            "address1": None,
            "city": None,
            "county": None,
            "state": None,
            "state_code": None,
            "postal_code": None,
            "country": None,
            "country_code": None
        }

        for component in components:
            types = component.get("types", [])
            long_name = component.get("long_name")
            short_name = component.get("short_name")

            if "street_number" in types:
                address["street_number"] = long_name
            elif "route" in types:
                address["street_name"] = long_name
            elif "locality" in types:
                address["city"] = long_name
            elif "administrative_area_level_2" in types:
                address["county"] = long_name
            elif "administrative_area_level_1" in types:
                address["state"] = long_name
                address["state_code"] = short_name
            elif "postal_code" in types:
                address["postal_code"] = long_name
            elif "country" in types:
                address["country"] = long_name
                address["country_code"] = short_name

        # Combine street number and name
        if address["street_number"] and address["street_name"]:
            address["address1"] = f"{address['street_number']} {address['street_name']}"
        elif address["street_name"]:
            address["address1"] = address["street_name"]

        return address

    # ==========================================
    # Geocoding
    # ==========================================

    def geocode_address(self, address: str) -> Dict:
        """
        Convert address to coordinates

        Args:
            address: Full address string or dict with components

        Returns:
            Dict with coordinates and parsed address
        """
        if isinstance(address, dict):
            # Build address string from components
            parts = [
                address.get("address1"),
                address.get("city"),
                address.get("state"),
                address.get("postal_code")
            ]
            address = ", ".join([p for p in parts if p])

        params = {"address": address}

        # Add API key
        if not self.use_service_account:
            params["key"] = self.api_key

        try:
            response = requests.get(
                self.geocoding_url,
                params=params,
                timeout=5
            )

            if response.status_code != 200:
                logit.error(f"Google Geocoding API error: {response.status_code}")
                return {
                    "success": False,
                    "error": f"API returned status {response.status_code}"
                }

            data = response.json()
            status = data.get("status")

            if status == "ZERO_RESULTS":
                return {
                    "success": False,
                    "error": "Address not found"
                }

            if status != "OK":
                error_message = data.get("error_message", status)
                logit.error(f"Google Geocoding error: {error_message}")
                return {
                    "success": False,
                    "error": error_message
                }

            # Get first result
            results = data.get("results", [])
            if not results:
                return {
                    "success": False,
                    "error": "No results found"
                }

            result = results[0]
            location = result.get("geometry", {}).get("location", {})

            return {
                "success": True,
                "latitude": location.get("lat"),
                "longitude": location.get("lng"),
                "formatted_address": result.get("formatted_address"),
                "place_id": result.get("place_id"),
                "address_components": self._parse_address_components(result.get("address_components", []))
            }

        except requests.exceptions.RequestException as e:
            logit.exception(f"Google Geocoding request failed: {e}")
            raise GoogleAPIError(f"Request failed: {e}")

    # ==========================================
    # Reverse Geocoding
    # ==========================================

    def reverse_geocode(self, latitude: float, longitude: float) -> Dict:
        """
        Convert coordinates to address

        Args:
            latitude: Latitude coordinate
            longitude: Longitude coordinate

        Returns:
            Dict with address information
        """
        params = {"latlng": f"{latitude},{longitude}"}

        # Add API key
        if not self.use_service_account:
            params["key"] = self.api_key

        try:
            response = requests.get(
                self.geocoding_url,
                params=params,
                timeout=5
            )

            if response.status_code != 200:
                logit.error(f"Google Reverse Geocoding API error: {response.status_code}")
                return {
                    "success": False,
                    "error": f"API returned status {response.status_code}"
                }

            data = response.json()
            status = data.get("status")

            if status == "ZERO_RESULTS":
                return {
                    "success": False,
                    "error": "No address found for coordinates"
                }

            if status != "OK":
                error_message = data.get("error_message", status)
                logit.error(f"Google Reverse Geocoding error: {error_message}")
                return {
                    "success": False,
                    "error": error_message
                }

            # Get first result (most precise)
            results = data.get("results", [])
            if not results:
                return {
                    "success": False,
                    "error": "No results found"
                }

            result = results[0]

            return {
                "success": True,
                "formatted_address": result.get("formatted_address"),
                "place_id": result.get("place_id"),
                "address_components": self._parse_address_components(result.get("address_components", []))
            }

        except requests.exceptions.RequestException as e:
            logit.exception(f"Google Reverse Geocoding request failed: {e}")
            raise GoogleAPIError(f"Request failed: {e}")

    # ==========================================
    # Time Zone
    # ==========================================

    def get_timezone(self, latitude: float, longitude: float, timestamp: Optional[int] = None) -> Dict:
        """
        Get timezone information for coordinates

        Args:
            latitude: Latitude coordinate
            longitude: Longitude coordinate
            timestamp: Unix timestamp (default: current time)

        Returns:
            Dict with timezone information
        """
        if timestamp is None:
            timestamp = int(datetime.now().timestamp())

        params = {
            "location": f"{latitude},{longitude}",
            "timestamp": timestamp
        }

        # Add API key
        if not self.use_service_account:
            params["key"] = self.api_key

        try:
            response = requests.get(
                self.timezone_url,
                params=params,
                timeout=5
            )

            if response.status_code != 200:
                logit.error(f"Google Timezone API error: {response.status_code}")
                return {
                    "success": False,
                    "error": f"API returned status {response.status_code}"
                }

            data = response.json()
            status = data.get("status")

            if status != "OK":
                error_message = data.get("error_message", status)
                logit.error(f"Google Timezone error: {error_message}")
                return {
                    "success": False,
                    "error": error_message
                }

            return {
                "success": True,
                "timezone_id": data.get("timeZoneId"),
                "timezone_name": data.get("timeZoneName"),
                "raw_offset": data.get("rawOffset"),  # Seconds from UTC
                "dst_offset": data.get("dstOffset"),  # DST offset in seconds
                "total_offset": data.get("rawOffset", 0) + data.get("dstOffset", 0)  # Total offset
            }

        except requests.exceptions.RequestException as e:
            logit.exception(f"Google Timezone request failed: {e}")
            raise GoogleAPIError(f"Request failed: {e}")

    # ==========================================
    # Utility Methods
    # ==========================================

    def clear_tokens(self):
        """Manually clear all tokens (service account only)"""
        if self.use_service_account:
            with self._token_lock:
                self._access_token = None
                self._access_token_expires_at = None

    def get_token_status(self):
        """Get current token status (useful for debugging)"""
        if not self.use_service_account:
            return {"auth_method": "api_key", "no_tokens": True}

        now = datetime.now()

        status = {
            "auth_method": "service_account",
            "has_access_token": self._access_token is not None,
        }

        if self._access_token_expires_at:
            time_remaining = (self._access_token_expires_at - now).total_seconds()
            status["access_token_expires_in_seconds"] = max(0, int(time_remaining))
            status["access_token_expired"] = time_remaining <= 0

        return status


# ==========================================
# Session Management for Autocomplete
# ==========================================

class GoogleAutocompleteSession:
    """
    Manages a single autocomplete session for better pricing

    Google charges per-session ($2.83/1K) if you use session tokens,
    vs per-request ($17/1K) without session tokens.
    """

    def __init__(self, google_service: GoogleAddressService):
        self.google = google_service
        self.session_token = str(uuid.uuid4())
        self.session_started = False
        self.session_completed = False

    def get_suggestions(self, input_text: str, **kwargs) -> Dict:
        """Get suggestions using this session's token"""
        self.session_started = True
        return self.google.get_address_suggestions(
            input_text,
            session_token=self.session_token,
            **kwargs
        )

    def get_place_details(self, place_id: str) -> Dict:
        """Get place details and complete the session"""
        result = self.google.get_place_details(
            place_id,
            session_token=self.session_token
        )
        self.session_completed = True
        return result

    def is_active(self) -> bool:
        """Check if session is active (started but not completed)"""
        return self.session_started and not self.session_completed


# ==========================================
# Custom Exceptions
# ==========================================

class GoogleAuthenticationError(Exception):
    """Raised when authentication with Google fails"""
    pass


class GoogleAPIError(Exception):
    """Raised when Google API request fails"""
    pass


# ==========================================
# Singleton Instance
# ==========================================

google_api = None

def get_google_api():
    """
    Get or create singleton Google API instance

    Returns:
        GoogleAddressService: Singleton instance
    """
    global google_api
    if not google_api:
        google_api = GoogleAddressService(use_service_account=False)
    return google_api


def validate_address(address_data):
    """
    Validate address using Google Address Validation API

    Convenience function that uses singleton instance.

    Args:
        address_data: Dict with address components

    Returns:
        Dict with validation result
    """
    service = get_google_api()
    return service.validate_address(address_data)
