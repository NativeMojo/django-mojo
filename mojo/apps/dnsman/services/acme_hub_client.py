"""Narrow downstream transport for the protected ACME delegation hub.

This is not a DNS provider.  It can allocate one opaque delegation target and
publish or withdraw one immutable challenge lease through the fixed dnsman hub
endpoints.  It deliberately has no caller-selected record name or general DNS
CRUD surface.
"""

import ipaddress
import json
import math
import re
from urllib.parse import urlsplit, urlunsplit

import requests
from objict import objict

from mojo.helpers.settings import settings
from mojo.apps.dnsman.services.naming import normalize_domain


DEFAULT_CONNECT_TIMEOUT = 5
DEFAULT_READ_TIMEOUT = 30
DEFAULT_RETRIES = 1

MIN_CONNECT_TIMEOUT = 0.1
MAX_CONNECT_TIMEOUT = 30
MIN_READ_TIMEOUT = 0.1
MAX_READ_TIMEOUT = 120
MAX_RETRIES = 1
MAX_RESPONSE_BYTES = 16384
MAX_ACTIVE_VALUE_COUNT = 10000

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_VALUES = 20
_MAX_VALUE_LENGTH = 1024
_RETRY_STATUSES = frozenset((502, 503, 504))
_PATHS = {
    "allocate": "/api/dnsman/acme/delegation",
    "publish": "/api/dnsman/acme/challenge/publish",
    "withdraw": "/api/dnsman/acme/challenge/withdraw",
}


class AcmeHubClientError(Exception):
    """Base class for bounded, safe-to-persist downstream failures."""

    def __init__(self, message, kind, retriable=False, http_status=None):
        super().__init__(message)
        self.kind = kind
        self.retriable = retriable
        self.http_status = http_status


class AcmeHubConfigurationError(AcmeHubClientError):
    def __init__(self, message):
        super().__init__(message, "configuration")


class AcmeHubRequestError(AcmeHubClientError):
    def __init__(self, message):
        super().__init__(message, "request")


class AcmeHubTransportError(AcmeHubClientError):
    def __init__(self, message, retriable=True):
        super().__init__(message, "transport", retriable=retriable)


class AcmeHubResponseError(AcmeHubClientError):
    def __init__(self, message, http_status=None, retriable=False, kind="response"):
        super().__init__(
            message, kind, retriable=retriable, http_status=http_status)


class AcmeHubAllocation(objict):
    """Validated allocation returned by :func:`allocate`."""


class AcmeHubChallengeResult(objict):
    """Validated publish/withdraw success and active-value count."""


class _Config(object):
    __slots__ = ("base_url", "api_key", "connect_timeout", "read_timeout", "retries")

    def __init__(self, base_url, api_key, connect_timeout, read_timeout, retries):
        self.base_url = base_url
        self.api_key = api_key
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.retries = retries


def _bounded_number(name, default, minimum, maximum, integer=False):
    value = settings.get_static(name, default)
    if isinstance(value, bool):
        raise AcmeHubConfigurationError(f"{name} has an invalid value")
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError, OverflowError):
        raise AcmeHubConfigurationError(f"{name} has an invalid value")
    if not math.isfinite(parsed):
        raise AcmeHubConfigurationError(f"{name} has an invalid value")
    if parsed < minimum or parsed > maximum:
        raise AcmeHubConfigurationError(
            f"{name} must be between {minimum} and {maximum}")
    if integer and str(value).strip() not in (str(parsed), f"{parsed}.0"):
        raise AcmeHubConfigurationError(f"{name} must be an integer")
    return parsed


