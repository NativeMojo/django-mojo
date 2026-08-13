"""MojoSec wire protocol shared by the host sensor and incident receiver.

This module deliberately imports only the Python standard library.  The sensor
runs before (and independently of) Django, while the receiver imports the same
validators inside a configured application process.
"""

import datetime
import hashlib
import ipaddress
import json
import math
import re


PROTOCOL_VERSION = 1
BATCH_SCHEMA = "mojosec.batch"
ACK_SCHEMA = "mojosec.ack"

MAX_BATCH_EVENTS = 500
MAX_BATCH_BYTES = 512 * 1024
MAX_SUMMARY = 256
MAX_ATTRIBUTE_BYTES = 8 * 1024
MAX_SENSOR_ID = 128
MAX_KIND = 96

SEVERITIES = ("info", "warning", "high", "critical")
RECOMMENDATIONS = ("none", "review", "block_ip")
ACK_STATUSES = ("accepted", "duplicate", "rejected", "retry")

_ID_RE = re.compile(r"^[a-f0-9]{64}$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")
_SENSOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ATTRIBUTE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ProtocolError(ValueError):
    """Raised when an object is not valid MojoSec protocol data."""


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_timestamp(value):
    parsed = _require_timestamp(value, "timestamp")
    return parsed.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def event_id(sensor_id, kind, fingerprint, first_seen):
    """Return a stable id for one logical event or aggregation window."""
    material = "\x00".join((sensor_id, kind, fingerprint, first_seen)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def has_unstorable_text(value):
    """True when any string inside value cannot be stored by PostgreSQL.

    NUL code points are rejected by text columns and jsonb; lone surrogates
    (which json.loads happily produces from escapes, and surrogateescape
    decoding produces from raw bytes) are rejected by jsonb. Wire validation
    stays permissive so sensors never wedge on their own spooled data; the
    receiver calls this to terminally reject what its storage cannot hold.
    """
    if isinstance(value, str):
        if "\x00" in value:
            return True
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return True
        return False
    if isinstance(value, dict):
        return any(has_unstorable_text(key) or has_unstorable_text(child)
                   for key, child in value.items())
    if isinstance(value, (list, tuple)):
        return any(has_unstorable_text(child) for child in value)
    return False


def _require_keys(value, required, optional, label):
    if not isinstance(value, dict):
        raise ProtocolError(f"{label} must be an object")
    keys = set(value)
    missing = set(required) - keys
    unknown = keys - set(required) - set(optional)
    if missing:
        raise ProtocolError(f"{label} missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ProtocolError(f"{label} has unknown fields: {', '.join(sorted(unknown))}")


def _require_timestamp(value, label):
    if not isinstance(value, str) or len(value) > 40:
        raise ProtocolError(f"{label} must be an ISO-8601 string")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as err:
        raise ProtocolError(f"{label} must be an ISO-8601 timestamp") from err
    if parsed.tzinfo is None:
        raise ProtocolError(f"{label} must include a timezone")
    return parsed


def _validate_attributes(value, depth=0):
    # One bounded list of flat objects is required for process ancestors:
    # attributes -> list -> ancestor -> scalar. Deeper arbitrary structures
    # remain forbidden.
    if depth > 3:
        raise ProtocolError("event attributes are nested too deeply")
    if isinstance(value, dict):
        if len(value) > 64:
            raise ProtocolError("event attributes have too many keys")
        for key, child in value.items():
            if not isinstance(key, str) or not _ATTRIBUTE_KEY_RE.fullmatch(key):
                raise ProtocolError("event attribute keys must use lowercase snake_case")
            _validate_attributes(child, depth + 1)
        return
    if isinstance(value, list):
        if len(value) > 32:
            raise ProtocolError("event attribute lists may contain at most 32 values")
        for child in value:
            _validate_attributes(child, depth + 1)
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, str) and len(value) <= 2048:
        return
    raise ProtocolError("event attributes contain an unsupported value")


def validate_event(value):
    _require_keys(
        value,
        required=("id", "kind", "observed_at", "first_seen", "last_seen",
                  "severity", "summary", "count", "attributes", "recommendation"),
        optional=(),
        label="event",
    )
    if not isinstance(value["id"], str) or not _ID_RE.fullmatch(value["id"]):
        raise ProtocolError("event id must be a lowercase SHA-256 digest")
    if not isinstance(value["kind"], str) or not _KIND_RE.fullmatch(value["kind"]):
        raise ProtocolError("event kind is invalid")
    observed = _require_timestamp(value["observed_at"], "event observed_at")
    first = _require_timestamp(value["first_seen"], "event first_seen")
    last = _require_timestamp(value["last_seen"], "event last_seen")
    if first > last or observed != last:
        raise ProtocolError("event timestamps must satisfy first_seen <= last_seen == observed_at")
    if value["severity"] not in SEVERITIES:
        raise ProtocolError("event severity is invalid")
    if not isinstance(value["summary"], str) or not value["summary"] or len(value["summary"]) > MAX_SUMMARY:
        raise ProtocolError(f"event summary must be 1-{MAX_SUMMARY} characters")
    if (not isinstance(value["count"], int) or isinstance(value["count"], bool) or
            not 1 <= value["count"] <= 1000000000):
        raise ProtocolError("event count must be an integer from 1 to 1000000000")
    if value["recommendation"] not in RECOMMENDATIONS:
        raise ProtocolError("event recommendation is invalid")
    _validate_attributes(value["attributes"])
    if len(canonical_json(value["attributes"]).encode("utf-8")) > MAX_ATTRIBUTE_BYTES:
        raise ProtocolError("event attributes exceed the encoded size limit")
    source_ip = value["attributes"].get("source_ip")
    if source_ip:
        try:
            ipaddress.ip_address(source_ip)
        except ValueError as err:
            raise ProtocolError("event source_ip is invalid") from err
    return value


def make_event(sensor_id, observation, count=1, first_seen=None, last_seen=None):
    first_seen = first_seen or observation["observed_at"]
    last_seen = last_seen or observation["observed_at"]
    event = {
        "id": event_id(sensor_id, observation["kind"], observation["fingerprint"], first_seen),
        "kind": observation["kind"],
        "observed_at": last_seen,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "severity": observation["severity"],
        "summary": observation["summary"][:MAX_SUMMARY],
        "count": count,
        "attributes": observation.get("attributes", {}),
        "recommendation": observation.get("recommendation", "none"),
    }
    return validate_event(event)


def make_batch(sensor_id, events, policy_revision=""):
    if not isinstance(sensor_id, str) or not _SENSOR_RE.fullmatch(sensor_id):
        raise ProtocolError("sensor_id is invalid")
    batch = {
        "schema": BATCH_SCHEMA,
        "version": PROTOCOL_VERSION,
        "sensor_id": sensor_id,
        "sent_at": utc_now(),
        "policy_revision": str(policy_revision)[:128],
        "events": list(events),
    }
    return validate_batch(batch)


def validate_batch(value, encoded_size=None):
    _require_keys(
        value,
        required=("schema", "version", "sensor_id", "sent_at", "policy_revision", "events"),
        optional=(),
        label="batch",
    )
    if (value["schema"] != BATCH_SCHEMA or
            not isinstance(value["version"], int) or isinstance(value["version"], bool) or
            value["version"] != PROTOCOL_VERSION):
        raise ProtocolError("unsupported MojoSec batch schema or version")
    if not isinstance(value["sensor_id"], str) or not _SENSOR_RE.fullmatch(value["sensor_id"]):
        raise ProtocolError("sensor_id is invalid")
    _require_timestamp(value["sent_at"], "batch sent_at")
    if not isinstance(value["policy_revision"], str) or len(value["policy_revision"]) > 128:
        raise ProtocolError("policy_revision is invalid")
    if not isinstance(value["events"], list) or not value["events"]:
        raise ProtocolError("batch events must be a non-empty list")
    if len(value["events"]) > MAX_BATCH_EVENTS:
        raise ProtocolError("batch contains too many events")
    seen = set()
    for event in value["events"]:
        validate_event(event)
        if event["id"] in seen:
            raise ProtocolError("batch contains duplicate event ids")
        seen.add(event["id"])
    size = encoded_size
    if size is None:
        size = len(canonical_json(value).encode("utf-8"))
    if size > MAX_BATCH_BYTES:
        raise ProtocolError("batch exceeds the encoded size limit")
    return value


def validate_ack(value, expected_ids=None):
    _require_keys(value, required=("schema", "version", "results"), optional=(), label="ack")
    if (value["schema"] != ACK_SCHEMA or
            not isinstance(value["version"], int) or isinstance(value["version"], bool) or
            value["version"] != PROTOCOL_VERSION):
        raise ProtocolError("unsupported MojoSec acknowledgement schema or version")
    if not isinstance(value["results"], list):
        raise ProtocolError("ack results must be a list")
    if len(value["results"]) > MAX_BATCH_EVENTS:
        raise ProtocolError("ack contains too many results")
    expected = set(expected_ids or ())
    seen = set()
    for result in value["results"]:
        _require_keys(result, required=("id", "status"), optional=("reason",), label="ack result")
        if not isinstance(result["id"], str) or not _ID_RE.fullmatch(result["id"]):
            raise ProtocolError("ack result id must be a lowercase SHA-256 digest")
        if result["id"] in seen:
            raise ProtocolError("ack contains duplicate event ids")
        if expected and result["id"] not in expected:
            raise ProtocolError("ack contains an event id that was not sent")
        if result["status"] not in ACK_STATUSES:
            raise ProtocolError("ack result status is invalid")
        if "reason" in result and (not isinstance(result["reason"], str) or len(result["reason"]) > 256):
            raise ProtocolError("ack result reason is invalid")
        seen.add(result["id"])
    return value
