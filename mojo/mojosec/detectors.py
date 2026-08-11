"""High-signal detectors for journal and structured nginx records."""

import datetime
import math
import re
import shlex
import urllib.parse

from .events import bounded_text, observation, valid_ip
from .attribution import (
    audit_context, canonical_tty, canonical_uid, trusted_journal_source,
)
from .evidence import build_evidence, digest_text, fingerprint_values as evidence_fingerprint


_SSH_ACCEPTED = re.compile(
    r"Accepted (?P<method>\S+) for (?P<user>\S+) from (?P<ip>\S+) port \d+", re.I)
_SSH_FAILED = re.compile(
    r"Failed (?P<method>\S+) for (?:invalid user )?(?P<user>\S+) from (?P<ip>\S+) port \d+", re.I)
_SSH_INVALID = re.compile(r"Invalid user (?P<user>\S+) from (?P<ip>\S+)", re.I)
_SUDO_COMMAND = re.compile(
    r"^(?P<actor>\S+)\s*:\s*(?P<context>.*?)USER=(?P<target>\S+)\s*;\s*COMMAND=(?P<command>.*)$")
_SUDO_PWD = re.compile(r"(?:^|;)\s*PWD=(?P<pwd>[^;]+)")
_SUDO_TTY = re.compile(r"(?:^|;)\s*TTY=(?P<tty>[^;]+)")
_SESSION_OPEN = re.compile(
    r"^pam_unix\((?P<service>[^:]+):session\): session opened for user "
    r"(?P<user>[^ (]+)\(uid=(?P<target_uid>[0-9]+)\) by "
    r"(?:[^()]*)\(uid=(?P<opener_uid>[0-9]+)\)$", re.I)

_PROBE_MARKERS = (
    "/wp-admin", "/wp-login", "/wp-content", "/xmlrpc.php", "/phpmyadmin",
    "/.env", "/.git/", "/vendor/phpunit", "/cgi-bin/", "/actuator/",
    "/server-status", "/swagger", "/openapi.json", "/solr/", "/jenkins/",
)
_PROBE_SUFFIXES = (
    ".php", ".php3", ".php4", ".php5", ".php7", ".php8", ".phtml",
    ".asp", ".aspx", ".ashx", ".jsp", ".jspx", ".cgi",
)
_UUID_SEGMENT = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[1-5a-f0-9][a-f0-9]{3}-[89ab0-9][a-f0-9]{3}-[a-f0-9]{12}$", re.I)
_HEX_SEGMENT = re.compile(r"^[a-f0-9]{16,}$", re.I)
_TOKEN_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_DIGIT_SEGMENT = re.compile(r"^\d{6,}$")
_TOKEN_CONTEXT = {"activate", "invite", "magic", "password", "recover", "reset", "token", "verify"}
_REQUEST_ID = re.compile(r"^[a-f0-9]{32}$")
_HTTP_PROTOCOL = re.compile(r"^HTTP/(?:0\.9|1\.0|1\.1|2(?:\.0)?|3(?:\.0)?)$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._-]{1,96}$")
_UNIT_NAME = (
    r"[A-Za-z0-9@:.\\_-]{1,128}"
    r"\.(?:service|socket|target|device|mount|automount|swap|path|timer|slice|scope)"
)
_UNIT_FAILURE = re.compile(
    r"^(?P<unit>" + _UNIT_NAME + r"): "
    r"(?:Failed with result '(?P<result>[a-z0-9-]{1,64})'\.|Unit entered failed state\.)$")
_UNIT_FAILURE_LEGACY = re.compile(r"^Unit (?P<unit>" + _UNIT_NAME + r") entered failed state\.$")
_UNIT_FIELD = re.compile(r"^" + _UNIT_NAME + r"$")
# systemd's public SD_MESSAGE_UNIT_FAILED catalog id: wording-drift armor for
# the PID 1 failure grammar above. Only read after the kernel-owned _PID/_UID
# proof, with the caller-attachable UNIT= field validated against _UNIT_FIELD.
_UNIT_FAILED_MESSAGE_ID = "d9b373ed55a64feb8242e02dbe79ffcf"


class DetectorError(ValueError):
    pass


