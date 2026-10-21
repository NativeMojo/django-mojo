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
import uuid
from datetime import datetime
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
    "capabilities": "/api/dnsman/acme/capabilities",
    "lookup": "/api/dnsman/acme/consumer/lookup",
    "status": "/api/dnsman/acme/consumer/status",
    "join": "/api/dnsman/acme/consumer/join",
    "list_allocations": "/api/dnsman/acme/allocation/list",
    "list_participants": "/api/dnsman/acme/trust/list",
    "resolve_recipient": "/api/dnsman/acme/trust/recipient",
    "grant": "/api/dnsman/acme/trust/grant",
    "revoke": "/api/dnsman/acme/trust/revoke",
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
    from mojo.apps.dnsman.fleet_settings import normalize_origin
    try:
        return normalize_origin(value)
    except ValueError:
        raise AcmeHubConfigurationError(
            "DNSMAN_ACME_HUB_URL must be configured as an HTTPS origin") from None


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
    if not isinstance(value, str) or not _REF_RE.fullmatch(value):
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


_SAFE_ERRORS = {
    "acme_invalid_request": 400, "acme_not_authorized": (403, 404),
    "acme_trust_revoked": 403, "acme_reference_conflict": 409,
    "acme_alias_mismatch": 400, "acme_alias_lookup_failed": 503,
    "acme_hub_unavailable": 503,
}
_SHARED_OPERATIONS = frozenset(_PATHS) - {"allocate", "publish", "withdraw"}
MAX_SHARED_RESPONSE_BYTES = 65536


def _http_error(status, token=None):
    allowed = _SAFE_ERRORS.get(token) if isinstance(token, str) else None
    if allowed == status or isinstance(allowed, tuple) and status in allowed:
        return AcmeHubResponseError(token, http_status=status,
                                    retriable=status in _RETRY_STATUSES, kind=token)
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


def _response_data(response, max_bytes=MAX_RESPONSE_BYTES):
    content_type = str(response.headers.get("Content-Type", "")).lower()
    if not content_type.startswith("application/json"):
        raise AcmeHubResponseError("ACME hub returned a non-JSON response")
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            declared = int(declared)
        except (TypeError, ValueError):
            raise AcmeHubResponseError("ACME hub returned an invalid response envelope")
        if declared < 0 or declared > max_bytes:
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
            if size > max_bytes:
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
            raise _http_error(status, envelope.get("error"))
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
    if operation in {"publish", "withdraw", "cleanup"}:
        from mojo.apps.dnsman.services.challenge_dns import outgoing_hub
        return outgoing_hub(operation, payload, config, lambda: _wire(operation, payload, config, requests.post))
    return _wire(operation, payload, config, requests.post)


