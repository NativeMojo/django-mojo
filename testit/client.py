import requests
import base64
from objict import objict


class RestClient:
    """
    A simple REST client for making HTTP requests to a specified host.

    Uses requests.Session internally so cookies set by the server (e.g. _muid,
    _msid, mbp) are automatically persisted and sent on subsequent requests —
    just like a real browser. This is essential for testing server-controlled
    identity cookies (bouncer, session tracking, etc.).
    """

    # Default headers that emulate a real browser. Without these, server-side
    # signal analysis (bouncer EnvironmentService, etc.) flags every test request
    # as missing Accept/Accept-Language — inflating risk scores and producing
    # results that don't match production behavior.
    DEFAULT_HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
    }

    def __init__(self, host, logger=None):
        """
        Initializes the RestClient with a host URL.

        Uses requests.Session so cookies set by the server (e.g. _muid, _msid,
        mbp) are automatically persisted and sent on subsequent requests — just
        like a real browser.

        Args:
            host (str): The base URL of the host for making requests.
        """
        self.host = host if host[-1] == "/" else f"{host}/"
        self.logger = logger
        self.session = requests.Session()
        self.session.headers.update(self.DEFAULT_HEADERS)
        self.access_token = None
        self.is_authenticated = False
        self.bearer = "bearer"
        self.headers = {}

    def login(self, username, password):
        self.logout()
        resp = self.post("/api/login", dict(username=username, password=password))
        if resp.response.data and resp.response.data.access_token:
            self.is_authenticated = True
            self.access_token = resp.response.data.access_token
            junk, self.jwt_data = decode_jwt(self.access_token)
        return self.is_authenticated

    def logout(self):
        self.is_authenticated = False
        self.bearer = "bearer"
        self.access_token = None
        if "Authorization" in self.headers:
            del self.headers["Authorization"]
        # Keep cookies (device identity persists across logouts, like a real browser).
        # Call clear_cookies() to simulate a fresh browser with no history.

    def clear_cookies(self):
        """Clear all cookies — simulates a fresh browser with no history."""
        self.session.cookies.clear()

    def get_headers(self):
        if self.is_authenticated:
            self.headers["Authorization"] = f"{self.bearer} {self.access_token}"
        return self.headers

    def _make_request(self, method, path, **kwargs):
        """
        Makes an HTTP request using the specified method and path.

        Args:
            method (str): The HTTP method to use for the request (e.g., 'GET', 'POST').
            path (str): The endpoint path to append to the base host URL.
            **kwargs: Additional arguments to pass to the request (e.g., headers, params).

        Returns:
            dict: A dictionary containing the response data and status code. If an error occurs,
                  returns a dictionary with an error message instead.
        """
        if path[0] == "/":
            path = path[1:]
        url = f"{self.host}{path}"
        headers = self.get_headers()
        response = self.session.request(method, url, headers=headers, **kwargs)
        if self.logger:
            self.logger.info("REQUEST", f"{method}:{url}", headers)
            self.logger.info("params:",kwargs.get("params", ""), "json:", kwargs.get("json", ""))
        try:
            if response.content:
                try:
                    data = objict.fromdict(response.json())
                    response_data = objict(response=data, status_code=response.status_code, json=data)
                except ValueError:
                    # Not JSON, try to get text or HTML
                    text_data = response.text
                    response_data = objict(response=text_data, status_code=response.status_code, text=text_data)
            else:
                data = None
                response_data = objict(response=data, status_code=response.status_code, json=data)
            if not response.ok:
                response_data['error_reason'] = response.reason
            if self.logger:
                self.logger.info("RESPONSE", f"{method}:{url}")
                self.logger.info(response_data)
            return response_data
        except Exception as e:
            if self.logger:
                self.logger.error("RESPONSE", f"{method}:{url}")
                self.logger.exception(str(e), response.text)
            return objict(error=str(e), text=response.text)

    def get(self, path, **kwargs):
        """
        Sends a GET request to the specified path.

        Args:
            path (str): The endpoint path to append to the base host URL.
            **kwargs: Additional arguments to pass to the request (e.g., headers, params).

        Returns:
            dict: A dictionary containing the response data and status code.
        """
        return self._make_request('GET', path, **kwargs)

    def post(self, path, json=None, **kwargs):
        """
        Sends a POST request to the specified path.

        Args:
            path (str): The endpoint path to append to the base host URL.
            json (dict, optional): The JSON data to include in the request body.
            **kwargs: Additional arguments to pass to the request (e.g., headers).

        Returns:
            dict: A dictionary containing the response data and status code.
        """
        return self._make_request('POST', path, json=json, **kwargs)

    def put(self, path, json=None, **kwargs):
        """
        Sends a PUT request to the specified path.

        Args:
            path (str): The endpoint path to append to the base host URL.
            json (dict, optional): The JSON data to include in the request body.
            **kwargs: Additional arguments to pass to the request (e.g., headers).

        Returns:
            dict: A dictionary containing the response data and status code.
        """
        return self._make_request('PUT', path, json=json, **kwargs)

    def delete(self, path, **kwargs):
        """
        Sends a DELETE request to the specified path.

        Args:
            path (str): The endpoint path to append to the base host URL.
            **kwargs: Additional arguments to pass to the request (e.g., headers).

        Returns:
            dict: A dictionary containing the response data and status code.
        """
        return self._make_request('DELETE', path, **kwargs)



def base64_decode(data):
    """Decode base64-encoded data."""
    padding = '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def decode_jwt(token):
    """Decode a JWT token using base64 decoding."""
    headers, payload, signature = token.split('.')
    decoded_headers = objict.fromJSON(base64_decode(headers))
    decoded_payload = objict.fromJSON(base64_decode(payload))
    return decoded_headers, decoded_payload