def _localhost(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _normalized_base_url(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise AcmeHubConfigurationError(
            "DNSMAN_ACME_HUB_URL must be configured as an HTTPS origin")
    if "\\" in value or any(char.isspace() or ord(char) < 32 for char in value):
        raise AcmeHubConfigurationError(
            "DNSMAN_ACME_HUB_URL must be configured as an HTTPS origin")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        raise AcmeHubConfigurationError(
            "DNSMAN_ACME_HUB_URL must be configured as an HTTPS origin")
    if (
        not host or not parsed.netloc
        or parsed.username is not None or parsed.password is not None
        or parsed.path not in ("", "/") or parsed.query or parsed.fragment
        or port == 0 or "%" in host
    ):
        raise AcmeHubConfigurationError(
            "DNSMAN_ACME_HUB_URL must be configured as an HTTPS origin")

    scheme = parsed.scheme.lower()
    if scheme != "https" and not (scheme == "http" and _localhost(host.lower())):
        raise AcmeHubConfigurationError(
            "DNSMAN_ACME_HUB_URL requires HTTPS except for localhost")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            ascii_host = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise AcmeHubConfigurationError(
                "DNSMAN_ACME_HUB_URL contains an invalid host")
        if ascii_host != "localhost":
            try:
                ascii_host = normalize_domain(ascii_host)
            except Exception:
                raise AcmeHubConfigurationError(
                    "DNSMAN_ACME_HUB_URL contains an invalid host")
        normalized_host = ascii_host
    else:
        normalized_host = address.compressed
        if address.version == 6:
            normalized_host = f"[{normalized_host}]"

    netloc = normalized_host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, "", "", ""))


def _api_key(value):
    if (
        not isinstance(value, str) or not value or len(value) > 4096
        or value != value.strip()
        or any(char.isspace() or ord(char) < 32 for char in value)
    ):
        raise AcmeHubConfigurationError(
            "DNSMAN_ACME_HUB_API_KEY is missing or invalid")
    return value


def _config():
    base_url = _normalized_base_url(
        settings.get_static("DNSMAN_ACME_HUB_URL", None))
    api_key = _api_key(settings.get_static("DNSMAN_ACME_HUB_API_KEY", None))
    connect_timeout = _bounded_number(
        "DNSMAN_ACME_HUB_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT,
        MIN_CONNECT_TIMEOUT, MAX_CONNECT_TIMEOUT)
    read_timeout = _bounded_number(
        "DNSMAN_ACME_HUB_READ_TIMEOUT", DEFAULT_READ_TIMEOUT,
        MIN_READ_TIMEOUT, MAX_READ_TIMEOUT)
    retries = _bounded_number(
        "DNSMAN_ACME_HUB_RETRIES", DEFAULT_RETRIES, 0, MAX_RETRIES,
        integer=True)
    return _Config(base_url, api_key, connect_timeout, read_timeout, retries)


def is_available():
    """Return false only when both required settings are absent.

    Partial or unsafe configuration remains loud so a deployment typo cannot
    masquerade as an intentionally disabled capability.
    """
    url = settings.get_static("DNSMAN_ACME_HUB_URL", None)
    key = settings.get_static("DNSMAN_ACME_HUB_API_KEY", None)
    if not url and not key:
        return False
    _config()
    return True


def _reference(value, label):
    if not isinstance(value, str) or not _REF_RE.match(value):
        raise AcmeHubRequestError(
            f"{label} must be 1-128 safe identifier characters")
    return value


def _domain(value):
    try:
        return normalize_domain(value)
    except Exception:
        raise AcmeHubRequestError("domain must be a valid fully qualified name")


def _record_values(values):
    if not isinstance(values, list) or not values or len(values) > _MAX_VALUES:
        raise AcmeHubRequestError(
            f"record_values must be a non-empty list of at most {_MAX_VALUES} strings")
    result = []
    for value in values:
        if not isinstance(value, str) or not value or len(value) > _MAX_VALUE_LENGTH:
            raise AcmeHubRequestError(
                f"each record value must be 1-{_MAX_VALUE_LENGTH} characters")
        if "\x00" in value or "\r" in value or "\n" in value:
            raise AcmeHubRequestError("record values cannot contain control characters")
        if value not in result:
            result.append(value)
    return sorted(result)


