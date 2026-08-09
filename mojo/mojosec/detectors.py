"""High-signal detectors for journal and structured nginx records."""

import datetime
import re
import urllib.parse

from .events import bounded_text, observation, valid_ip


_SSH_ACCEPTED = re.compile(
    r"Accepted (?P<method>\S+) for (?P<user>\S+) from (?P<ip>\S+) port \d+", re.I)
_SSH_FAILED = re.compile(
    r"Failed (?P<method>\S+) for (?:invalid user )?(?P<user>\S+) from (?P<ip>\S+) port \d+", re.I)
_SSH_INVALID = re.compile(r"Invalid user (?P<user>\S+) from (?P<ip>\S+)", re.I)
_SUDO_COMMAND = re.compile(
    r"^(?P<actor>\S+)\s*:\s*.*?USER=(?P<target>\S+)\s*;\s*COMMAND=(?P<command>.*)$")
_SESSION_OPEN = re.compile(
    r"pam_unix\((?P<service>[^:]+):session\): session opened for user (?P<user>[^ (]+)", re.I)

_PROBE_MARKERS = (
    "/wp-admin", "/wp-login", "/wp-content", "/xmlrpc.php", "/phpmyadmin",
    "/.env", "/.git/", "/vendor/phpunit", "/cgi-bin/", "/actuator/",
    "/server-status", "/swagger", "/openapi.json", "/solr/", "/jenkins/",
)
_PROBE_SUFFIXES = (
    ".php", ".php3", ".php4", ".php5", ".php7", ".php8", ".phtml",
    ".asp", ".aspx", ".ashx", ".jsp", ".jspx", ".cgi",
)