def _wire(operation, payload, config, post):
    url = f"{config.base_url}{_PATHS[operation]}"
    headers = {
        "Authorization": f"apikey {config.api_key}",
        "Accept": "application/json",
    }
    timeout = (config.connect_timeout, config.read_timeout)
    attempts = 1 if operation in {"publish", "withdraw", "cleanup"} else config.retries + 1
    for attempt in range(attempts):
        try:
            response = post(
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
            if operation == "capabilities" and status == 404:
                raise AcmeHubResponseError("Shared ACME validation is unsupported by this hub",
                                          http_status=404, kind="shared_validation_unsupported")
            if status != 200:
                # Read only bounded JSON to decode fixed machine tokens. Remote
                # prose/HTML is never included in an exception or persisted.
                try:
                    _response_data(response)
                except AcmeHubResponseError as exc:
                    if exc.http_status == status and exc.kind in _SAFE_ERRORS:
                        raise
                except AcmeHubTransportError:
                    pass
                raise _http_error(status)
            try:
                return _response_data(response, MAX_SHARED_RESPONSE_BYTES if operation in _SHARED_OPERATIONS else MAX_RESPONSE_BYTES)
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
    if not isinstance(value, str) or len(value) > 253:
        raise AcmeHubResponseError(f"ACME hub returned an invalid {label} FQDN")
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


class AcmeHubParticipant(objict):
    """Strict v2 consumer (or controller) participant data."""


class AcmeHubPage(objict):
    """At most twenty validated items and a bounded opaque-to-callers cursor."""


def _shape(data, keys):
    if not isinstance(data, dict) or set(data) != set(keys):
        raise AcmeHubResponseError("ACME hub returned an unexpected response shape")


def _response_uuid(value):
    if not isinstance(value, str) or len(value) not in (32, 36):
        raise AcmeHubResponseError("ACME hub returned an invalid UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise AcmeHubResponseError("ACME hub returned an invalid UUID")
    if value not in (parsed.hex, str(parsed)):
        raise AcmeHubResponseError("ACME hub returned an invalid UUID")
    return value


def _request_uuid(value):
    try:
        return _response_uuid(value)
    except AcmeHubResponseError:
        raise AcmeHubRequestError("a valid project or participant UUID is required")


def _timestamp(value):
    try:
        if not isinstance(value, str) or not value or len(value) > 40:
            raise ValueError()
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError):
        raise AcmeHubResponseError("ACME hub returned an invalid timestamp")
    return value


def _page_cursor(value, response=False):
    if value is None:
        return None
    if (not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value)
            or int(value) > 9223372036854775807):
        error = AcmeHubResponseError if response else AcmeHubRequestError
        raise error("ACME hub cursor must be a positive decimal identifier")
    return value


def _participant(data, controller=False, expected_client_ref=None, expected_domain=None,
                 expected_allocation_ref=None, expected_participant_ref=None, status=False,
                 expected_consumer_uuid=None):
    keys = {"allocation_ref", "participant_ref", "client_ref", "domain", "source", "target",
            "role", "trust_state", "scope", "can_publish", "can_withdraw", "revoked_at"}
    if controller:
        keys.update(("consumer_project_uuid", "consumer_label"))
    if status:
        keys.add("alias")
    _shape(data, keys)
    try:
        client_ref = _reference(data["client_ref"], "client_ref")
    except AcmeHubRequestError:
        raise AcmeHubResponseError("ACME hub returned an invalid client reference")
    allocation = _allocation({key: data[key] for key in ("client_ref", "domain", "source", "target")},
                             expected_client_ref or client_ref, expected_domain=expected_domain)
    if data["allocation_ref"] != allocation.target or (
            expected_allocation_ref is not None and data["allocation_ref"] != expected_allocation_ref):
        raise AcmeHubResponseError("ACME hub returned a cross-request allocation")
    _response_uuid(data["participant_ref"])
    if expected_participant_ref is not None and data["participant_ref"] != expected_participant_ref:
        raise AcmeHubResponseError("ACME hub returned a cross-request participant")
    if (data["role"] not in ("owner", "trusted") or data["trust_state"] not in ("active", "revoked")
            or data["scope"] != [allocation.domain, f"*.{allocation.domain}"]
            or type(data["can_publish"]) is not bool or type(data["can_withdraw"]) is not bool
            or data["can_publish"] and not data["can_withdraw"]):
        raise AcmeHubResponseError("ACME hub returned invalid participant authority")
    if data["trust_state"] == "revoked":
        _timestamp(data["revoked_at"])
        if data["can_publish"] or data["role"] == "owner":
            raise AcmeHubResponseError("ACME hub returned invalid revoked authority")
    elif data["revoked_at"] is not None:
        raise AcmeHubResponseError("ACME hub returned an invalid revocation timestamp")
    if controller:
        _response_uuid(data["consumer_project_uuid"])
        if (expected_consumer_uuid is not None and data["consumer_project_uuid"] != expected_consumer_uuid
                or not isinstance(data["consumer_label"], str) or len(data["consumer_label"]) > 200):
            raise AcmeHubResponseError("ACME hub returned an invalid recipient")
    if status:
        _alias_observation(data["alias"], allocation.target)
    return AcmeHubParticipant(data)


