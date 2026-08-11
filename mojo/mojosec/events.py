"""Bounded internal observations produced by MojoSec collectors."""

import hashlib
import ipaddress
import json

from .protocol import SEVERITIES, normalize_timestamp, utc_now


def bounded_text(value, limit):
    # Normalize NUL and lone surrogates so every summary and bounded field is
    # storable by construction on the receiving side.
    value = str(value or "").replace("\x00", "")
    value = value.encode("utf-8", errors="replace").decode("utf-8").strip()
    return value[:limit]


def valid_ip(value):
    try:
        return str(ipaddress.ip_address(str(value)))
    except ValueError:
        return ""


def fingerprint(kind, values):
    material = json.dumps([kind] + list(values), ensure_ascii=True,
                          separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def observation(kind, severity, summary, attributes=None, fingerprint_values=None,
                aggregate=True, recommendation="none", observed_at=None):
    if severity not in SEVERITIES:
        raise ValueError(f"unsupported observation severity: {severity}")
    attributes = dict(attributes or {})
    source_ip = attributes.get("source_ip")
    if source_ip:
        source_ip = valid_ip(source_ip)
        if source_ip:
            attributes["source_ip"] = source_ip
        else:
            attributes.pop("source_ip", None)
    values = fingerprint_values
    if values is None:
        values = (summary, source_ip or "")
    return {
        "kind": kind,
        "severity": severity,
        "summary": bounded_text(summary, 256),
        "attributes": attributes,
        "fingerprint": fingerprint(kind, values),
        "aggregate": bool(aggregate),
        "recommendation": recommendation,
        "observed_at": normalize_timestamp(observed_at) if observed_at else utc_now(),
    }
