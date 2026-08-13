"""Central validation and secret-safe Event projection for MojoSec evidence."""

import datetime
import hashlib
import ipaddress
import math
import re
import unicodedata
import urllib.parse


_METHOD = re.compile(r"^[A-Z][A-Z0-9!#$%&'*+.^_`|~-]{0,31}$")
_USER = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
_TTY = re.compile(r"^[A-Za-z0-9_.\-/]{1,96}$")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_BOOT = re.compile(r"^[a-f0-9]{32}$")
_PRODUCT = re.compile(r"(?P<family>[A-Za-z][A-Za-z0-9._-]{0,63})/(?P<major>\d{1,6})")
_REQUEST_ID = re.compile(r"^[a-f0-9]{32}$")
_HTTP_PROTOCOL = re.compile(r"^HTTP/(?:0\.9|1\.0|1\.1|2(?:\.0)?|3(?:\.0)?)$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._-]{1,96}$")
_SYSTEMD_UNIT = re.compile(r"^user@(?P<uid>0|[1-9][0-9]{0,9})\.service$")
_FAILED_UNIT = re.compile(
    r"^[A-Za-z0-9@:.\\_-]{1,128}"
    r"\.(?:service|socket|target|device|mount|automount|swap|path|timer|slice|scope)$")
_FAILURE_KIND = re.compile(r"^[a-z0-9-]{1,64}$")
_UUID_SEGMENT = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[1-5a-f0-9][a-f0-9]{3}-"
    r"[89ab0-9][a-f0-9]{3}-[a-f0-9]{12}$", re.I)
_HEX_SEGMENT = re.compile(r"^[a-f0-9]{16,}$", re.I)
_TOKEN_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_DIGIT_SEGMENT = re.compile(r"^\d{6,}$")
_SENSOR_TOKEN_SEGMENT = re.compile(r"^~[a-f0-9]{12}$")
_TOKEN_CONTEXT = {
    "activate", "invite", "magic", "password", "recover", "reset", "token", "verify",
}
_KNOWN_UA = {
    "chrome": "Chrome", "chromium": "Chromium", "firefox": "Firefox",
    "safari": "Safari", "curl": "curl", "wget": "Wget",
    "python-requests": "python-requests", "go-http-client": "Go-http-client",
    "googlebot": "Googlebot", "bingbot": "bingbot",
}
_SUDO_COMMAND_FAMILIES = {
    "/bin/bash": "shell", "/bin/sh": "shell", "/usr/bin/bash": "shell",
    "/bin/systemctl": "service", "/usr/bin/systemctl": "service",
    "/sbin/service": "service", "/usr/sbin/service": "service",
    "/usr/bin/apt": "package", "/usr/bin/apt-get": "package",
    "/usr/bin/dnf": "package", "/usr/bin/dpkg": "package",
    "/usr/bin/rpm": "package", "/usr/bin/yum": "package",
    "/usr/bin/curl": "network_client", "/usr/bin/wget": "network_client",
    "/usr/bin/mysql": "database_client", "/usr/bin/psql": "database_client",
    "/usr/bin/rsync": "file_transfer", "/usr/bin/scp": "file_transfer",
}


def _text(value, limit=2048):
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    value = value.replace("\x00", "").encode("utf-8", errors="replace").decode("utf-8")
    return value[:limit]


def _digest(value):
    return hashlib.sha256(_text(value, 8192).encode("utf-8")).hexdigest()


def _ip(value):
    try:
        return str(ipaddress.ip_address(str(value)))
    except ValueError:
        return None


def _user(value):
    value = _text(value, 128)
    return value if _USER.fullmatch(value) else None


def _tty(value):
    value = _text(value, 96)
    if value.startswith("/dev/"):
        value = value[5:]
    return value if _TTY.fullmatch(value) and ".." not in value else None


def _method(value):
    if not isinstance(value, str):
        return None
    value = _text(value, 32).upper()
    return value if _METHOD.fullmatch(value) else None


def _host(value):
    if not isinstance(value, str):
        return None
    value = _text(value, 512).strip().rstrip(".").lower()
    if not value or any(char in value for char in "/\\@?#"):
        return None
    try:
        return str(ipaddress.ip_address(value.strip("[]")))
    except ValueError:
        pass
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return value if _HOST.fullmatch(value) else None