def _alias_observation(data, target):
    _shape(data, {"state", "observed_target", "one_hop", "checked_at"})
    _timestamp(data["checked_at"])
    state, observed, hop = data["state"], data["observed_target"], data["one_hop"]
    if state not in ("match", "mismatch", "lookup_failed"):
        raise AcmeHubResponseError("ACME hub returned an invalid alias state")
    if observed is not None:
        _response_domain(observed, "observed target")
        if type(hop) is not bool or state == "lookup_failed":
            raise AcmeHubResponseError("ACME hub returned an invalid alias observation")
    elif hop is not None or state == "match":
        raise AcmeHubResponseError("ACME hub returned an invalid alias observation")
    if state == "match" and (observed != target or hop is not True):
        raise AcmeHubResponseError("ACME hub returned a forged alias match")
    if state == "mismatch" and observed == target and hop is True:
        raise AcmeHubResponseError("ACME hub returned a conflicting alias observation")


def _page(data, validate, cursor=None):
    _shape(data, {"items", "next_cursor"})
    if not isinstance(data["items"], list) or len(data["items"]) > 20:
        raise AcmeHubResponseError("ACME hub returned an invalid page size")
    next_cursor = _page_cursor(data["next_cursor"], response=True)
    if next_cursor is not None and (len(data["items"]) != 20 or cursor is not None and int(next_cursor) <= int(cursor)):
        raise AcmeHubResponseError("ACME hub returned an invalid page cursor")
    rows = [validate(row) for row in data["items"]]
    refs = [row.get("participant_ref", row["allocation_ref"]) for row in rows]
    if len(set(refs)) != len(refs):
        raise AcmeHubResponseError("ACME hub returned duplicate page entries")
    return AcmeHubPage(items=rows, next_cursor=next_cursor)


def capabilities(*, transport=None):
    data = (transport or _post)("capabilities", {})
    _shape(data, {"version", "shared_validation", "consumer_project_uuid", "can_consume",
                  "can_manage_trust", "trust_scope"})
    if (type(data["version"]) is not int or data["version"] != 2
            or type(data["shared_validation"]) is not bool or data["trust_scope"] != "apex_and_wildcard"
            or type(data["can_consume"]) is not bool or type(data["can_manage_trust"]) is not bool):
        raise AcmeHubResponseError("ACME hub returned unsupported capabilities")
    if data["consumer_project_uuid"] is not None:
        _response_uuid(data["consumer_project_uuid"])
    if (data["can_consume"] != (data["shared_validation"] and data["consumer_project_uuid"] is not None)
            or (not data["shared_validation"] and data["can_manage_trust"])):
        raise AcmeHubResponseError("ACME hub returned invalid consumer capabilities")
    return objict(data)


def capabilities_for(origin, api_key, *, post=None, proof=False):
    """Authenticate a fresh explicit candidate, without mutating loaded settings."""
    config = _Config(_normalized_base_url(origin), _api_key(api_key),
                     0.5 if proof else 2, 1.5 if proof else 5, 0)
    return capabilities(transport=lambda operation, payload: _wire(
        operation, payload, config, post or requests.post))


def lookup(domain, allocation_ref=None, cursor=None, *, transport=None):
    domain = _domain(domain)
    payload = dict(domain=domain)
    if allocation_ref is not None:
        allocation_ref = payload["allocation_ref"] = _domain(allocation_ref)
    if cursor is not None:
        payload["cursor"] = _page_cursor(cursor)
    return _page((transport or _post)("lookup", payload), lambda row: _participant(
        row, expected_domain=domain, expected_allocation_ref=allocation_ref), cursor)


def status(client_ref, *, transport=None):
    client_ref = _reference(client_ref, "client_ref")
    return _participant((transport or _post)("status", dict(client_ref=client_ref)),
                        expected_client_ref=client_ref, status=True)