def _http_error(status):
    if status in (401, 403):
        message = f"ACME hub authentication was rejected (HTTP {status})"
    elif status == 400:
        message = "ACME hub rejected invalid challenge data (HTTP 400)"
    elif status == 409:
        message = "ACME hub rejected a conflicting immutable reference (HTTP 409)"
    elif status == 429:
        message = "ACME hub rate limit rejected the request (HTTP 429)"
    elif status >= 500:
        message = f"ACME hub is unavailable (HTTP {status})"
    else:
        message = f"ACME hub rejected the request (HTTP {status})"
    return AcmeHubResponseError(
        message, http_status=status, retriable=status in _RETRY_STATUSES,
        kind="http")


def _response_data(response):
    content_type = str(response.headers.get("Content-Type", "")).lower()
    if not content_type.startswith("application/json"):
        raise AcmeHubResponseError("ACME hub returned a non-JSON response")
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            declared = int(declared)
        except (TypeError, ValueError):
            raise AcmeHubResponseError("ACME hub returned an invalid response envelope")
        if declared < 0 or declared > MAX_RESPONSE_BYTES:
            raise AcmeHubResponseError("ACME hub response exceeded the size limit")
    chunks = []
    size = 0
    try:
        iterator = response.iter_content(chunk_size=4096)
        for chunk in iterator:
            if not isinstance(chunk, (bytes, bytearray)):
                raise AcmeHubResponseError(
                    "ACME hub returned an invalid response envelope")
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise AcmeHubResponseError(
                    "ACME hub response exceeded the size limit")
            chunks.append(bytes(chunk))
    except AcmeHubClientError:
        raise
    except requests.exceptions.RequestException:
        raise AcmeHubTransportError(
            "ACME hub response timed out or lost its connection")
    except Exception:
        raise AcmeHubResponseError("ACME hub response could not be read")
    content = b"".join(chunks)
    try:
        envelope = json.loads(content)
    except (TypeError, ValueError, UnicodeError):
        raise AcmeHubResponseError("ACME hub returned malformed JSON")
    if not isinstance(envelope, dict):
        raise AcmeHubResponseError("ACME hub returned an invalid response envelope")

    if envelope.get("status") is not True:
        status = envelope.get("code")
        if isinstance(status, int) and not isinstance(status, bool) and 400 <= status <= 599:
            raise _http_error(status)
        raise AcmeHubResponseError("ACME hub returned an unsuccessful response envelope")
    allowed = {"status", "code", "data", "server"}
    if set(envelope) - allowed:
        raise AcmeHubResponseError("ACME hub returned an invalid response envelope")
    code = envelope.get("code", 200)
    if code != 200 or isinstance(code, bool) or not isinstance(envelope.get("data"), dict):
        raise AcmeHubResponseError("ACME hub returned an invalid response envelope")
    server = envelope.get("server")
    if server is not None and (not isinstance(server, str) or len(server) > 255):
        raise AcmeHubResponseError("ACME hub returned an invalid response envelope")
    return envelope["data"]


def _post(operation, payload):
    config = _config()
    url = f"{config.base_url}{_PATHS[operation]}"
    headers = {
        "Authorization": f"apikey {config.api_key}",
        "Accept": "application/json",
    }
    timeout = (config.connect_timeout, config.read_timeout)
    attempts = config.retries + 1
    for attempt in range(attempts):
        try:
            response = requests.post(
                url, json=payload, headers=headers, timeout=timeout,
                allow_redirects=False, stream=True)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt + 1 < attempts:
                continue
            raise AcmeHubTransportError("ACME hub request timed out or lost its connection")
        except requests.exceptions.RequestException:
            raise AcmeHubTransportError("ACME hub request could not be sent", retriable=False)

        try:
            status = response.status_code
            if isinstance(status, bool) or not isinstance(status, int):
                raise AcmeHubResponseError("ACME hub returned an invalid HTTP status")
            if status in _RETRY_STATUSES and attempt + 1 < attempts:
                continue
            if 300 <= status <= 399:
                raise AcmeHubResponseError(
                    "ACME hub refused an unexpected redirect", http_status=status,
                    kind="redirect")
            if status != 200:
                raise _http_error(status)
            try:
                return _response_data(response)
            except AcmeHubTransportError:
                if attempt + 1 < attempts:
                    continue
                raise
        finally:
            response.close()
    raise AcmeHubTransportError("ACME hub request failed")