def _safe_path(value):
    if not isinstance(value, str):
        return None
    raw = _text(value, 2048)
    try:
        path = urllib.parse.urlsplit(raw).path
        path = urllib.parse.unquote(path, errors="replace")
    except (TypeError, ValueError):
        path = raw.split("?", 1)[0]
    pieces = []
    prior = ""
    for raw_piece in path.split("/")[:32]:
        piece = "".join(char for char in raw_piece if ord(char) >= 32)[:128]
        lowered = piece.lower()
        secret = bool(
            prior in _TOKEN_CONTEXT or "@" in piece or _UUID_SEGMENT.fullmatch(piece) or
            _HEX_SEGMENT.fullmatch(piece) or _TOKEN_SEGMENT.fullmatch(piece) or
            _DIGIT_SEGMENT.fullmatch(piece) or _SENSOR_TOKEN_SEGMENT.fullmatch(piece) or
            (piece.count(".") == 2 and len(piece) >= 24)
        )
        if piece in (".", ".."):
            pieces.append("~dot")
        else:
            pieces.append("~token" if secret else piece)
        if piece:
            prior = lowered
    result = "/".join(pieces)
    if path.startswith("/"):
        result = "/" + result.lstrip("/")
    return result[:512] or "/"


def _referrer_origin(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urllib.parse.urlsplit(_text(value, 2048))
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or parsed.username or parsed.password:
        return None
    host = _host(parsed.hostname)
    if host is None:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    default = 80 if parsed.scheme.lower() == "http" else 443
    if port is not None and not 1 <= port <= 65535:
        return None
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != default:
        authority += f":{port}"
    return f"{parsed.scheme.lower()}://{authority}"


def _referrer(value):
    if not isinstance(value, str):
        return None
    origin = _referrer_origin(value)
    if origin is None:
        return None
    try:
        path = urllib.parse.urlsplit(_text(value, 2048)).path
    except ValueError:
        return None
    safe_path = _safe_path(path)
    return origin + (safe_path if safe_path != "/" else "")


def _sanitize_url(match):
    value = match.group(0).rstrip(").,;]")
    suffix = match.group(0)[len(value):]
    safe = _referrer(value)
    return (safe or "<redacted-url>") + suffix


def _user_agent_display(value):
    if not isinstance(value, str):
        return None
    value = _text(value, 1536)
    if not value:
        return None
    value = "".join(
        " " if unicodedata.category(character) in ("Cc", "Cf") else character
        for character in value)
    value = re.sub(r"https?://[^\s]+", _sanitize_url, value, flags=re.I)
    value = re.sub(
        r"(?i)(authorization\s*[:=]\s*(?:basic|bearer)?\s*)[^\s,;]+",
        r"\1<redacted>", value)
    value = re.sub(r"(?i)\b(?:basic|bearer)\s+[^\s,;]+", "<redacted>", value)
    value = re.sub(
        r"(?i)\b(?:access[_-]?token|api[_-]?key|auth|authorization|password|secret|token)="
        r"[^\s&;,]+", "<redacted>", value)
    value = re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
        "<redacted>", value)
    value = re.sub(r"-----BEGIN[ A-Z0-9_-]{1,64}-----", "<redacted>", value)
    value = re.sub(
        r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
        "<redacted>", value)
    value = re.sub(
        r"(?<![A-Za-z0-9_])[A-Za-z0-9_+/=-]{24,}(?![A-Za-z0-9_])",
        "<redacted>", value)
    return re.sub(r"\s+", " ", value).strip()[:512] or None


def _user_agent(value):
    if not isinstance(value, str):
        return None
    raw = _text(value, 2048)
    if not raw:
        return None
    result = {"sha256": _digest(raw)}
    display = _user_agent_display(raw)
    if display:
        result["display"] = display
    match = _PRODUCT.search(raw)
    if match:
        family = _KNOWN_UA.get(match.group("family").lower(), "Other")
        result.update({"family": family, "major": int(match.group("major"))})
    else:
        result["family"] = "Other"
    return result


def _numbers(value, minimum, maximum, scale=1):
    if not isinstance(value, str) or len(value) > 256:
        return None
    items = re.split(r"\s*[,;:]\s*", value)
    if len(items) > 8:
        return None
    values = []
    for item in items:
        if not item or item == "-":
            continue
        try:
            number = float(item)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(number) or not minimum <= number <= maximum:
            return None
        values.append(int(number * scale))
    return values or None