def _secret_segment(segment, previous):
    return bool(
        previous in _TOKEN_CONTEXT or "@" in segment or _UUID_SEGMENT.fullmatch(segment) or
        _HEX_SEGMENT.fullmatch(segment) or _TOKEN_SEGMENT.fullmatch(segment) or
        _DIGIT_SEGMENT.fullmatch(segment) or (segment.count(".") == 2 and len(segment) >= 24)
    )


def _normalized_web_paths(path):
    """Return bounded evidence and low-cardinality fingerprint path forms."""
    pieces = []
    keys = []
    previous = ""
    for raw_segment in path.split("/")[:32]:
        segment = "".join(character for character in raw_segment if ord(character) >= 32)[:128]
        lowered = segment.lower()
        if segment in (".", ".."):
            pieces.append("~dot")
            keys.append("~dot")
        elif _secret_segment(segment, previous):
            digest = digest_text(segment)[:12]
            pieces.append("~" + digest)
            keys.append("~token")
        else:
            pieces.append(segment)
            keys.append(segment)
        if segment:
            previous = lowered
    evidence = "/".join(pieces)
    key = "/".join(keys)
    if path.startswith("/"):
        evidence = "/" + evidence.lstrip("/")
        key = "/" + key.lstrip("/")
    return evidence[:512] or "/", key[:512] or "/"


def _sudo_command_identity(command):
    command = str(command or "")[:4096]
    digest = digest_text(command)
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        words = command.split()
    executable = "unknown"
    for word in words:
        if "=" in word and not word.startswith("/"):
            continue
        executable = word
        break
    return bounded_text(executable, 256), digest


