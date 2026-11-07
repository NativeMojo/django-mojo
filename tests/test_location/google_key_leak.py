"""
The Google Maps API key never reaches a log or an HTTP body when a request to
Google fails (maestro #9921).

A real GoogleAddressService is pointed at closed loopback (127.0.0.1:1), so
requests raises a real ConnectionError whose text carries the key-bearing URL.
A test-owned handler on the `error` and `mojo` loggers captures the formatted
records, traceback included. No real network.
"""
import logging
import traceback
from contextlib import contextmanager
from unittest import mock

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

KEY = "DUMMYKEY-9921"
CLOSED = "http://127.0.0.1:1"
LOGGERS = ("error", "mojo")


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))

    @property
    def text(self):
        return "\n".join(self.lines)


@contextmanager
def _captured():
    handler = _Capture()
    loggers = [logging.getLogger(name) for name in LOGGERS]
    for logger in loggers:
        logger.addHandler(handler)
    try:
        yield handler
    finally:
        for logger in loggers:
            logger.removeHandler(handler)


def _service():
    from mojo.helpers.location.google import GoogleAddressService
    service = GoogleAddressService(use_service_account=False)
    service.api_key = KEY
    service.validation_url = f"{CLOSED}/v1"
    service.autocomplete_url = f"{CLOSED}/maps/api/place/autocomplete/json"
    service.place_details_url = f"{CLOSED}/maps/api/place/details/json"
    service.geocoding_url = f"{CLOSED}/maps/api/geocode/json"
    service.timezone_url = f"{CLOSED}/maps/api/timezone/json"
    return service


CALLS = {
    "validate_address": lambda s: s.validate_address(
        {"address1": "1 Main St", "city": "Anytown", "state": "CA"}),
    "get_address_suggestions": lambda s: s.get_address_suggestions("1600 Amph"),
    "get_place_details": lambda s: s.get_place_details("ChIJplace"),
    "geocode_address": lambda s: s.geocode_address("1 Main St, Anytown, CA"),
    "reverse_geocode": lambda s: s.reverse_geocode(37.4, -122.0),
    "get_timezone": lambda s: s.get_timezone(37.4, -122.0, 1700000000),
}


def _raise(name, service):
    from mojo.helpers.location.google import GoogleAPIError
    try:
        CALLS[name](service)
    except GoogleAPIError as err:
        return err
    raise AssertionError(f"{name} must raise GoogleAPIError on a failed request")


def _assert_clean(name, err, log):
    rendered = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    assert_true(KEY not in str(err), f"{name}: the key must not be in the error text: {err}")
    assert_true(KEY not in rendered, f"{name}: the key must not be in the formatted exception chain")
    assert_true(KEY not in log, f"{name}: the key must not reach the log: {log}")


def _connection_failure(name):
    service = _service()
    with _captured() as cap:
        err = _raise(name, service)
    _assert_clean(name, err, cap.text)
    assert_true("class=ConnectionError" in cap.text,
                f"{name}: the log keeps the failure class for triage: {cap.text}")
    assert_true("host=127.0.0.1" in cap.text,
                f"{name}: the log keeps the endpoint host for triage: {cap.text}")


@th.django_unit_test()
def test_validate_address_connection_failure_hides_key(opts):
    _connection_failure("validate_address")


@th.django_unit_test()
def test_suggestions_connection_failure_hides_key(opts):
    _connection_failure("get_address_suggestions")


@th.django_unit_test()
def test_place_details_connection_failure_hides_key(opts):
    _connection_failure("get_place_details")


@th.django_unit_test()
def test_geocode_connection_failure_hides_key(opts):
    _connection_failure("geocode_address")


@th.django_unit_test()
def test_reverse_geocode_connection_failure_hides_key(opts):
    _connection_failure("reverse_geocode")


@th.django_unit_test()
def test_timezone_connection_failure_hides_key(opts):
    _connection_failure("get_timezone")


@th.django_unit_test()
def test_timeout_hides_key(opts):
    """The four methods with no Timeout branch: a timeout whose text carries
    the key-bearing URL must not leak it either."""
    import requests
    from mojo.helpers.location import google

    def timeout(url, **kwargs):
        raise requests.exceptions.ReadTimeout(
            f"HTTPSConnectionPool: Read timed out for url: {url}?key={KEY}")

    fake = mock.Mock(exceptions=requests.exceptions, get=timeout, post=timeout)
    for name in ("get_place_details", "geocode_address", "reverse_geocode", "get_timezone"):
        with mock.patch.object(google, "requests", fake), _captured() as cap:
            err = _raise(name, _service())
        _assert_clean(name, err, cap.text)


@th.django_unit_test()
def test_address_validation_sends_key_in_header(opts):
    headers, params = _service()._get_auth_headers_and_params()
    assert_eq(headers.get("X-Goog-Api-Key"), KEY, f"the key goes in X-Goog-Api-Key: {headers}")
    assert_true("key" not in params, f"the key must not be a URL parameter: {params}")


# The seven public routes, called in-process with the singleton swapped for the
# closed-loopback service. Status codes must stay as they are.
ROUTES = (
    ("rest_validate_address",
     {"address1": "1 Main St", "city": "Anytown", "state": "CA", "provider": "google"}, 200),
    ("rest_validate_address_sample", {}, 200),
    ("rest_address_suggestions", {"input": "1600 Amph"}, 400),
    ("rest_place_details", {"place_id": "ChIJplace"}, 400),
    ("rest_geocode_address", {"address": "1 Main St, Anytown, CA"}, 400),
    ("rest_reverse_geocode", {"lat": "37.4", "lng": "-122.0"}, 400),
    ("rest_get_timezone", {"lat": "37.4", "lng": "-122.0"}, 400),
)


def _call_route(name, data):
    from types import SimpleNamespace
    from objict import objict
    import importlib
    rest_location = importlib.import_module("mojo.rest.location")
    request = SimpleNamespace(DATA=objict(data), user=None, method="GET")
    return getattr(rest_location, name)(request)


def _route(name, data, status):
    from mojo.helpers.location import google
    with mock.patch.object(google, "google_api", _service()), _captured() as cap:
        resp = _call_route(name, data)
    body = resp.content.decode("utf-8")
    assert_true(KEY not in body, f"{name}: the key must not be in the response body: {body}")
    assert_true(KEY not in cap.text, f"{name}: the key must not reach the log: {cap.text}")
    assert_eq(resp.status_code, status, f"{name}: status stays {status}, got {resp.status_code}: {body}")
    return resp


@th.django_unit_test()
def test_route_validate_hides_key(opts):
    _route(*ROUTES[0])


@th.django_unit_test()
def test_route_validate_sample_hides_key(opts):
    _route(*ROUTES[1])


@th.django_unit_test()
def test_route_suggestions_hides_key(opts):
    _route(*ROUTES[2])


@th.django_unit_test()
def test_route_place_details_hides_key(opts):
    _route(*ROUTES[3])


@th.django_unit_test()
def test_route_geocode_hides_key(opts):
    _route(*ROUTES[4])


@th.django_unit_test()
def test_route_reverse_geocode_hides_key(opts):
    _route(*ROUTES[5])


@th.django_unit_test()
def test_route_timezone_hides_key(opts):
    _route(*ROUTES[6])
