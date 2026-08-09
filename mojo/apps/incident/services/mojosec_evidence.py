"""Central validation and secret-safe Event projection for MojoSec evidence."""

import hashlib
import ipaddress
import re
import shlex
import urllib.parse


_METHOD = re.compile(r"^[A-Z][A-Z0-9!#$%&'*+.^_`|~-]{0,31}$")
_USER = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
_TTY = re.compile(r"^[A-Za-z0-9_.\-/]{1,96}$")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_BOOT = re.compile(r"^[a-f0-9]{32}$")
_PRODUCT = re.compile(r"(?P<family>[A-Za-z][A-Za-z0-9._-]{0,63})/(?P<major>\d{1,6})")
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
    value = _text(value, 32).upper()
    return value if _METHOD.fullmatch(value) else None


def _host(value):
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


def _user_agent(value):
    raw = _text(value, 2048)
    if not raw:
        return None
    result = {"sha256": _digest(raw)}
    match = _PRODUCT.search(raw)
    if match:
        family = _KNOWN_UA.get(match.group("family").lower(), "Other")
        result.update({"family": family, "major": int(match.group("major"))})
    else:
        result["family"] = "Other"
    return result


def _numbers(value, minimum, maximum, scale=1):
    values = []
    for item in re.split(r"\s*[,;:]\s*", _text(value, 256))[:8]:
        if not item or item == "-":
            continue
        try:
            number = float(item)
        except ValueError:
            return None
        if not minimum <= number <= maximum:
            return None
        values.append(int(number * scale))
    return values or None


def _sudo_command(attributes):
    raw = _text(attributes.get("command"), 4096)
    try:
        words = shlex.split(raw, posix=True) if raw else []
    except ValueError:
        words = []
    command_path = _text(attributes.get("command_path"), 512)
    candidate = command_path if command_path in _SUDO_COMMAND_FAMILIES else ""
    if not candidate and words and words[0] in _SUDO_COMMAND_FAMILIES:
        candidate = words[0]
    # The receipt owns the executable, digest, command, and arguments. Event
    # exposes only a server-owned classification with a constant shape.
    return {
        "family": _SUDO_COMMAND_FAMILIES.get(candidate, "unknown"),
        "detail": "<redacted>",
    }


def project(kind, attributes, count=1):
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
    elif kind in ("auth.sudo_command", "auth.sudo_failure"):
        provenance = attributes.get("attribution_provenance")
        if provenance in ("audit_session", "who"):
            source_ip = _ip(attributes.get("source_ip"))
            evidence["attribution"] = provenance
        for key in ("actor", "target_user"):
            value = _user(attributes.get(key))
            if value is not None:
                evidence[key] = value
        tty = _tty(attributes.get("tty"))
        if tty is not None:
            evidence["tty"] = tty
        boot_id = _text(attributes.get("boot_id"), 64).replace("-", "").lower()
        if _BOOT.fullmatch(boot_id):
            evidence["boot_id"] = boot_id
        try:
            audit_session = int(attributes.get("audit_session"))
        except (TypeError, ValueError):
            audit_session = None
        if audit_session is not None and 0 <= audit_session < 4294967295:
            evidence["audit_session"] = audit_session
        if kind == "auth.sudo_command":
            evidence["command"] = _sudo_command(attributes)
    elif kind in ("web.probe", "web.error", "web.denied"):
        source_ip = _ip(attributes.get("source_ip"))
        peer_ip = _ip(attributes.get("peer_ip"))
        method = _method(attributes.get("method"))
        host = _host(attributes.get("host"))
        try:
            status = int(attributes.get("status"))
        except (TypeError, ValueError):
            status = None
        if peer_ip:
            evidence["peer_ip"] = peer_ip
        if method:
            evidence["method"] = method
        if host:
            evidence["host"] = host
        if status is not None and 100 <= status <= 599:
            evidence["status"] = status
        evidence["path"] = _safe_path(
            attributes.get("request_uri") or attributes.get("path"))
        upstream = _numbers(attributes.get("upstream_status"), 100, 599)
        if upstream:
            evidence["upstream_status"] = upstream
        # A repeated Event represents one fingerprint bucket; volatile samples
        # are omitted instead of presenting the last request as the whole set.
        if count == 1:
            request_time = _numbers(attributes.get("request_time"), 0, 3600, 1000)
            upstream_time = _numbers(
                attributes.get("upstream_response_time"), 0, 3600, 1000)
            referrer = _referrer_origin(attributes.get("referrer"))
            user_agent = _user_agent(attributes.get("user_agent"))
            if request_time:
                evidence["request_time_ms"] = request_time[0]
            if upstream_time:
                evidence["upstream_response_time_ms"] = upstream_time
            if referrer:
                evidence["referrer_origin"] = referrer
            if user_agent:
                evidence["user_agent"] = user_agent
    return {"source_ip": source_ip, "evidence": evidence}