def join(domain, client_ref, allocation_ref, *, transport=None):
    domain, client_ref, allocation_ref = _domain(domain), _reference(client_ref, "client_ref"), _domain(allocation_ref)
    data = (transport or _post)("join", dict(domain=domain, client_ref=client_ref, allocation_ref=allocation_ref))
    result = _allocation(data, client_ref, expected_domain=domain)
    if result.target != allocation_ref:
        raise AcmeHubResponseError("ACME hub returned a cross-request target")
    return result


def _controller_allocation(data, domain):
    _shape(data, {"allocation_ref", "domain", "source", "target", "owner_project_uuid", "can_manage_trust"})
    allocation = _allocation(dict(client_ref="validate", **{key: data[key] for key in ("domain", "source", "target")}),
                             "validate", expected_domain=domain)
    if data["allocation_ref"] != allocation.target or data["can_manage_trust"] is not True:
        raise AcmeHubResponseError("ACME hub returned an invalid allocation authority")
    _response_uuid(data["owner_project_uuid"])
    return objict(data)


def list_allocations(domain, cursor=None, *, transport=None):
    domain = _domain(domain)
    payload = dict(domain=domain)
    if cursor is not None:
        payload["cursor"] = _page_cursor(cursor)
    return _page((transport or _post)("list_allocations", payload), lambda row: _controller_allocation(row, domain), cursor)


def list_participants(allocation_ref, cursor=None, *, transport=None):
    allocation_ref = _domain(allocation_ref)
    payload = dict(allocation_ref=allocation_ref)
    if cursor is not None:
        payload["cursor"] = _page_cursor(cursor)
    return _page((transport or _post)("list_participants", payload), lambda row: _participant(
        row, controller=True, expected_allocation_ref=allocation_ref), cursor)


def resolve_recipient(allocation_ref, consumer_project_uuid, *, transport=None):
    allocation_ref, consumer_project_uuid = _domain(allocation_ref), _request_uuid(consumer_project_uuid)
    data = (transport or _post)("resolve_recipient", dict(allocation_ref=allocation_ref, consumer_project_uuid=consumer_project_uuid))
    _shape(data, {"consumer_project_uuid", "label", "active"})
    if (data["consumer_project_uuid"] != consumer_project_uuid or data["active"] is not True
            or not isinstance(data["label"], str) or len(data["label"]) > 200):
        raise AcmeHubResponseError("ACME hub returned an invalid recipient")
    return objict(data)


def grant(connection_request, *, transport=None):
    if not isinstance(connection_request, dict) or set(connection_request) != {
            "version", "consumer_project_uuid", "client_ref", "domain", "allocation_ref"}:
        raise AcmeHubRequestError("a version 1 connection request is required")
    if type(connection_request["version"]) is not int or connection_request["version"] != 1:
        raise AcmeHubRequestError("a version 1 connection request is required")
    descriptor = dict(version=1, consumer_project_uuid=_request_uuid(connection_request["consumer_project_uuid"]),
                      client_ref=_reference(connection_request["client_ref"], "client_ref"),
                      domain=_domain(connection_request["domain"]), allocation_ref=_domain(connection_request["allocation_ref"]))
    result = _participant((transport or _post)("grant", dict(connection_request=descriptor)), controller=True,
                          expected_client_ref=descriptor["client_ref"], expected_domain=descriptor["domain"],
                          expected_allocation_ref=descriptor["allocation_ref"],
                          expected_consumer_uuid=descriptor["consumer_project_uuid"])
    if result.role != "trusted" or result.trust_state != "active":
        raise AcmeHubResponseError("ACME hub returned an invalid grant")
    return result


def revoke(allocation_ref, participant_ref, *, transport=None):
    allocation_ref, participant_ref = _domain(allocation_ref), _request_uuid(participant_ref)
    result = _participant((transport or _post)("revoke", dict(allocation_ref=allocation_ref, participant_ref=participant_ref)),
                          controller=True, expected_allocation_ref=allocation_ref, expected_participant_ref=participant_ref)
    if result.role != "trusted" or result.trust_state != "revoked":
        raise AcmeHubResponseError("ACME hub returned an invalid revocation")
    return result
