import requests
from datetime import datetime, timedelta
from mojo.helpers import logit
from mojo.helpers.settings import settings
import threading


class USPSAddressValidator:
    """
    USPS API v3 Address Validator with local token management

    Token Lifecycle:
    - Access Token: Valid for 8 hours (28800 seconds)
    - Client Credentials grant does NOT provide refresh tokens
    - Tokens stored locally in instance
    - Automatically re-authenticates when access token expires

    Note: Each instance maintains its own tokens. Multiple instances
    (e.g., multiple Django workers) will each authenticate separately.
    This is fine - USPS allows multiple concurrent sessions.

    API Details:
    - OAuth: Client Credentials grant (OAuth 2.0)
    - Address Validation: GET /addresses/v3/address (query parameters)
    - Authentication: Bearer token in Authorization header
    """

    def __init__(self, use_test_environment=False):
        """
        Initialize USPS API client

        Args:
            use_test_environment: If True, use test/sandbox environment instead of production
        """
        self.client_id = settings.USPS_CLIENT_ID
        self.client_secret = settings.USPS_CLIENT_SECRET

        # Validate credentials
        if not self.client_id or not self.client_secret:
            raise USPSAuthenticationError(
                "USPS_CLIENT_ID and USPS_CLIENT_SECRET must be set in settings"
            )

        # Check if using test environment (from settings or parameter)
        use_test = use_test_environment or getattr(settings, 'USPS_USE_TEST_ENVIRONMENT', False)

        # Set URLs based on environment
        if use_test:
            self.token_url = "https://apis-tem.usps.com/oauth2/v3/token"
            self.api_base_url = "https://apis-tem.usps.com"
            logit.info("Using USPS TEST environment")
        else:
            self.token_url = "https://apis.usps.com/oauth2/v3/token"
            self.api_base_url = "https://apis.usps.com"
            logit.info("Using USPS PRODUCTION environment")

        # Token storage (instance variables)
        self._access_token = None
        self._access_token_expires_at = None

        # Thread lock for token refresh (prevent race conditions in same instance)
        self._token_lock = threading.Lock()

    def get_access_token(self):
        """
        Get valid access token, re-authenticating if needed

        Flow:
        1. Check if we have a valid access token
        2. If expired, re-authenticate (client credentials doesn't support refresh)

        Note: Client credentials grant does NOT provide refresh tokens.
        Only authorization_code grant provides refresh tokens.
        """
        with self._token_lock:
            # Check if we have a valid access token
            if self._access_token and self._access_token_expires_at:
                # Check if token is still valid (with 5 min buffer)
                if datetime.now() < (self._access_token_expires_at - timedelta(minutes=5)):
                    return self._access_token

            # Access token expired or doesn't exist, re-authenticate
            return self._authenticate()

    def _authenticate(self):
        """
        Authenticate with client credentials (initial login)
        Returns access token

        Note: Client credentials grant only returns access_token, not refresh_token
        """
        logit.info("Authenticating with USPS API")

        # The OpenAPI spec example shows credentials in body:
        # grant_type=client_credentials &client_id=123 &client_secret=ABC &scope=addresses
        # Try this approach first (credentials in body, no Basic Auth)
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        try:
            # Send credentials in body as form data (per OpenAPI spec example)
            response = requests.post(
                self.token_url,
                data=payload,
                timeout=10
            )

            # Log response for debugging
            logit.info(f"USPS auth response status: {response.status_code}")

            if response.status_code != 200:
                # Log error details
                error_body = response.text
                logit.error(f"USPS auth error ({response.status_code}): {error_body}")

                # Try to parse error JSON
                try:
                    error_json = response.json()
                    error_msg = error_json.get("error_description", error_json.get("error", error_body))
                except:
                    error_msg = error_body

                raise USPSAuthenticationError(
                    f"USPS authentication failed ({response.status_code}): {error_msg}"
                )

            response.raise_for_status()

            token_data = response.json()
            return self._store_tokens(token_data)

        except USPSAuthenticationError:
            raise
        except requests.exceptions.RequestException as e:
            logit.exception(f"USPS authentication request failed: {e}")
            raise USPSAuthenticationError(f"Failed to authenticate with USPS: {e}")

    def _store_tokens(self, token_data):
        """
        Store access token with expiration time

        Token data structure (client_credentials grant):
        {
            "access_token": "...",
            "token_type": "Bearer",
            "expires_in": 28800,  # 8 hours in seconds
            "status": "approved",
            "scope": "addresses",
            "issuer": "api.usps.com",
            "client_id": "...",
            ...
        }

        Note: refresh_token only comes from authorization_code grant, not client_credentials
        """
        access_token = token_data.get("access_token")

        if not access_token:
            raise USPSAuthenticationError("No access token in response")

        # Calculate expiration time
        now = datetime.now()

        # Access token expires in 8 hours (28800 seconds)
        access_expires_in = token_data.get("expires_in", 28800)
        self._access_token = access_token
        self._access_token_expires_at = now + timedelta(seconds=access_expires_in)

        logit.info(f"USPS token acquired, expires in {access_expires_in} seconds")

        return self._access_token

    def validate_address(self, address_data):
        """
        Validate address using USPS API v3
        Automatically handles token re-authentication

        Input format:
        {
            "address1": "123 Main St",
            "address2": "Apt 4B",  # optional
            "city": "Anytown",
            "state": "CA",
            "postal_code": "12345"  # optional
        }
        """
        url = f"{self.api_base_url}/addresses/v3/address"

        # Prepare query parameters (API uses GET method with query params)
        params = {
            "streetAddress": address_data["address1"],
            "state": address_data["state"],
        }

        # Add optional fields
        if address_data.get("address2"):
            params["secondaryAddress"] = address_data["address2"]
        if address_data.get("city"):
            params["city"] = address_data["city"]
        if address_data.get("postal_code"):
            params["ZIPCode"] = address_data["postal_code"]
        if address_data.get("firm"):
            params["firm"] = address_data["firm"]

        # Try validation with automatic token re-authentication
        max_retries = 2
        for attempt in range(max_retries):
            try:
                access_token = self.get_access_token()

                headers = {
                    "Authorization": f"Bearer {access_token}",
                }

                response = requests.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=10
                )

                # Handle 401 Unauthorized (token issue)
                if response.status_code == 401:
                    logit.warning("USPS: Received 401, clearing tokens and retrying")
                    # Clear tokens to force re-authentication
                    self._access_token = None
                    self._refresh_token = None
                    self._access_token_expires_at = None
                    self._refresh_token_expires_at = None

                    if attempt < max_retries - 1:
                        continue  # Retry
                    else:
                        raise USPSAPIError("Authentication failed after retry")

                # Handle other errors
                if response.status_code != 200:
                    error_detail = response.text
                    logit.error(f"USPS API error {response.status_code}: {error_detail}")
                    raise USPSAPIError(f"USPS API returned {response.status_code}: {error_detail}")

                # Success - parse and return
                return self._parse_response(response.json(), address_data)

            except requests.exceptions.Timeout:
                logit.error("USPS API timeout")
                raise USPSAPIError("USPS API request timed out")

            except requests.exceptions.RequestException as e:
                logit.error(f"USPS API request failed: {e}")
                raise USPSAPIError(f"USPS API request failed: {e}")

        # Should not reach here
        raise USPSAPIError("Failed to validate address after retries")

    def _parse_response(self, data, original_address):
        """
        Parse USPS JSON response and return standardized format

        Response structure:
        {
            "firm": "...",
            "address": {...},
            "additionalInfo": {...},
            "corrections": [...],  # array of {code, text}
            "matches": [...],      # array of {code, text}
            "warnings": [...]      # array of strings
        }
        """
        # Check for errors in response
        if "error" in data:
            error_msg = data.get("error", {}).get("message", "Unknown error")
            return {
                "valid": False,
                "error": f"USPS API error: {error_msg}",
                "original_address": original_address
            }

        address = data.get("address", {})
        additional_info = data.get("additionalInfo", {})

        # Corrections is an array of {code, text} objects
        corrections_list = data.get("corrections", [])
        matches_list = data.get("matches", [])
        warnings_list = data.get("warnings", [])

        # Check DPV (Delivery Point Validation)
        dpv_confirmation = additional_info.get("DPVConfirmation", "")

        if dpv_confirmation == "N":
            return {
                "valid": False,
                "error": "Address not found in USPS database",
                "original_address": original_address
            }
        elif dpv_confirmation == "D":
            return {
                "valid": False,
                "error": "Address is missing secondary information (apt, suite, etc.)",
                "original_address": original_address
            }
        elif dpv_confirmation != "Y":
            return {
                "valid": False,
                "error": "Address not deliverable (failed DPV check)",
                "original_address": original_address
            }

        # Check if vacant
        if additional_info.get("vacant") == "Y":
            return {
                "valid": False,
                "error": "Address appears to be vacant",
                "original_address": original_address
            }

        # Check for CMRA (Commercial Mail Receiving Agency - PO Box equivalents)
        if additional_info.get("DPVCMRA") == "Y":
            return {
                "valid": False,
                "error": "Commercial mail receiving agency (PO Box, UPS Store, etc.) not accepted",
                "original_address": original_address
            }

        # Determine if residential or business
        is_business = additional_info.get("business") == "Y"
        is_residential = not is_business

        # Parse corrections array for specific correction types
        correction_codes = [c.get("code") for c in corrections_list]

        # Parse matches array (code 31 = exact match)
        match_codes = [m.get("code") for m in matches_list]
        is_exact_match = "31" in match_codes

        # Code 32 = default address (needs more info like apt/suite)
        # Code 22 = multiple addresses found
        needs_secondary = "32" in correction_codes
        multiple_addresses = "22" in correction_codes

        # Build standardized response
        return {
            "valid": True,
            "source": "usps_v3",
            "standardized_address": {
                "line1": address.get("streetAddress", ""),
                "line1_abbreviated": address.get("streetAddressAbbreviation"),
                "line2": address.get("secondaryAddress") if address.get("secondaryAddress") else None,
                "city": address.get("city", ""),
                "city_abbreviated": address.get("cityAbbreviation"),
                "state": address.get("state", ""),
                "postal_code": address.get("ZIPCode", ""),
                "zip4": address.get("ZIPPlus4"),
                "full_zip": f"{address.get('ZIPCode')}-{address.get('ZIPPlus4')}" if address.get('ZIPPlus4') else address.get('ZIPCode'),
                "urbanization": address.get("urbanization")
            },
            "metadata": {
                "residential": is_residential,
                "business": is_business,
                "deliverable": True,
                "vacant": False,
                "carrier_route": additional_info.get("carrierRoute"),
                "delivery_point": additional_info.get("deliveryPoint"),
                "dpv_confirmation": dpv_confirmation,
                "cmra": additional_info.get("DPVCMRA") == "Y",
                "central_delivery_point": additional_info.get("centralDeliveryPoint") == "Y",
                "exact_match": is_exact_match,
                "needs_secondary_info": needs_secondary,
                "multiple_addresses_found": multiple_addresses
            },
            "corrections": {
                "corrections_applied": len(corrections_list) > 0,
                "correction_codes": correction_codes,
                "correction_details": [
                    {"code": c.get("code"), "description": c.get("text")}
                    for c in corrections_list
                ]
            },
            "matches": {
                "match_codes": match_codes,
                "match_details": [
                    {"code": m.get("code"), "description": m.get("text")}
                    for m in matches_list
                ]
            },
            "warnings": warnings_list,
            "firm": data.get("firm"),
            "original_address": original_address
        }

    def clear_tokens(self):
        """
        Manually clear all tokens (useful for testing or forcing re-authentication)
        """
        with self._token_lock:
            self._access_token = None
            self._access_token_expires_at = None

    def get_token_status(self):
        """
        Get current token status (useful for debugging/monitoring)
        """
        now = datetime.now()

        status = {
            "has_access_token": self._access_token is not None,
        }

        if self._access_token_expires_at:
            time_remaining = (self._access_token_expires_at - now).total_seconds()
            status["access_token_expires_in_seconds"] = max(0, int(time_remaining))
            status["access_token_expired"] = time_remaining <= 0

        return status


# Custom Exceptions
class USPSAuthenticationError(Exception):
    """Raised when authentication with USPS fails"""
    pass


class USPSAPIError(Exception):
    """Raised when USPS API request fails"""
    pass


validator = None

def validate_address(address):
    global validator
    if not validator:
        validator = USPSAddressValidator()
    return validator.validate_address(address)