def _journal_time(record):
    raw = record.get("__REALTIME_TIMESTAMP")
    try:
        value = datetime.datetime.fromtimestamp(int(raw) / 1000000, datetime.timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return None


def _canonical_int(value, minimum, maximum, field, optional=False):
    if optional and value in (None, "", "-"):
        return None
    try:
        if isinstance(value, bool) or isinstance(value, float):
            raise ValueError
        text = str(value)
        if not re.fullmatch(r"0|[1-9][0-9]*", text):
            raise ValueError
        number = int(text)
    except (TypeError, ValueError, OverflowError) as err:
        raise DetectorError(f"nginx {field} must be an integer") from err
    if not minimum <= number <= maximum:
        raise DetectorError(f"nginx {field} must be from {minimum} to {maximum}")
    return number


def _canonical_number_list(value, minimum, maximum, field, integer=False):
    if value in (None, "", "-"):
        return ""
    items = re.split(r"\s*[,;:]\s*", str(value))
    if len(items) > 8:
        raise DetectorError(f"nginx {field} must contain at most 8 values")
    result = []
    for item in items:
        if item in ("", "-"):
            continue
        try:
            if integer:
                if not re.fullmatch(r"0|[1-9][0-9]*", item):
                    raise ValueError
                number = int(item)
            else:
                number = float(item)
        except (TypeError, ValueError, OverflowError) as err:
            raise DetectorError(f"nginx {field} contains an invalid number") from err
        if (not integer and not math.isfinite(number)) or not minimum <= number <= maximum:
            raise DetectorError(f"nginx {field} contains an out-of-range number")
        result.append(str(number) if integer else item)
    return ",".join(result)


def _optional_token(value, field, pattern=_SAFE_TOKEN):
    if value in (None, "", "-"):
        return ""
    value = str(value)
    if not pattern.fullmatch(value):
        raise DetectorError(f"nginx {field} is invalid")
    return value


def _systemd_user_session(record, session, attribution):
    service = session.group("service").lower()
    if service != "systemd-user":
        return None
    target_uid = canonical_uid(session.group("target_uid"))
    opener_uid = canonical_uid(session.group("opener_uid"))
    producer_uid = canonical_uid(record.get("_UID"))
    producer_pid = canonical_uid(record.get("_PID"))
    audit_loginuid = canonical_uid(
        record.get("_AUDIT_LOGINUID", record.get("AUDIT_LOGINUID")))
    context = audit_context(record)
    unit = str(record.get("_SYSTEMD_UNIT") or "")
    if (target_uid is None or opener_uid != 0 or producer_uid != 0 or
            not producer_pid or str(record.get("_COMM") or "") != "(systemd)" or
            str(record.get("_EXE") or "") != "/usr/lib/systemd/systemd" or
            unit != f"user@{target_uid}.service" or audit_loginuid != target_uid or
            not context["boot_id"] or context["audit_session"] is None):
        return None
    source_ip = ""
    provenance = "none"
    if attribution is not None:
        source_ip, provenance, context = attribution.resolve(
            record, session.group("user"), context.get("tty"), allow_who=False)
    values = {
        "source_ip": source_ip, "attribution_provenance": provenance,
        "service": service, "target_user": session.group("user"),
        "target_uid": target_uid, "opener_uid": opener_uid,
        "producer_uid": producer_uid, "producer_pid": producer_pid,
        "producer_comm": record.get("_COMM"), "producer_exe": record.get("_EXE"),
        "systemd_unit": unit, "boot_id": context.get("boot_id"),
        "audit_session": context.get("audit_session"),
        "audit_loginuid": audit_loginuid, "tty": context.get("tty"),
    }
    return build_evidence("auth.session_open", values)


def detect_journal(record, attribution=None):
    """Return zero or one observation for a bounded journal JSON record."""
    message = bounded_text(record.get("MESSAGE"), 2048)
    if not message:
        return None
    identifier = bounded_text(
        record.get("SYSLOG_IDENTIFIER") or record.get("_COMM") or record.get("_SYSTEMD_UNIT"), 96
    ).lower()
    observed_at = _journal_time(record)

    if (trusted_journal_source(record, "sshd") and
            (_SSH_ACCEPTED.search(message) or _SSH_FAILED.search(message) or
             _SSH_INVALID.search(message))):
        match = _SSH_ACCEPTED.search(message)
        if match and valid_ip(match.group("ip")):
            values = match.groupdict()
            context = audit_context(record)
            attributes = build_evidence("auth.ssh_login", {
                "source_ip": values["ip"], "user": values["user"],
                "auth_method": values["method"], **context,
            })
            return observation(
                "auth.ssh_login", "high", "SSH login accepted",
                attributes=attributes,
                fingerprint_values=(values["ip"], values["user"], values["method"], observed_at or ""),
                aggregate=False, recommendation="review", observed_at=observed_at,
            )
        match = _SSH_FAILED.search(message) or _SSH_INVALID.search(message)
        if match and valid_ip(match.group("ip")):
            values = match.groupdict()
            context = audit_context(record)
            attributes = build_evidence("auth.ssh_failure", {
                "source_ip": values["ip"], "user": values.get("user"),
                "auth_method": values.get("method", "unknown"), **context,
            })
            return observation(
                "auth.ssh_failure", "warning", "SSH authentication failed",
                attributes=attributes,
                fingerprint_values=(
                    values["ip"], values.get("user", ""), values.get("method", "unknown")),
                aggregate=True, recommendation="review", observed_at=observed_at,
            )

    if (trusted_journal_source(record, "sudo") and
            (_SUDO_COMMAND.search(message) or "authentication failure" in message.lower())):
        match = _SUDO_COMMAND.search(message)
        if match:
            values = match.groupdict()
            command_path, command_digest = _sudo_command_identity(values["command"])
            tty_match = _SUDO_TTY.search(values["context"])
            pwd_match = _SUDO_PWD.search(values["context"])
            tty = canonical_tty(tty_match.group("tty").strip()) if tty_match else ""
            context = audit_context(record)
            source_ip = ""
            provenance = "none"
            if attribution is not None:
                source_ip, provenance, context = attribution.resolve(
                    record, values["actor"], tty or context.get("tty"))
            attributes = build_evidence("auth.sudo_command", {
                "source_ip": source_ip,
                "actor": values["actor"],
                "target_user": values["target"],
                "tty": tty or context.get("tty"),
                "boot_id": context.get("boot_id"),
                "audit_session": context.get("audit_session"),
                "attribution_provenance": provenance,
                "cwd": pwd_match.group("pwd").strip() if pwd_match else "",
                "command_path": command_path,
                "command_sha256": command_digest,
                "command": values["command"],
            })
            return observation(
                "auth.sudo_command", "high", "Privileged sudo command executed",
                attributes=attributes,
                fingerprint_values=(values["actor"], values["target"], command_digest, observed_at or ""),
                aggregate=False, recommendation="review", observed_at=observed_at,
            )
        if "authentication failure" in message.lower():
            context = audit_context(record)
            attributes = build_evidence("auth.sudo_failure", context)
            return observation(
                "auth.sudo_failure", "high", "sudo authentication failed",
                attributes=attributes,
                fingerprint_values=(bounded_text(message, 256),),
                aggregate=True, recommendation="review", observed_at=observed_at,
            )

    session = _SESSION_OPEN.fullmatch(message)
    if session:
        attributes = _systemd_user_session(record, session, attribution)
    else:
        attributes = None
    if attributes is not None:
        return observation(
            "auth.session_open", "info", "PAM service session opened",
            attributes=attributes,
            fingerprint_values=(session.group("service"), session.group("user"), observed_at or ""),
            aggregate=False, recommendation="none", observed_at=observed_at,
        )

    priority = record.get("PRIORITY", 7)
    unit = bounded_text(record.get("_SYSTEMD_UNIT") or identifier, 128)
    try:
        if isinstance(priority, bool):
            raise ValueError
        priority = int(priority)
    except (TypeError, ValueError):
        raise DetectorError("journal priority must be an integer from 0 to 7")
    if not 0 <= priority <= 7:
        raise DetectorError("journal priority must be an integer from 0 to 7")
    lowered = message.lower()
    if str(record.get("_TRANSPORT") or "") == "kernel" and (
            "out of memory" in lowered or "killed process" in lowered):
        # The kernel emits exactly one "Killed process" line per victim; the
        # oom-kill: detail line and invoked-oom-killer header no longer match,
        # so one kill is one critical event. _TRANSPORT is journald-owned.
        return observation(
            "system.oom", "critical", "Kernel out-of-memory action detected",
            attributes=build_evidence(
                "system.oom", {"unit": unit, "message": message}),
            fingerprint_values=(unit, "oom"),
            aggregate=False, recommendation="review", observed_at=observed_at,
        )
    if (str(record.get("_PID") or "") == "1" and
            canonical_uid(record.get("_UID")) == 0):
        # Only PID 1's own unit-failure declarations count as a service
        # failure; caller-controlled identifiers never open this branch.
        match = _UNIT_FAILURE.fullmatch(message) or _UNIT_FAILURE_LEGACY.fullmatch(message)
        failed_unit = ""
        failure_kind = ""
        if match:
            failed_unit = match.group("unit")
            failure_kind = match.groupdict().get("result") or "failed"
        elif (str(record.get("MESSAGE_ID") or "").lower() == _UNIT_FAILED_MESSAGE_ID and
              _UNIT_FIELD.fullmatch(str(record.get("UNIT") or ""))):
            failed_unit = str(record["UNIT"])
            failure_kind = "unknown"
        if failed_unit:
            return observation(
                "system.service_error", "high", "System service reported a failure",
                attributes=build_evidence("system.service_error", {
                    "unit": failed_unit, "priority": priority, "message": message,
                    "failure_kind": failure_kind,
                }),
                fingerprint_values=(failed_unit, failure_kind),
                aggregate=True, recommendation="review", observed_at=observed_at,
            )
    return None


def _nginx_time(record):
    value = record.get("time") or record.get("time_iso8601") or record.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.isoformat().replace("+00:00", "Z")


def detect_nginx(record):
    """Detect only actionable probes, access denials, and server errors.

    User-Agent alone is intentionally never a signal. Modern crawlers and AI
    bots frequently identify themselves inconsistently; behavior is safer and
    substantially quieter than an identity string supplied by the client.
    """
    if not isinstance(record, dict):
        return None
    method = bounded_text(record.get("method") or record.get("request_method"), 16).upper()
    raw_path = bounded_text(
        record.get("uri") or record.get("request_uri") or record.get("path"), 32768
    )
    path = raw_path.split("?", 1)[0][:2048]
    try:
        decoded_path = urllib.parse.unquote(path, errors="replace")
        decoded = decoded_path.lower()
    except (TypeError, ValueError):
        decoded_path = path
        decoded = path.lower()
    source_ip = valid_ip(record.get("remote_addr") or record.get("source_ip"))
    peer_ip = valid_ip(record.get("peer_addr") or record.get("realip_remote_addr"))
    raw_status = record.get("status")
    try:
        if isinstance(raw_status, bool) or isinstance(raw_status, float):
            raise ValueError
        status = int(raw_status)
    except (TypeError, ValueError) as err:
        raise DetectorError("nginx status must be an integer") from err
    if not 100 <= status <= 599:
        raise DetectorError("nginx status must be from 100 to 599")
    if not path or status == 499:
        return None
    evidence_path, fingerprint_path = _normalized_web_paths(decoded_path)
    evidence_values = {
        "method": method, "path": evidence_path, "status": status,
        "request_uri": raw_path,
        "source_ip": source_ip,
        "peer_ip": peer_ip if peer_ip != source_ip else "",
        "host": record.get("host") or record.get("server_name"),
        "referrer": record.get("referrer") or record.get("referer") or record.get("http_referer"),
        "user_agent": record.get("user_agent") or record.get("http_user_agent"),
        "upstream_status": record.get("upstream_status"),
        "upstream_response_time": record.get("upstream_response_time"),
    }
    for field in ("remote_port", "peer_port", "server_port"):
        value = _canonical_int(record.get(field), 1, 65535, field, optional=True)
        if value is not None:
            evidence_values[field] = value
    for field in ("request_length", "response_bytes", "response_body_bytes"):
        value = _canonical_int(record.get(field), 0, 2 ** 63 - 1, field, optional=True)
        if value is not None:
            evidence_values[field] = value
    request_id = _optional_token(record.get("request_id"), "request_id", _REQUEST_ID)
    if request_id:
        evidence_values["request_id"] = request_id
    scheme = _optional_token(record.get("scheme"), "scheme")
    if scheme and scheme not in ("http", "https"):
        raise DetectorError("nginx scheme must be http or https")
    if scheme:
        evidence_values["scheme"] = scheme
    protocol = _optional_token(record.get("protocol"), "protocol", _HTTP_PROTOCOL)
    if protocol:
        evidence_values["protocol"] = protocol
    for field in ("tls_protocol", "tls_cipher"):
        value = _optional_token(record.get(field), field)
        if value:
            evidence_values[field] = value
    for field in ("request_time", "upstream_connect_time", "upstream_header_time",
                  "upstream_response_time"):
        value = _canonical_number_list(record.get(field), 0, 3600, field)
        if value:
            evidence_values[field] = value
    for field in ("upstream_response_length", "upstream_bytes_received",
                  "upstream_bytes_sent"):
        value = _canonical_number_list(
            record.get(field), 0, 2 ** 63 - 1, field, integer=True)
        if value:
            evidence_values[field] = value
    upstream_status = _canonical_number_list(
        record.get("upstream_status"), 100, 599, "upstream_status", integer=True)
    evidence_values["upstream_status"] = upstream_status
    observed_at = _nginx_time(record)

    kind = None

    is_probe = any(marker in decoded for marker in _PROBE_MARKERS)
    is_probe = is_probe or any(decoded.endswith(suffix) for suffix in _PROBE_SUFFIXES)
    if is_probe:
        kind, severity, summary, recommendation = (
            "web.probe", "high", "Known exploit path probe", "block_ip")
    elif status >= 500:
        kind, severity, summary, recommendation = (
            "web.error", "high" if status >= 502 else "warning",
            "Web request returned a server error", "review")
    elif status in (401, 403):
        kind, severity, summary, recommendation = (
            "web.denied", "warning", "Web request was denied", "none")
    if kind is None:
        return None
    attributes = build_evidence(kind, evidence_values)
    scalar_fields = (
        "source_ip", "peer_ip", "method", "status", "host",
        "upstream_status",
    )
    if kind == "web.error":
        # One server fault hits every client at once; address identity
        # multiplies one incident into per-client aggregates without
        # adding signal. Probe/denied stay per-actor keys: there the
        # address is the identity being screened (probe feeds block_ip).
        scalar_fields = ("method", "status", "host", "upstream_status")
    fingerprints = evidence_fingerprint(attributes, scalar_fields) + (fingerprint_path,)
    return observation(
        kind, severity, summary, attributes=attributes,
        fingerprint_values=fingerprints, aggregate=True,
        recommendation=recommendation, observed_at=observed_at,
    )