def _allocation(data, expected_client_ref, expected_domain=None,
                expected_challenge_ref=None, include_count=False):
    expected_keys = {"client_ref", "domain", "source", "target"}
    if expected_challenge_ref is not None:
        expected_keys.add("challenge_ref")
    if include_count:
        expected_keys.add("active_value_count")
    if set(data) != expected_keys:
        raise AcmeHubResponseError("ACME hub returned an unexpected response shape")
    if data.get("client_ref") != expected_client_ref:
        raise AcmeHubResponseError("ACME hub returned a cross-request client reference")
    if expected_challenge_ref is not None and data.get("challenge_ref") != expected_challenge_ref:
        raise AcmeHubResponseError("ACME hub returned a cross-request challenge reference")

    domain = _response_domain(data.get("domain"), "domain")
    if expected_domain is not None and domain != expected_domain:
        raise AcmeHubResponseError("ACME hub returned a cross-request domain")
    source = data.get("source")
    if source != f"_acme-challenge.{domain}":
        raise AcmeHubResponseError("ACME hub returned an invalid challenge source")
    target = _response_domain(data.get("target"), "target")
    try:
        ipaddress.ip_address(target)
    except ValueError:
        pass
    else:
        raise AcmeHubResponseError("ACME hub returned an invalid target FQDN")
    if target == source:
        raise AcmeHubResponseError("ACME hub returned a non-distinct challenge target")

    result_class = (
        AcmeHubChallengeResult if expected_challenge_ref is not None
        else AcmeHubAllocation)
    result = result_class(
        success=True,
        client_ref=expected_client_ref,
        domain=domain,
        source=source,
        target=target,
    )
    if expected_challenge_ref is not None:
        result.challenge_ref = expected_challenge_ref
    if include_count:
        count = data.get("active_value_count")
        if (
            isinstance(count, bool) or not isinstance(count, int)
            or count < 0 or count > MAX_ACTIVE_VALUE_COUNT
        ):
            raise AcmeHubResponseError("ACME hub returned an invalid active value count")
        result.active_value_count = count
    return result


def _response_domain(value, label):
    try:
        normalized = normalize_domain(value)
    except Exception:
        raise AcmeHubResponseError(f"ACME hub returned an invalid {label} FQDN")
    if value != normalized:
        raise AcmeHubResponseError(f"ACME hub returned a non-normalized {label} FQDN")
    return normalized


def allocate(domain, client_ref):
    """Allocate and validate one permanent source/target delegation."""
    domain = _domain(domain)
    client_ref = _reference(client_ref, "client_ref")
    data = _post("allocate", {"domain": domain, "client_ref": client_ref})
    return _allocation(data, client_ref, expected_domain=domain)


def publish(client_ref, challenge_ref, record_values):
    """Publish the complete value set for one immutable challenge lease."""
    client_ref = _reference(client_ref, "client_ref")
    challenge_ref = _reference(challenge_ref, "challenge_ref")
    record_values = _record_values(record_values)
    data = _post("publish", {
        "client_ref": client_ref,
        "challenge_ref": challenge_ref,
        "values": record_values,
    })
    return _allocation(
        data, client_ref, expected_challenge_ref=challenge_ref,
        include_count=True)


def withdraw(client_ref, challenge_ref):
    """Idempotently withdraw one challenge lease."""
    client_ref = _reference(client_ref, "client_ref")
    challenge_ref = _reference(challenge_ref, "challenge_ref")
    data = _post("withdraw", {
        "client_ref": client_ref,
        "challenge_ref": challenge_ref,
    })
    return _allocation(
        data, client_ref, expected_challenge_ref=challenge_ref,
        include_count=True)