def _integers(value, minimum, maximum):
    if not isinstance(value, str) or len(value) > 256:
        return None
    items = re.split(r"\s*[,;:]\s*", value)
    if len(items) > 8:
        return None
    values = []
    for item in items:
        if not item or item == "-":
            continue
        number = _integer(item, minimum, maximum)
        if number is None:
            return None
        values.append(number)
    return values or None


def _integer(value, minimum, maximum):
    try:
        if isinstance(value, bool) or isinstance(value, float):
            return None
        text = str(value)
        if not re.fullmatch(r"0|[1-9][0-9]*", text):
            return None
        number = int(text)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if minimum <= number <= maximum else None


def _token(value, pattern=_SAFE_TOKEN):
    if not isinstance(value, str):
        return None
    value = _text(value, 96)
    return value if pattern.fullmatch(value) else None


def _observed_at(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if parsed.tzinfo is not None else None


def _identity_context(attributes, evidence):
    boot_id = _text(attributes.get("boot_id"), 64).replace("-", "").lower()
    if _BOOT.fullmatch(boot_id):
        evidence["boot_id"] = boot_id
    for field in ("audit_session", "audit_loginuid"):
        value = _integer(attributes.get(field), 0, 4294967294)
        if value is not None:
            evidence[field] = value
    tty = _tty(attributes.get("tty"))
    if tty is not None:
        evidence["tty"] = tty


def _exact_bounded_text(value, byte_limit):
    """Accept trusted sensor text without changing one code point."""
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value if len(encoded) <= byte_limit else None


def _sudo_command(attributes, evidence):
    for field, byte_limit in (("command", 2048), ("command_path", 512), ("cwd", 512)):
        value = _exact_bounded_text(attributes.get(field), byte_limit)
        if value is None:
            continue
        evidence[field] = value
        marker = field + "_truncated"
        if attributes.get(marker) is True:
            evidence[marker] = True
    command_path = evidence.get("command_path")
    if command_path is not None and "command_path_truncated" not in evidence:
        evidence["command_family"] = _SUDO_COMMAND_FAMILIES.get(command_path, "unknown")


def project(kind, attributes, count=1, last_seen=None):
    """Return only canonical, secret-safe fields for Event projection."""
    if not isinstance(attributes, dict):
        return {"source_ip": None, "evidence": {}}
    evidence = {}
    source_ip = None
    if kind.startswith("auth.ssh_"):
        source_ip = _ip(attributes.get("source_ip"))
        for key, func in (("user", _user), ("auth_method", _method), ("tty", _tty)):
            value = func(attributes.get(key))
            if value is not None:
                evidence[key] = value
        _identity_context(attributes, evidence)
    elif kind in ("auth.sudo_command", "auth.sudo_failure"):
        for key in ("actor", "target_user"):
            value = _user(attributes.get(key))
            if value is not None:
                evidence[key] = value
        _identity_context(attributes, evidence)
        if kind == "auth.sudo_command":
            _sudo_command(attributes, evidence)
        provenance = attributes.get("attribution_provenance")
        candidate_ip = _ip(attributes.get("source_ip"))
        audit_proof = (
            provenance == "audit_session" and candidate_ip is not None and
            "actor" in evidence and "boot_id" in evidence and
            "audit_session" in evidence)
        who_proof = (
            provenance == "who" and candidate_ip is not None and
            "actor" in evidence and "tty" in evidence)
        if audit_proof or who_proof:
            source_ip = candidate_ip
            evidence["attribution"] = provenance
        else:
            evidence["attribution"] = "none"
    elif kind == "auth.session_open":
        if attributes.get("attribution_provenance") == "audit_session":
            source_ip = _ip(attributes.get("source_ip"))
            if source_ip:
                evidence["attribution"] = "audit_session"
        service = _text(attributes.get("service"), 128).lower()
        if service == "systemd-user":
            evidence["service"] = service
        target_user = _user(attributes.get("target_user"))
        if target_user:
            evidence["target_user"] = target_user
        for field in ("target_uid", "opener_uid", "producer_uid", "producer_pid"):
            value = _integer(attributes.get(field), 0 if field != "producer_pid" else 1,
                             4294967294)
            if value is not None:
                evidence[field] = value
        producer_comm = _text(attributes.get("producer_comm"), 96)
        if producer_comm == "(systemd)":
            evidence["producer_comm"] = producer_comm
        producer_exe = _text(attributes.get("producer_exe"), 256)
        if producer_exe == "/usr/lib/systemd/systemd":
            evidence["producer_exe"] = producer_exe
        unit = _text(attributes.get("systemd_unit"), 256)
        unit_match = _SYSTEMD_UNIT.fullmatch(unit)
        if unit_match:
            evidence["systemd_unit"] = unit
        _identity_context(attributes, evidence)
    elif kind in ("web.probe", "web.error", "web.denied"):
        source_ip = _ip(attributes.get("source_ip"))
        peer_ip = _ip(attributes.get("peer_ip"))
        method = _method(attributes.get("method"))
        host = _host(attributes.get("host"))
        status = _integer(attributes.get("status"), 100, 599)
        if peer_ip:
            evidence["peer_ip"] = peer_ip
        if method:
            evidence["method"] = method
        if host:
            evidence["host"] = host
        if status is not None and 100 <= status <= 599:
            evidence["status"] = status
        raw_path = attributes.get("request_uri")
        if raw_path in (None, ""):
            raw_path = attributes.get("path")
        path = _safe_path(raw_path)
        if path:
            evidence["path"] = path
        upstream = _integers(attributes.get("upstream_status"), 100, 599)
        if upstream:
            evidence["upstream_status"] = upstream
        volatile = {}
        request_id = _token(attributes.get("request_id"), _REQUEST_ID)
        if request_id:
            volatile["request_id"] = request_id
        scheme = _token(attributes.get("scheme"))
        if scheme in ("http", "https"):
            volatile["scheme"] = scheme
        protocol = _token(attributes.get("protocol"), _HTTP_PROTOCOL)
        if protocol:
            volatile["protocol"] = protocol
        for field in ("tls_protocol", "tls_cipher"):
            value = _token(attributes.get(field))
            if value:
                volatile[field] = value
        for field in ("remote_port", "peer_port", "server_port"):
            value = _integer(attributes.get(field), 1, 65535)
            if value is not None:
                volatile[field] = value
        for field in ("request_length", "response_bytes", "response_body_bytes"):
            value = _integer(attributes.get(field), 0, 2 ** 63 - 1)
            if value is not None:
                volatile[field] = value
        time_fields = (
            ("request_time", "request_time_ms", False),
            ("upstream_connect_time", "upstream_connect_time_ms", True),
            ("upstream_header_time", "upstream_header_time_ms", True),
            ("upstream_response_time", "upstream_response_time_ms", True),
        )
        for source, target, multiple in time_fields:
            values = _numbers(attributes.get(source), 0, 3600, 1000)
            if values:
                volatile[target] = values if multiple else values[0]
        for field in ("upstream_response_length", "upstream_bytes_received",
                      "upstream_bytes_sent"):
            values = _integers(attributes.get(field), 0, 2 ** 63 - 1)
            if values:
                volatile[field] = values
        referrer_origin = _referrer_origin(attributes.get("referrer"))
        referrer = _referrer(attributes.get("referrer"))
        user_agent = _user_agent(attributes.get("user_agent"))
        if referrer_origin:
            volatile["referrer_origin"] = referrer_origin
        if referrer:
            volatile["referrer"] = referrer
        if user_agent:
            volatile["user_agent"] = user_agent
        if count == 1:
            evidence.update(volatile)
        elif count > 1 and volatile:
            observed_at = _observed_at(last_seen)
            if observed_at:
                evidence["last_occurrence_sample"] = {
                    "semantics": "last_occurrence", "observed_at": observed_at,
                    **volatile,
                }
    elif kind in ("system.service_error", "system.oom"):
        unit = attributes.get("unit")
        if isinstance(unit, str) and (unit == "kernel" or _FAILED_UNIT.fullmatch(unit)):
            evidence["unit"] = unit
        failure_kind = attributes.get("failure_kind")
        if isinstance(failure_kind, str) and _FAILURE_KIND.fullmatch(failure_kind):
            evidence["failure_kind"] = failure_kind
    return {"source_ip": source_ip, "evidence": evidence}