def _journal_time(record):
    raw = record.get("__REALTIME_TIMESTAMP")
    try:
        value = datetime.datetime.fromtimestamp(int(raw) / 1000000, datetime.timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return None


def detect_journal(record):
    """Return zero or one observation for a bounded journal JSON record."""
    message = bounded_text(record.get("MESSAGE"), 2048)
    if not message:
        return None
    identifier = bounded_text(
        record.get("SYSLOG_IDENTIFIER") or record.get("_COMM") or record.get("_SYSTEMD_UNIT"), 96
    ).lower()
    auth_facility = str(record.get("SYSLOG_FACILITY", "")) == "10"
    observed_at = _journal_time(record)

    if (identifier in ("sshd", "sshd.service") or "sshd" in identifier or
            (auth_facility and (_SSH_ACCEPTED.search(message) or _SSH_FAILED.search(message) or
                                _SSH_INVALID.search(message)))):
        match = _SSH_ACCEPTED.search(message)
        if match and valid_ip(match.group("ip")):
            values = match.groupdict()
            return observation(
                "auth.ssh_login", "high", "SSH login accepted",
                attributes={
                    "source_ip": values["ip"], "user": bounded_text(values["user"], 64),
                    "auth_method": bounded_text(values["method"], 32),
                },
                fingerprint_values=(values["ip"], values["user"], values["method"], observed_at or ""),
                aggregate=False, recommendation="review", observed_at=observed_at,
            )
        match = _SSH_FAILED.search(message) or _SSH_INVALID.search(message)
        if match and valid_ip(match.group("ip")):
            values = match.groupdict()
            return observation(
                "auth.ssh_failure", "warning", "SSH authentication failed",
                attributes={
                    "source_ip": values["ip"], "user": bounded_text(values.get("user"), 64),
                    "auth_method": bounded_text(values.get("method", "unknown"), 32),
                },
                fingerprint_values=(values["ip"], values.get("user", "")),
                aggregate=True, recommendation="review", observed_at=observed_at,
            )

    if (identifier in ("sudo", "sudo.service") or
            (auth_facility and (_SUDO_COMMAND.search(message) or
                                "sudo" in message.lower()))):
        match = _SUDO_COMMAND.search(message)
        if match:
            values = match.groupdict()
            return observation(
                "auth.sudo_command", "high", "Privileged sudo command executed",
                attributes={
                    "actor": bounded_text(values["actor"], 64),
                    "target_user": bounded_text(values["target"], 64),
                    "command": bounded_text(values["command"], 512),
                },
                fingerprint_values=(values["actor"], values["target"], values["command"], observed_at or ""),
                aggregate=False, recommendation="review", observed_at=observed_at,
            )
        if "authentication failure" in message.lower():
            return observation(
                "auth.sudo_failure", "high", "sudo authentication failed",
                attributes={"message": bounded_text(message, 512)},
                fingerprint_values=(bounded_text(message, 256),),
                aggregate=True, recommendation="review", observed_at=observed_at,
            )

    session = _SESSION_OPEN.search(message)
    if session and session.group("service").lower() not in ("sshd", "sudo"):
        return observation(
            "auth.session_open", "high", "Host login session opened",
            attributes={
                "service": bounded_text(session.group("service"), 64),
                "user": bounded_text(session.group("user"), 64),
            },
            fingerprint_values=(session.group("service"), session.group("user"), observed_at or ""),
            aggregate=False, recommendation="review", observed_at=observed_at,
        )

    priority = record.get("PRIORITY")
    unit = bounded_text(record.get("_SYSTEMD_UNIT") or identifier, 128)
    try:
        priority = int(priority)
    except (TypeError, ValueError):
        priority = 7
    lowered = message.lower()
    if identifier in ("kernel", "systemd") or unit.endswith(".service"):
        if "out of memory" in lowered or "oom-kill" in lowered or "killed process" in lowered:
            return observation(
                "system.oom", "critical", "Kernel out-of-memory action detected",
                attributes={"unit": unit, "message": bounded_text(message, 512)},
                fingerprint_values=(unit, bounded_text(message, 128)),
                aggregate=True, recommendation="review", observed_at=observed_at,
            )
        if priority <= 3 or "failed with result" in lowered or "entered failed state" in lowered:
            return observation(
                "system.service_error", "high", "System service reported a failure",
                attributes={"unit": unit, "priority": priority, "message": bounded_text(message, 512)},
                fingerprint_values=(unit, bounded_text(message, 128)),
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
        record.get("uri") or record.get("request_uri") or record.get("path"), 2048
    )
    path = raw_path.split("?", 1)[0][:512]
    try:
        decoded = urllib.parse.unquote(path, errors="replace").lower()
    except (TypeError, ValueError):
        decoded = path.lower()
    source_ip = valid_ip(record.get("remote_addr") or record.get("source_ip"))
    peer_ip = valid_ip(record.get("peer_addr") or record.get("realip_remote_addr"))
    try:
        status = int(record.get("status"))
    except (TypeError, ValueError):
        return None
    if not path or status == 499:
        return None
    attributes = {"method": method, "path": path, "status": status}
    if source_ip:
        attributes["source_ip"] = source_ip
    if peer_ip and peer_ip != source_ip:
        attributes["peer_ip"] = peer_ip
    request_time = record.get("request_time")
    try:
        attributes["request_time_ms"] = max(0, min(int(float(request_time) * 1000), 3600000))
    except (TypeError, ValueError):
        pass
    observed_at = _nginx_time(record)

    is_probe = any(marker in decoded for marker in _PROBE_MARKERS)
    is_probe = is_probe or any(decoded.endswith(suffix) for suffix in _PROBE_SUFFIXES)
    if is_probe:
        return observation(
            "web.probe", "high", "Known exploit path probe",
            attributes=attributes,
            fingerprint_values=(source_ip, method, path),
            aggregate=True, recommendation="block_ip", observed_at=observed_at,
        )
    if status >= 500:
        return observation(
            "web.error", "high" if status >= 502 else "warning",
            "Web request returned a server error",
            attributes=attributes,
            fingerprint_values=(status, method, path),
            aggregate=True, recommendation="review", observed_at=observed_at,
        )
    if status in (401, 403):
        return observation(
            "web.denied", "warning", "Web request was denied",
            attributes=attributes,
            fingerprint_values=(source_ip, status, path),
            aggregate=True, recommendation="none", observed_at=observed_at,
        )
    return None
