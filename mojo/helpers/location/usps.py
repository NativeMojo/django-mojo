import requests
from datetime import datetime, timedelta
from mojo.helpers import logit
from mojo.helpers.settings import settings
import threading


class USPSAddressValidator:
    """
    USPS API v3 Address Validator with local token management

    Token Lifecycle:
    - Access Token: Valid for 8 hours
    - Refresh Token: Valid for 7 days
    - Tokens stored locally in instance
    - Automatically refreshes access token when expired
    - Re-authenticates when refresh token expires

    Note: Each instance maintains its own tokens. Multiple instances
    (e.g., multiple Django workers) will each authenticate separately.
    This is fine - USPS allows multiple concurrent sessions.
    """

    def __init__(self):
        self.client_id = settings.USPS_CLIENT_ID
        self.client_secret = settings.USPS_CLIENT_SECRET
        self.token_url = "https://api.usps.com/oauth2/v3/token"
        self.api_base_url = "https://api.usps.com"

        # Token storage (instance variables)
        self._access_token = None
        self._refresh_token = None
        self._access_token_expires_at = None
        self._refresh_token_expires_at = None

        # Thread lock for token refresh (prevent race conditions in same instance)
        self._token_lock = threading.Lock()

    def get_access_token(self):
        """
        Get valid access token, refreshing if needed

        Flow:
        1. Check if we have a valid access token
        2. If expired, try to refresh using refresh token
        3. If refresh fails or refresh token expired, re-authenticate
        """
        with self._token_lock:
            # Check if we have a valid access token
            if self._access_token and self._access_token_expires_at:
                # Check if token is still valid (with 5 min buffer)
                if datetime.now() < (self._access_token_expires_at - timedelta(minutes=5)):
                    return self._access_token

            # Access token expired or doesn't exist, try refresh
            if self._refresh_token and self._refresh_token_expires_at:
                # Check if refresh token is still valid (with 1 hour buffer)
                if datetime.now() < (self._refresh_token_expires_at - timedelta(hours=1)):
                    try:
                        return self._refresh_access_token()
                    except Exception as e:
                        return self._authenticate()
                else:
                    return self._authenticate()
            else:
                return self._authenticate()

    def _authenticate(self):
        """
        Authenticate with client credentials (initial login)
        Returns access token and stores both access and refresh tokens
        """
        logger.info("Authenticating with USPS API")

        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "addresses"
        }

        try:
            response = requests.post(
                self.token_url,
                data=data,
                timeout=10
            )
            response.raise_for_status()

            token_data = response.json()
            return self._store_tokens(token_data)

        except requests.exceptions.RequestException as e:
            logit.exception(f"USPS authentication failed: {e}")
            raise USPSAuthenticationError(f"Failed to authenticate with USPS: {e}")

    def _refresh_access_token(self):
        """
        Refresh access token using refresh token
        """

        data = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self._refresh_token
        }

        try:
            response = requests.post(
                self.token_url,
                data=data,
                timeout=10
            )
            response.raise_for_status()

            token_data = response.json()
            return self._store_tokens(token_data)

        except requests.exceptions.RequestException as e:
            logit.exception(f"USPS token refresh failed: {e}")
            raise USPSTokenRefreshError(f"Failed to refresh token: {e}")

    def _store_tokens(self, token_data):
        """
        Store access and refresh tokens with expiration times

        Token data structure:
        {
            "access_token": "...",
            "refresh_token": "...",
            "token_type": "Bearer",
            "expires_in": 28800,  # 8 hours in seconds
            "refresh_token_expires_in": 604800  # 7 days in seconds
        }
        """
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")

        if not access_token:
            raise USPSAuthenticationError("No access token in response")

        # Calculate expiration times
        now = datetime.now()

        # Access token expires in 8 hours (28800 seconds)
        access_expires_in = token_data.get("expires_in", 28800)
        self._access_token = access_token
        self._access_token_expires_at = now + timedelta(seconds=access_expires_in)

        # Refresh token expires in 7 days (604800 seconds)
        if refresh_token:
            refresh_expires_in = token_data.get("refresh_token_expires_in", 604800)
            self._refresh_token = refresh_token
            self._refresh_token_expires_at = now + timedelta(seconds=refresh_expires_in)

        return self._access_token

    def validate_address(self, address_data):
        """
        Validate address using USPS API v3
        Automatically handles token refresh
        {
            "address1": "123 Main St",
            "address2": "Apt 4B",
            "city": "Anytown",
            "state": "CA",
            "zip": "12345"
        }
        """
        url = f"{self.api_base_url}/addresses/v3/address"

        # Prepare payload
        payload = {
            "streetAddress": address_data["address1"],
            "city": address_data["city"],
            "state": address_data["state"],
        }

        # Add optional fields
        if address_data.get("address2"):
            payload["secondaryAddress"] = address_data["address2"]
        if address_data.get("zip"):
            payload["ZIPCode"] = address_data["zip"]

        # Try validation with automatic token refresh
        max_retries = 2
        for attempt in range(max_retries):
            try:
                access_token = self.get_access_token()

                headers = {
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                }

                response = requests.post(
                    url,
                    json=payload,
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
        additional_info = data.get("addressAdditionalInfo", {})
        corrections = data.get("addressCorrections", {})

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

        # Build standardized response
        return {
            "valid": True,
            "source": "usps_v3",
            "standardized_address": {
                "line1": address.get("streetAddress", ""),
                "line2": address.get("secondaryAddress") if address.get("secondaryAddress") else None,
                "city": address.get("city", ""),
                "state": address.get("state", ""),
                "zip": address.get("ZIPCode", ""),
                "zip4": address.get("ZIPPlus4"),
                "full_zip": f"{address.get('ZIPCode')}-{address.get('ZIPPlus4')}" if address.get('ZIPPlus4') else address.get('ZIPCode')
            },
            "metadata": {
                "residential": is_residential,
                "business": is_business,
                "deliverable": True,
                "vacant": False,
                "carrier_route": additional_info.get("carrierRoute"),
                "delivery_point": additional_info.get("deliveryPoint"),
                "dpv_confirmation": dpv_confirmation,
                "cmra": additional_info.get("DPVCMRA") == "Y"
            },
            "corrections": {
                "address_corrected": corrections.get("addressCorrected", False),
                "street_corrected": corrections.get("streetAddressCorrected", False),
                "city_state_corrected": corrections.get("cityStateCorrected", False),
                "zip_corrected": corrections.get("ZIPCorrected", False),
                "zip4_corrected": corrections.get("ZIPPlus4Corrected", False)
            },
            "original_address": original_address
        }

    def clear_tokens(self):
        """
        Manually clear all tokens (useful for testing or forcing re-authentication)
        """
        with self._token_lock:
            self._access_token = None
            self._refresh_token = None
            self._access_token_expires_at = None
            self._refresh_token_expires_at = None

    def get_token_status(self):
        """
        Get current token status (useful for debugging/monitoring)
        """
        now = datetime.now()

        status = {
            "has_access_token": self._access_token is not None,
            "has_refresh_token": self._refresh_token is not None,
        }

        if self._access_token_expires_at:
            time_remaining = (self._access_token_expires_at - now).total_seconds()
            status["access_token_expires_in_seconds"] = max(0, int(time_remaining))
            status["access_token_expired"] = time_remaining <= 0

        if self._refresh_token_expires_at:
            time_remaining = (self._refresh_token_expires_at - now).total_seconds()
            status["refresh_token_expires_in_seconds"] = max(0, int(time_remaining))
            status["refresh_token_expired"] = time_remaining <= 0

        return status


# Custom Exceptions
class USPSAuthenticationError(Exception):
    """Raised when authentication with USPS fails"""
    pass


class USPSTokenRefreshError(Exception):
    """Raised when token refresh fails"""
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
